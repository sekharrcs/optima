"""API integration tests for the Slice 5 small-first vertical path."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from optima.api.app import create_app
from optima.api.dependencies import ExecutionDependencies
from optima.api.models import RunRequest
from optima.cache import (
    FakeSemanticCache,
    SemanticCache,
    SemanticCacheLookupRequest,
    SemanticCacheLookupResult,
)
from optima.config import AppSettings
from optima.context import (
    ContextPreservationEvidence,
    ContextReductionResult,
    FakeContextReducer,
    RecordingTokenCounter,
    RegexTokenCounter,
)
from optima.context.safety import DeterministicExtractiveSafetyPolicy
from optima.cost import CostCalculator, PriceCatalog, PriceCatalogEntry
from optima.domain.cache import CacheCandidate
from optima.domain.embedding import EmbeddingAttempt, EmbeddingUsage
from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    ExecutionEventCode,
    ModelRole,
    PlannerReasonCode,
    SemanticCacheOutcome,
)
from optima.domain.request_binding import RequestBinding, build_request_binding
from optima.domain.run import RunResult
from optima.evaluation import EvaluationEvidence, FakeEvaluator
from optima.providers import (
    FakeProviderResponse,
    ModelProviderRequest,
    ModelProviderResult,
    build_fake_small_provider,
    build_fake_strong_provider,
)


class IncrementingClock:
    """Deterministic monotonic clock for API execution tests."""

    def __init__(self) -> None:
        self._value = 0.0

    def now(self) -> float:
        value = self._value
        self._value += 0.001
        return value


class UncheckedSemanticCache:
    """Adapter that returns its configured value without normalization."""

    def __init__(self, candidate: CacheCandidate) -> None:
        self._candidate = candidate

    async def lookup(
        self,
        request: SemanticCacheLookupRequest,
    ) -> SemanticCacheLookupResult:
        return SemanticCacheLookupResult.model_construct(
            candidate=self._candidate,
            embedding_attempt=None,
        )


class SubstitutingCacheCandidate(CacheCandidate):
    """Provider value that attempts output substitution during detachment."""

    def detached_copy(self) -> CacheCandidate:
        values = self.model_dump(mode="python")
        values["output_text"] = "substituted output"
        return CacheCandidate.model_validate(values)


class RaisingSmallProvider:
    """Small-role API test provider that fails before returning measurements."""

    provider_name = "raising-provider"
    deployment_name = "small"
    model_role = ModelRole.SMALL

    async def generate(
        self,
        request: ModelProviderRequest,
    ) -> ModelProviderResult:
        raise RuntimeError("provider unavailable")


def request_payload(**updates: object) -> dict[str, object]:
    """Build one strict request that Planner V1 routes to small-first."""
    values: dict[str, object] = {
        "input_text": "Summarize the incident",
        "context": "Incident context",
        "request_profile": {
            "task_type": "SUMMARIZATION",
            "complexity": "LOW",
            "input_tokens": 100,
            "risk_tier": "LOW",
            "cache_eligible": False,
            "has_large_context": False,
        },
        "quality_profile": "HIGH",
        "optimization_mode": "COST",
        "risk_tier": "LOW",
        "reference_output": "Expected summary",
        "criteria": ["Preserve the outcome"],
        "metadata": {"scenario": "api"},
    }
    values.update(updates)
    return values


def cache_eligible_profile(**updates: object) -> dict[str, object]:
    """Build the default API profile with semantic-cache lookup enabled."""
    values: dict[str, object] = {
        "task_type": "SUMMARIZATION",
        "complexity": "LOW",
        "input_tokens": 100,
        "risk_tier": "LOW",
        "cache_eligible": True,
        "has_large_context": False,
    }
    values.update(updates)
    return values


def evidence(score: float) -> EvaluationEvidence:
    """Build evaluator-owned fake evidence."""
    return EvaluationEvidence(
        evaluator_type="fake-deterministic",
        evaluator_valid=True,
        score=score,
        metadata={"source": "api-test"},
    )


def request_binding(
    source_payload: dict[str, object] | None = None,
) -> RequestBinding:
    """Derive a complete binding from one API source request fixture."""
    request = RunRequest.model_validate(source_payload or request_payload())
    return build_request_binding(
        input_text=request.input_text,
        context=request.context,
        reference_output=request.reference_output,
        criteria=request.criteria,
        metadata=request.metadata,
        task_type=request.request_profile.task_type,
        complexity=request.request_profile.complexity,
    )


def cache_candidate(
    *,
    source_payload: dict[str, object] | None = None,
    **updates: object,
) -> CacheCandidate:
    """Build one resolved result whose source threshold differs truthfully."""
    values: dict[str, object] = {
        "source_run_id": "run-source-cache-1",
        "output_text": "exact cached output",
        "request_binding": request_binding(source_payload),
        "similarity": 0.97,
        "prior_evaluation": EvaluationResult(
            evaluator_type="source-deterministic",
            evaluator_valid=True,
            score=0.95,
            threshold=0.80,
            mandatory_checks_passed=True,
            passed=True,
            reasons=("Source contract passed",),
            metadata={"source_run_id": "run-source-cache-1"},
        ),
        "contract_compatible": True,
        "safe_to_reuse": True,
    }
    values.update(updates)
    return CacheCandidate.model_validate(values)


def with_semantic_cache(
    configured: ExecutionDependencies,
    cache: SemanticCache | None,
    *,
    context_reduction_enabled: bool = False,
) -> ExecutionDependencies:
    """Enable semantic cache while preserving fresh application dependencies."""
    return replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=True,
            context_reduction_enabled=context_reduction_enabled,
            historical_policy_enabled=False,
        ),
        semantic_cache=cache,
    )


def reduction_result(original_context: str) -> ContextReductionResult:
    """Build reducer evidence measured by the API test counter."""
    counter = RegexTokenCounter()
    reduced_context = "Priya Nair owns incident INC-204."
    return ContextReductionResult(
        reduced_context=reduced_context,
        original_token_count=counter.count(original_context),
        reduced_token_count=counter.count(reduced_context),
        reducer_name="fake-context-reducer",
        method="EXTRACTIVE_TEST",
        token_counter_name=counter.counter_name,
        preservation=ContextPreservationEvidence(
            source_order_preserved=True,
            original_segment_count=2,
            retained_segment_indexes=(0,),
            removed_duplicate_count=0,
            removed_irrelevant_count=1,
            task_terms_used=("incident",),
        ),
    )


def dependencies(
    *scores: float,
) -> tuple[ExecutionDependencies, Any, Any, FakeEvaluator]:
    """Build fresh application-scoped fakes for one API test."""
    small = build_fake_small_provider(
        provider_name="fake",
        deployment_name="small",
        responses=(
            FakeProviderResponse(
                output_text="small output",
                input_tokens=100,
                output_tokens=20,
            ),
        ),
        clock=IncrementingClock(),
    )
    strong = build_fake_strong_provider(
        provider_name="fake",
        deployment_name="strong",
        responses=(
            FakeProviderResponse(
                output_text="strong output",
                input_tokens=110,
                output_tokens=30,
            ),
        ),
        clock=IncrementingClock(),
    )
    evaluator = FakeEvaluator(responses=tuple(evidence(score) for score in scores))
    calculator = CostCalculator(
        PriceCatalog(
            version="api-test-v1",
            currency="TEST",
            entries=(
                PriceCatalogEntry(
                    provider="fake",
                    deployment="small",
                    input_rate_per_million_tokens=Decimal("2"),
                    output_rate_per_million_tokens=Decimal("40"),
                ),
                PriceCatalogEntry(
                    provider="fake",
                    deployment="strong",
                    input_rate_per_million_tokens=Decimal("30"),
                    output_rate_per_million_tokens=Decimal("190"),
                ),
            ),
        )
    )
    configured = ExecutionDependencies(
        settings=AppSettings(
            semantic_cache_enabled=False,
            context_reduction_enabled=False,
            historical_policy_enabled=False,
        ),
        small_provider=small,
        strong_provider=strong,
        evaluator=evaluator,
        cost_calculator=calculator,
        monotonic_clock=IncrementingClock(),
        utc_now=lambda: datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
        run_id_factory=lambda: "run-api-1",
        correlation_id_factory=lambda: "correlation-api-1",
    )
    return configured, small, strong, evaluator


def test_run_endpoint_returns_small_pass_with_plan_and_runtime_facts() -> None:
    """Prove the complete API path with injected local deterministic fakes."""
    configured, small, strong, evaluator = dependencies(0.93)
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run-api-1"
    assert body["correlation_id"] == "correlation-api-1"
    assert body["final_output"] == "small output"
    assert body["contract_met"] is True
    assert body["escalated"] is False
    assert body["total_input_tokens"] == 100
    assert body["total_output_tokens"] == 20
    assert body["total_tokens"] == 120
    assert body["total_calculated_cost"] == "0.001"
    assert body["total_cost_provenance"] == {
        "catalog_version": "api-test-v1",
        "currency": "TEST",
    }
    assert body["model_usages"][0]["calculated_cost"] == "0.001"
    assert body["model_usages"][0]["pricing_provenance"] == {
        "catalog_version": "api-test-v1",
        "currency": "TEST",
    }
    assert (
        PlannerReasonCode.SMALL_FIRST_SELECTED in body["execution_plan"]["reason_codes"]
    )
    assert ExecutionEventCode.QUALITY_CONTRACT_MET in {
        code for step in body["steps"] for code in step["event_codes"]
    }
    assert len(small.calls) == 1
    assert len(strong.calls) == 0
    assert len(evaluator.calls) == 1


def test_run_endpoint_serializes_measured_reduction_evidence() -> None:
    """Return canonical runtime evidence and send measured reduced context to SMALL."""
    original_context = (
        "Priya Nair owns incident INC-204.\nPriya Nair owns incident INC-204."
    )
    configured, small, strong, evaluator = dependencies(0.93)
    reducer = FakeContextReducer((reduction_result(original_context),))
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=False,
            context_reduction_enabled=True,
            historical_policy_enabled=False,
        ),
        context_reducer=reducer,
        token_counter=RegexTokenCounter(),
        context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            context=original_context,
            request_profile={
                "task_type": "SUMMARIZATION",
                "complexity": "LOW",
                "input_tokens": 4_000,
                "risk_tier": "LOW",
                "cache_eligible": False,
                "has_large_context": True,
            },
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_plan"]["context_policy"] == "REDUCE"
    assert body["steps"][0]["step_type"] == "CONTEXT_REDUCTION"
    assert body["steps"][0]["status"] == "SUCCEEDED"
    reduction = body["steps"][0]["context_reduction"]
    assert reduction["outcome"] == "APPLIED"
    assert reduction["original_token_count"] == RegexTokenCounter().count(
        original_context
    )
    assert reduction["effective_token_count"] == RegexTokenCounter().count(
        reduction_result(original_context).reduced_context
    )
    assert reduction["method"] == "EXTRACTIVE_TEST"
    assert len(reducer.calls) == 1
    assert small.calls[0].request.input_text == "Summarize the incident"
    assert (
        small.calls[0].request.context
        == reduction_result(original_context).reduced_context
    )
    assert len(strong.calls) == 0
    assert evaluator.calls[0].request.context == original_context


def test_disabled_configuration_bypasses_reducer_and_preserves_original() -> None:
    """Use Planner V1 disabled behavior with zero reducer calls and no fake step."""
    original_context = (
        "Priya Nair owns incident INC-204.\nPriya Nair owns incident INC-204."
    )
    configured, small, _, _ = dependencies(0.93)
    reducer = FakeContextReducer((reduction_result(original_context),))
    counter = RecordingTokenCounter(RegexTokenCounter())
    configured = replace(
        configured,
        context_reducer=reducer,
        token_counter=counter,
        context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            context=original_context,
            request_profile={
                "task_type": "SUMMARIZATION",
                "complexity": "LOW",
                "input_tokens": 4_000,
                "risk_tier": "LOW",
                "cache_eligible": False,
                "has_large_context": True,
            },
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_plan"]["context_policy"] == "KEEP_ORIGINAL"
    assert (
        PlannerReasonCode.CONTEXT_REDUCTION_DISABLED
        in body["execution_plan"]["reason_codes"]
    )
    assert all(step["step_type"] != "CONTEXT_REDUCTION" for step in body["steps"])
    assert reducer.calls == ()
    assert counter.calls == ()
    assert small.calls[0].request.context == original_context


def test_unsupported_task_is_not_selected_or_called() -> None:
    """Reject a configured reducer for a task outside its supported envelope."""
    original_context = (
        "Priya Nair owns incident INC-204.\nPriya Nair owns incident INC-204."
    )
    configured, small, _, _ = dependencies(0.93)
    reducer = FakeContextReducer((reduction_result(original_context),))
    counter = RecordingTokenCounter(RegexTokenCounter())
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=False,
            context_reduction_enabled=True,
            historical_policy_enabled=False,
        ),
        context_reducer=reducer,
        token_counter=counter,
        context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            context=original_context,
            request_profile={
                "task_type": "Q_AND_A",
                "complexity": "LOW",
                "input_tokens": 4_000,
                "risk_tier": "LOW",
                "cache_eligible": False,
                "has_large_context": True,
            },
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_plan"]["context_policy"] == "KEEP_ORIGINAL"
    assert (
        PlannerReasonCode.SAFE_REDUCER_UNAVAILABLE
        in body["execution_plan"]["reason_codes"]
    )
    assert reducer.calls == ()
    assert counter.calls == ()
    assert small.calls[0].request.context == original_context


def test_configured_reducer_without_safety_policy_is_not_task_safe() -> None:
    """Do not infer task safety merely from configured runtime dependencies."""
    original_context = (
        "Priya Nair owns incident INC-204.\nPriya Nair owns incident INC-204."
    )
    configured, small, _, _ = dependencies(0.93)
    reducer = FakeContextReducer((reduction_result(original_context),))
    counter = RecordingTokenCounter(RegexTokenCounter())
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=False,
            context_reduction_enabled=True,
            historical_policy_enabled=False,
        ),
        context_reducer=reducer,
        token_counter=counter,
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            context=original_context,
            request_profile={
                "task_type": "SUMMARIZATION",
                "complexity": "LOW",
                "input_tokens": 4_000,
                "risk_tier": "LOW",
                "cache_eligible": False,
                "has_large_context": True,
            },
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_plan"]["context_policy"] == "KEEP_ORIGINAL"
    assert (
        PlannerReasonCode.SAFE_REDUCER_UNAVAILABLE
        in body["execution_plan"]["reason_codes"]
    )
    assert reducer.calls == ()
    assert counter.calls == ()
    assert small.calls[0].request.context == original_context


def test_reducer_safety_is_recomputed_for_each_request() -> None:
    """Do not reuse a supported request's safety decision for a later request."""
    original_context = (
        "Priya Nair owns incident INC-204.\nPriya Nair owns incident INC-204."
    )
    configured, small, _, _ = dependencies(0.93, 0.93)
    reducer = FakeContextReducer((reduction_result(original_context),))
    counter = RecordingTokenCounter(RegexTokenCounter())
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=False,
            context_reduction_enabled=True,
            historical_policy_enabled=False,
        ),
        context_reducer=reducer,
        token_counter=counter,
        context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
    )
    client = TestClient(create_app(execution_dependencies=configured))
    profile = {
        "task_type": "SUMMARIZATION",
        "complexity": "LOW",
        "input_tokens": 4_000,
        "risk_tier": "LOW",
        "cache_eligible": False,
        "has_large_context": True,
    }

    supported = client.post(
        "/api/v1/runs",
        json=request_payload(context=original_context, request_profile=profile),
    )
    unsupported = client.post(
        "/api/v1/runs",
        json=request_payload(
            context=original_context,
            request_profile={**profile, "task_type": "Q_AND_A"},
        ),
    )

    assert supported.status_code == 200
    assert supported.json()["execution_plan"]["context_policy"] == "REDUCE"
    assert unsupported.status_code == 200
    assert unsupported.json()["execution_plan"]["context_policy"] == "KEEP_ORIGINAL"
    assert (
        PlannerReasonCode.SAFE_REDUCER_UNAVAILABLE
        in unsupported.json()["execution_plan"]["reason_codes"]
    )
    assert len(reducer.calls) == 1
    assert len(counter.calls) == 2
    assert (
        small.calls[0].request.context
        == reduction_result(original_context).reduced_context
    )
    assert small.calls[1].request.context == original_context


