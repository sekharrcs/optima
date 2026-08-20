"""API integration tests for the Slice 5 small-first vertical path."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from optima.api.app import create_app
from optima.api.dependencies import ExecutionDependencies
from optima.config import AppSettings
from optima.cost import CostCalculator, PriceCatalog, PriceCatalogEntry
from optima.domain.execution import ExecutionEventCode, ModelRole, PlannerReasonCode
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


def evidence(score: float) -> EvaluationEvidence:
    """Build evaluator-owned fake evidence."""
    return EvaluationEvidence(
        evaluator_type="fake-deterministic",
        evaluator_valid=True,
        score=score,
        metadata={"source": "api-test"},
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


def test_strong_direct_plan_returns_structured_unsupported_error() -> None:
    """Honor Planner V1 without coercing an excluded strong-direct plan."""
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

    assert response.status_code == 501
    assert response.json()["detail"] == {
        "code": "UNSUPPORTED_EXECUTION_PLAN",
        "message": "Slice 5 supports KEEP_ORIGINAL SMALL_FIRST_WITH_FALLBACK only",
        "facts": {
            "model_policy": "STRONG_DIRECT",
            "context_policy": "KEEP_ORIGINAL",
        },
    }
    assert len(small.calls) == 0
    assert len(strong.calls) == 0
    assert len(evaluator.calls) == 0


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