def test_critical_high_risk_preserves_planner_safeguard_without_runtime_calls() -> None:
    """Let Planner V1 reject a supported task under its critical/high-risk rule."""
    original_context = (
        "Priya Nair owns incident INC-204.\nPriya Nair owns incident INC-204."
    )
    configured, small, _, _ = dependencies(0.93)
    reducer = FakeContextReducer((reduction_result(original_context),))
    counter = RecordingTokenCounter(RegexTokenCounter())
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=False,
            context_reduction_enabled=True,
            historical_policy_enabled=False,
        ),
        context_reducer=reducer,
        token_counter=counter,
        context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            context=original_context,
            quality_profile="CRITICAL",
            risk_tier="HIGH",
            request_profile={
                "task_type": "SUMMARIZATION",
                "complexity": "LOW",
                "input_tokens": 4_000,
                "risk_tier": "LOW",
                "cache_eligible": False,
                "has_large_context": True,
            },
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_plan"]["context_policy"] == "KEEP_ORIGINAL"
    assert (
        PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_HIGH_RISK
        in body["execution_plan"]["reason_codes"]
    )
    assert (
        PlannerReasonCode.SAFE_REDUCER_UNAVAILABLE
        not in body["execution_plan"]["reason_codes"]
    )
    assert reducer.calls == ()
    assert counter.calls == ()
    assert small.calls[0].request.context == original_context


def test_run_endpoint_escalates_once_and_returns_strong_facts() -> None:
    """Expose both calls, final strong evidence, and escalation runtime events."""
    configured, small, strong, evaluator = dependencies(0.80, 0.95)
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["final_output"] == "strong output"
    assert body["contract_met"] is True
    assert body["escalated"] is True
    assert body["total_input_tokens"] == 210
    assert body["total_output_tokens"] == 50
    assert body["total_tokens"] == 260
    assert body["total_calculated_cost"] == "0.010"
    assert body["total_cost_provenance"] == {
        "catalog_version": "api-test-v1",
        "currency": "TEST",
    }
    assert [usage["model_role"] for usage in body["model_usages"]] == [
        ModelRole.SMALL,
        ModelRole.STRONG,
    ]
    runtime_events = [code for step in body["steps"] for code in step["event_codes"]]
    assert runtime_events.count(ExecutionEventCode.ESCALATED_TO_STRONG) == 1
    assert ExecutionEventCode.ESCALATION_REQUIRED in runtime_events
    assert len(small.calls) == 1
    assert len(strong.calls) == 1
    assert len(evaluator.calls) == 2


def test_run_endpoint_returns_measured_final_contract_failure() -> None:
    """Return the final strong result when valid evidence still misses quality."""
    configured, _, strong, evaluator = dependencies(0.70, 0.85)
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["final_output"] == "strong output"
    assert body["contract_met"] is False
    assert body["final_evaluation"] == body["evaluations"][-1]
    assert len(strong.calls) == 1
    assert len(evaluator.calls) == 2
    assert ExecutionEventCode.FINAL_QUALITY_CONTRACT_NOT_MET in {
        code for step in body["steps"] for code in step["event_codes"]
    }


def test_run_endpoint_strictly_rejects_unknown_and_coerced_inputs() -> None:
    """Keep request validation strict without executing dependencies."""
    configured, small, strong, evaluator = dependencies(0.93)
    client = TestClient(create_app(execution_dependencies=configured))
    unknown = client.post(
        "/api/v1/runs",
        json=request_payload(undocumented=True),
    )
    coerced = client.post(
        "/api/v1/runs",
        json=request_payload(
            request_profile={
                "task_type": "SUMMARIZATION",
                "complexity": "LOW",
                "input_tokens": "100",
                "risk_tier": "LOW",
                "cache_eligible": False,
                "has_large_context": False,
            }
        ),
    )

    assert unknown.status_code == 422
    assert coerced.status_code == 422
    assert len(small.calls) == 0
    assert len(strong.calls) == 0
    assert len(evaluator.calls) == 0


def test_default_app_returns_structured_unconfigured_error() -> None:
    """Keep create_app usable without silently wiring fake runtime services."""
    response = TestClient(create_app()).post(
        "/api/v1/runs",
        json=request_payload(),
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "EXECUTION_NOT_CONFIGURED",
            "message": "Model providers and quality evaluator are not configured",
            "facts": {},
        }
    }


def test_provider_failure_returns_structured_failed_run_without_fallback() -> None:
    """Expose operational failure facts without output, compliance, or usage."""
    configured, _, strong, evaluator = dependencies(0.93)
    failed_dependencies = replace(
        configured,
        small_provider=RaisingSmallProvider(),
    )
    response = TestClient(create_app(execution_dependencies=failed_dependencies)).post(
        "/api/v1/runs", json=request_payload()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["final_output"] is None
    assert body["contract_met"] is None
    assert body["escalated"] is False
    assert body["model_usages"] == []
    assert body["evaluations"] == []
    assert body["total_input_tokens"] is None
    assert body["total_output_tokens"] is None
    assert body["total_tokens"] is None
    assert body["total_calculated_cost"] is None
    assert body["steps"][-1]["status"] == "FAILED"
    assert body["error"] == "SMALL model call RuntimeError"
    assert len(strong.calls) == 0
    assert len(evaluator.calls) == 0


def assert_measured_strong_direct(
    body: dict[str, Any],
    *,
    expected_context_source: str,
    expected_score: float = 0.93,
    expected_threshold: float = 0.9,
) -> None:
    """Assert one measured direct STRONG attempt without escalation evidence."""
    assert body["status"] == "COMPLETED"
    assert body["final_output"] == "strong output"
    assert body["contract_met"] is True
    assert body["escalated"] is False
    assert body["execution_plan"]["model_policy"] == "STRONG_DIRECT"
    assert body["execution_plan"]["initial_model_role"] == ModelRole.STRONG
    assert body["execution_plan"]["escalation_model_role"] is None
    assert [usage["model_role"] for usage in body["model_usages"]] == [ModelRole.STRONG]
    assert body["model_usages"][0]["input_tokens"] == 110
    assert body["model_usages"][0]["output_tokens"] == 30
    assert body["model_usages"][0]["calculated_cost"] == "0.009"
    assert body["model_usages"][0]["pricing_provenance"] == {
        "catalog_version": "api-test-v1",
        "currency": "TEST",
    }
    assert body["total_input_tokens"] == 110
    assert body["total_output_tokens"] == 30
    assert body["total_tokens"] == 140
    assert body["total_calculated_cost"] == "0.009"
    assert body["total_cost_provenance"] == {
        "catalog_version": "api-test-v1",
        "currency": "TEST",
    }
    assert len(body["evaluations"]) == 1
    assert body["final_evaluation"] == body["evaluations"][0]
    assert body["final_evaluation"]["evaluator_type"] == "fake-deterministic"
    assert body["final_evaluation"]["score"] == expected_score
    assert body["final_evaluation"]["threshold"] == expected_threshold
    assert body["final_evaluation"]["passed"] is True
    model_steps = [step for step in body["steps"] if step["step_type"] == "MODEL_CALL"]
    assert len(model_steps) == 1
    assert model_steps[0]["facts"]["model_role"] == ModelRole.STRONG
    assert model_steps[0]["context_source"] == expected_context_source
    assert all(step["step_type"] != "ESCALATION" for step in body["steps"])
    runtime_events = [code for step in body["steps"] for code in step["event_codes"]]
    assert ExecutionEventCode.QUALITY_CONTRACT_MET in runtime_events
    assert ExecutionEventCode.ESCALATION_REQUIRED not in runtime_events
    assert ExecutionEventCode.ESCALATED_TO_STRONG not in runtime_events


def test_high_complexity_executes_strong_direct_with_measured_facts() -> None:
    """Execute HIGH complexity with one verified STRONG call and no SMALL call."""
    configured, small, strong, evaluator = dependencies(0.93)
    high_profile = {
        "task_type": "GENERAL_REASONING",
        "complexity": "HIGH",
        "input_tokens": 100,
        "risk_tier": "LOW",
        "cache_eligible": False,
        "has_large_context": False,
    }
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(request_profile=high_profile),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_plan"]["human_readable_name"] == "Strong -> Verify"
    assert (
        PlannerReasonCode.HIGH_COMPLEXITY_STRONG_DIRECT
        in body["execution_plan"]["reason_codes"]
    )
    assert_measured_strong_direct(body, expected_context_source="ORIGINAL")
    assert len(small.calls) == 0
    assert len(strong.calls) == 1
    assert len(evaluator.calls) == 1


def test_quality_mode_strong_direct_executes_successfully() -> None:
    """Execute the Quality-mode policy that selects STRONG directly."""
    configured, small, strong, evaluator = dependencies(0.93)
    quality_mode_profile = {
        "task_type": "GENERAL_REASONING",
        "complexity": "MEDIUM",
        "input_tokens": 100,
        "risk_tier": "LOW",
        "cache_eligible": False,
        "has_large_context": False,
    }

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            request_profile=quality_mode_profile,
            optimization_mode="QUALITY",
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert (
        PlannerReasonCode.QUALITY_MODE_PREFERS_STRONG
        in body["execution_plan"]["reason_codes"]
    )
    assert body["execution_plan"]["human_readable_name"] == "Strong -> Verify"
    assert_measured_strong_direct(body, expected_context_source="ORIGINAL")
    assert len(small.calls) == 0
    assert len(strong.calls) == 1
    assert len(evaluator.calls) == 1


def test_reduce_strong_direct_uses_reduced_model_context_and_original_evaluation() -> (
    None
):
    """Reduce once for STRONG while evaluating against the original context."""
    original_context = (
        "System ARC-9 requires audit logging.\nSystem ARC-9 requires audit logging."
    )
    configured, small, strong, evaluator = dependencies(0.99)
    reducer = FakeContextReducer((reduction_result(original_context),))
    counter = RecordingTokenCounter(RegexTokenCounter())
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=False,
            context_reduction_enabled=True,
            historical_policy_enabled=False,
        ),
        context_reducer=reducer,
        token_counter=counter,
        context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
    )
    strong_direct_profile = {
        "task_type": "SUMMARIZATION",
        "complexity": "LOW",
        "input_tokens": 8_000,
        "risk_tier": "LOW",
        "cache_eligible": False,
        "has_large_context": True,
    }

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            context=original_context,
            request_profile=strong_direct_profile,
            quality_profile="CRITICAL",
            optimization_mode="QUALITY",
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_plan"]["context_policy"] == "REDUCE"
    assert body["execution_plan"]["human_readable_name"] == (
        "Reduce Context -> Strong -> Verify"
    )
    assert body["steps"][0]["context_reduction"]["outcome"] == "APPLIED"
    assert body["steps"][0]["context_reduction"]["context_source"] == "REDUCED"
    assert_measured_strong_direct(
        body,
        expected_context_source="REDUCED",
        expected_score=0.99,
        expected_threshold=0.95,
    )
    assert len(reducer.calls) == 1
    assert len(counter.calls) == 2
    assert len(small.calls) == 0
    assert len(strong.calls) == 1
    assert (
        strong.calls[0].request.context
        == reduction_result(original_context).reduced_context
    )
    assert len(evaluator.calls) == 1
    assert evaluator.calls[0].request.context == original_context


def test_unsafe_reduction_keeps_original_for_strong_direct_without_reducer_call() -> (
    None
):
    """Keep original context when reducer policy does not support the task."""
    original_context = (
        "System ARC-9 requires audit logging.\nSystem ARC-9 requires audit logging."
    )
    configured, small, strong, evaluator = dependencies(0.93)
    reducer = FakeContextReducer((reduction_result(original_context),))
    counter = RecordingTokenCounter(RegexTokenCounter())
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=False,
            context_reduction_enabled=True,
            historical_policy_enabled=False,
        ),
        context_reducer=reducer,
        token_counter=counter,
        context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            context=original_context,
            request_profile={
                "task_type": "Q_AND_A",
                "complexity": "HIGH",
                "input_tokens": 8_000,
                "risk_tier": "LOW",
                "cache_eligible": False,
                "has_large_context": True,
            },
            optimization_mode="QUALITY",
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_plan"]["context_policy"] == "KEEP_ORIGINAL"
    assert (
        PlannerReasonCode.SAFE_REDUCER_UNAVAILABLE
        in body["execution_plan"]["reason_codes"]
    )
    assert_measured_strong_direct(body, expected_context_source="ORIGINAL")
    assert reducer.calls == ()
    assert counter.calls == ()
    assert len(small.calls) == 0
    assert len(strong.calls) == 1
    assert strong.calls[0].request.context == original_context
    assert len(evaluator.calls) == 1
    assert evaluator.calls[0].request.context == original_context


def test_separate_apps_do_not_share_mutable_fake_state() -> None:
    """Keep provider and evaluator call histories scoped to injected app state."""
    first, first_small, _, _ = dependencies(0.93)
    second, second_small, _, _ = dependencies(0.93)

    first_response = TestClient(create_app(execution_dependencies=first)).post(
        "/api/v1/runs", json=request_payload()
    )
    second_response = TestClient(create_app(execution_dependencies=second)).post(
        "/api/v1/runs", json=request_payload()
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert len(first_small.calls) == 1
    assert len(second_small.calls) == 1


def test_disabled_semantic_cache_bypasses_dependency_completely() -> None:
    """Do not call a configured cache when the typed module flag is disabled."""
    configured, small, _, _ = dependencies(0.93)
    cache = FakeSemanticCache((cache_candidate(),))
    configured = replace(configured, semantic_cache=cache)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(request_profile=cache_eligible_profile()),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["semantic_cache"]["outcome"] == "DISABLED_BYPASSED"
    assert body["semantic_cache"]["lookup_latency_ms"] == 0
    assert (
        PlannerReasonCode.SEMANTIC_CACHE_DISABLED
        in (body["execution_plan"]["reason_codes"])
    )
    assert all(step["step_type"] != "SEMANTIC_CACHE" for step in body["steps"])
    assert cache.calls == ()
    assert len(small.calls) == 1


def test_cache_hit_returns_exact_bound_output_and_preserves_source_evidence() -> None:
    """Reuse one accepted match without model, reducer, or current evaluator calls."""
    original_context = "Incident ARC-9 resolved.\nIncident ARC-9 resolved."
    configured, small, strong, evaluator = dependencies(0.93)
    source_payload = request_payload(
        context=original_context,
        request_profile={
            "task_type": "SUMMARIZATION",
            "complexity": "LOW",
            "input_tokens": 4_000,
            "risk_tier": "LOW",
            "cache_eligible": True,
            "has_large_context": True,
        },
    )
    cache = FakeSemanticCache((cache_candidate(source_payload=source_payload),))
    reducer = FakeContextReducer((reduction_result(original_context),))
    counter = RecordingTokenCounter(RegexTokenCounter())
    configured = replace(
        with_semantic_cache(configured, cache, context_reduction_enabled=True),
        context_reducer=reducer,
        token_counter=counter,
        context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=source_payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["execution_plan"]["human_readable_name"] == "Cached Result"
    assert body["final_output"] == "exact cached output"
    assert body["contract_met"] is True
    assert body["semantic_cache"]["outcome"] == SemanticCacheOutcome.REUSED
    assert body["semantic_cache"]["source_run_id"] == "run-source-cache-1"
    assert body["semantic_cache"]["similarity"] == 0.97
    assert body["semantic_cache"]["prior_evaluation"] == (
        cache_candidate().prior_evaluation.model_dump(mode="json")
    )
    assert body["semantic_cache"]["prior_evaluation"]["threshold"] == 0.80
    assert body["semantic_cache"]["prior_evaluation"]["metadata"] == {
        "source_run_id": "run-source-cache-1"
    }
    assert body["evaluations"] == []
    assert body["final_evaluation"] is None
    assert body["model_usages"] == []
    assert body["escalated"] is False
    assert body["total_input_tokens"] == 0
    assert body["total_output_tokens"] == 0
    assert body["total_tokens"] == 0
    assert body["total_calculated_cost"] is None
    assert [step["step_type"] for step in body["steps"]] == [
        "SEMANTIC_CACHE",
        "RETURN",
    ]
    assert body["steps"][0]["semantic_cache"] == body["semantic_cache"]
    assert body["latency_ms"] >= body["semantic_cache"]["lookup_latency_ms"]
    assert len(cache.calls) == 1
    assert small.calls == ()
    assert strong.calls == ()
    assert evaluator.calls == ()
    assert reducer.calls == ()
    assert counter.calls == ()


@pytest.mark.parametrize(
    "request_update",
    [
        {"input_text": "Explain the incident"},
        {"context": "Different incident context"},
        {"reference_output": "Incompatible reference"},
        {"criteria": []},
        {"criteria": ["Preserve the outcome", "The answer must be JSON."]},
        {"criteria": ["Use JSON", "Preserve the outcome"]},
        {"metadata": {"scenario": "different-audience"}},
    ],
)
def test_cache_candidate_for_different_complete_request_is_rejected(
    request_update: dict[str, object],
) -> None:
    """Reject a candidate assessed for any materially different request fact."""
    configured, small, strong, evaluator = dependencies(0.93)
    cache = FakeSemanticCache((cache_candidate(),))
    configured = with_semantic_cache(configured, cache)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            request_profile=cache_eligible_profile(),
            **request_update,
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["semantic_cache"]["outcome"] == SemanticCacheOutcome.MATCH_REJECTED
    assert body["semantic_cache"]["planner_reason_code"] == (
        PlannerReasonCode.CACHE_REQUEST_BINDING_MISMATCH
    )
    assert body["final_output"] != "exact cached output"
    assert len(cache.calls) == 1
    assert len(small.calls) == 1
    assert strong.calls == ()
    assert len(evaluator.calls) == 1


def test_cache_miss_preserves_small_first_execution() -> None:
    """Continue through the unchanged small-first path after a truthful miss."""
    configured, small, strong, evaluator = dependencies(0.93)
    cache = FakeSemanticCache((None,))
    configured = with_semantic_cache(configured, cache)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(request_profile=cache_eligible_profile()),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["semantic_cache"]["outcome"] == SemanticCacheOutcome.MISS
    assert body["semantic_cache"]["source_run_id"] is None
    assert body["execution_plan"]["model_policy"] == "SMALL_FIRST_WITH_FALLBACK"
    assert body["steps"][0]["event_codes"] == [ExecutionEventCode.CACHE_MISS]
    assert len(cache.calls) == 1
    assert len(small.calls) == 1
    assert strong.calls == ()
    assert len(evaluator.calls) == 1


def test_cache_miss_preserves_strong_direct_execution() -> None:
    """Continue through the unchanged strong-direct path after a truthful miss."""
    configured, small, strong, evaluator = dependencies(0.93)
    cache = FakeSemanticCache((None,))
    configured = with_semantic_cache(configured, cache)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            request_profile={
                "task_type": "GENERAL_REASONING",
                "complexity": "HIGH",
                "input_tokens": 100,
                "risk_tier": "LOW",
                "cache_eligible": True,
                "has_large_context": False,
            }
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["semantic_cache"]["outcome"] == SemanticCacheOutcome.MISS
    assert body["execution_plan"]["model_policy"] == "STRONG_DIRECT"
    assert small.calls == ()
    assert len(strong.calls) == 1
    assert len(evaluator.calls) == 1


def test_cache_miss_still_executes_selected_context_reduction() -> None:
    """Run the selected reducer after the leading successful cache lookup miss."""
    original_context = "Incident ARC-9 resolved.\nIncident ARC-9 resolved."
    configured, small, _, _ = dependencies(0.93)
    cache = FakeSemanticCache((None,))
    reducer = FakeContextReducer((reduction_result(original_context),))
    configured = replace(
        with_semantic_cache(configured, cache, context_reduction_enabled=True),
        context_reducer=reducer,
        token_counter=RegexTokenCounter(),
        context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            context=original_context,
            request_profile={
                "task_type": "SUMMARIZATION",
                "complexity": "LOW",
                "input_tokens": 4_000,
                "risk_tier": "LOW",
                "cache_eligible": True,
                "has_large_context": True,
            },
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert [step["step_type"] for step in body["steps"][:2]] == [
        "SEMANTIC_CACHE",
        "CONTEXT_REDUCTION",
    ]
    assert body["execution_plan"]["context_policy"] == "REDUCE"
    assert len(reducer.calls) == 1
    assert len(small.calls) == 1


def test_rejected_cache_match_preserves_source_facts_and_planner_reason() -> None:
    """Keep candidate evidence while Planner V1 continues normal execution."""
    configured, small, _, evaluator = dependencies(0.93)
    rejected = cache_candidate(similarity=0.94)
    cache = FakeSemanticCache((rejected,))
    configured = with_semantic_cache(configured, cache)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(request_profile=cache_eligible_profile()),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["semantic_cache"]["outcome"] == "MATCH_REJECTED"
    assert body["semantic_cache"]["source_run_id"] == rejected.source_run_id
    assert body["semantic_cache"]["similarity"] == 0.94
    assert body["semantic_cache"]["planner_reason_code"] == (
        PlannerReasonCode.CACHE_SIMILARITY_BELOW_THRESHOLD
    )
    assert body["execution_plan"]["cache_candidate"] is None
    assert len(small.calls) == 1
    assert len(evaluator.calls) == 1

    body["semantic_cache"]["source_run_id"] = "forged-source"
    body["semantic_cache"]["similarity"] = 0.01
    body["steps"][0]["semantic_cache"] = body["semantic_cache"]
    for computed_field in (
        "total_input_tokens",
        "total_output_tokens",
        "total_tokens",
        "total_calculated_cost",
        "total_cost_provenance",
    ):
        body.pop(computed_field)
    with pytest.raises(ValidationError, match="candidate assessment"):
        RunResult.model_validate(body)


def test_invalid_cache_provider_value_fails_closed_to_model_execution() -> None:
    """Normalize cache-adapter values before they cross the lookup boundary."""
    configured, small, _, evaluator = dependencies(0.93)
    supplied = cache_candidate()
    evaluation_values = {
        field_name: getattr(supplied.prior_evaluation, field_name)
        for field_name in type(supplied.prior_evaluation).model_fields
    }
    evaluation_values["reasons"] = ()
    invalid_evaluation = EvaluationResult.model_construct(**evaluation_values)
    candidate_values = {
        field_name: getattr(supplied, field_name)
        for field_name in type(supplied).model_fields
    }
    candidate_values["prior_evaluation"] = invalid_evaluation
    invalid_candidate = CacheCandidate.model_construct(**candidate_values)
    configured = with_semantic_cache(
        configured,
        UncheckedSemanticCache(invalid_candidate),
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(request_profile=cache_eligible_profile()),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["semantic_cache"]["outcome"] == "LOOKUP_FAILED"
    assert body["semantic_cache"]["source_run_id"] is None
    assert len(small.calls) == 1
    assert len(evaluator.calls) == 1


def test_run_endpoint_cache_hit_reports_priced_embedding_usage() -> None:
    """A cache hit must report the paid embedding tokens and cost truthfully."""
    configured, small, _, evaluator = dependencies(0.93)
    payload = request_payload(request_profile=cache_eligible_profile())
    candidate = cache_candidate(source_payload=payload)
    usage = EmbeddingUsage(
        run_id="run-api-1",
        provider="fake-embed",
        deployment="optima-embed",
        embedding_profile="profile-hash",
        input_tokens=8,
        latency_ms=1,
    )
    cache = FakeSemanticCache(
        (
            SemanticCacheLookupResult(
                candidate=candidate,
                embedding_attempt=EmbeddingAttempt(
                    invoked=True, outbound_attempted=True, usage=usage
                ),
            ),
        )
    )
    calculator = CostCalculator(
        PriceCatalog(
            version="api-test-v1",
            currency="TEST",
            entries=(
                PriceCatalogEntry(
                    provider="fake-embed",
                    deployment="optima-embed",
                    input_rate_per_million_tokens=Decimal("100"),
                    output_rate_per_million_tokens=Decimal("0"),
                ),
            ),
        )
    )
    configured = replace(
        with_semantic_cache(configured, cache),
        cost_calculator=calculator,
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["semantic_cache"]["outcome"] == "REUSED"
    embedding = body["semantic_cache"]["embedding_attempt"]["usage"]
    assert body["semantic_cache"]["embedding_attempt"]["outbound_attempted"] is True
    assert embedding["input_tokens"] == 8
    assert Decimal(embedding["calculated_cost"]) == Decimal("0.0008")
    assert body["model_usages"] == []
    assert body["total_tokens"] == 8
    assert Decimal(body["total_calculated_cost"]) == Decimal("0.0008")
    assert len(small.calls) == 0
    assert len(evaluator.calls) == 0


def test_fake_cache_does_not_invoke_candidate_controlled_detachment() -> None:
    """Return the configured cache value rather than a subclass substitution."""
    configured, small, strong, evaluator = dependencies(0.93)
    supplied = SubstitutingCacheCandidate.model_validate(
        cache_candidate().model_dump(mode="python")
    )
    configured = with_semantic_cache(
        configured,
        FakeSemanticCache((supplied,)),
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(request_profile=cache_eligible_profile()),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["semantic_cache"]["outcome"] == "REUSED"
    assert body["final_output"] == "exact cached output"
    assert len(small.calls) == 0
    assert len(strong.calls) == 0
    assert len(evaluator.calls) == 0


@pytest.mark.parametrize(
    ("error", "outcome", "status_value", "event"),
    [
        (
            RuntimeError("cache unavailable"),
            SemanticCacheOutcome.LOOKUP_FAILED,
            "FAILED",
            ExecutionEventCode.CACHE_LOOKUP_FAILED,
        ),
        (
            TimeoutError("cache timed out"),
            SemanticCacheOutcome.LOOKUP_TIMED_OUT,
            "TIMED_OUT",
            ExecutionEventCode.CACHE_LOOKUP_TIMED_OUT,
        ),
    ],
)
def test_cache_operational_failure_falls_back_without_claiming_miss(
    error: Exception,
    outcome: SemanticCacheOutcome,
    status_value: str,
    event: ExecutionEventCode,
) -> None:
    """Record optional-cache failure distinctly and complete normal execution."""
    configured, small, _, evaluator = dependencies(0.93)
    cache = FakeSemanticCache((error,))
    configured = with_semantic_cache(configured, cache)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(request_profile=cache_eligible_profile()),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["semantic_cache"]["outcome"] == outcome
    assert body["steps"][0]["status"] == status_value
    assert body["steps"][0]["event_codes"] == [event]
    assert body["semantic_cache"]["source_run_id"] is None
    assert body["semantic_cache"]["similarity"] is None
    assert body["semantic_cache"]["error"] == f"Semantic cache {type(error).__name__}"
    assert len(small.calls) == 1
    assert len(evaluator.calls) == 1


def test_enabled_semantic_cache_without_dependency_fails_before_models() -> None:
    """Expose missing enabled composition as a structural API failure."""
    configured, small, strong, evaluator = dependencies(0.93)
    configured = with_semantic_cache(configured, None)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(request_profile=cache_eligible_profile()),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SEMANTIC_CACHE_NOT_CONFIGURED"
    assert small.calls == ()
    assert strong.calls == ()
    assert evaluator.calls == ()
