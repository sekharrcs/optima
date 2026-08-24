"""Tests for the provider-independent semantic-cache lookup boundary."""

import asyncio

import pytest
from pydantic import ValidationError

from optima.cache import (
    FakeSemanticCache,
    InMemoryCacheEntry,
    InMemorySemanticCache,
    SemanticCacheLookupRequest,
)
from optima.domain.cache import CacheCandidate, CacheCandidateAssessment
from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    ExecutionEventCode,
    ExecutionPlan,
    ExecutionStatus,
    ExecutionStep,
    ExecutionStepType,
    ModelPolicy,
    ModelRole,
    PlannerDecisionEvidence,
    PlannerModuleStates,
    PlannerReasonCode,
    SemanticCacheEvidence,
    SemanticCacheOutcome,
)
from optima.domain.immutable import FrozenJsonArray, FrozenJsonObject
from optima.domain.quality_contract import (
    OptimizationMode,
    QualityContract,
    QualityProfile,
    RiskTier,
)
from optima.domain.request_binding import RequestBinding, build_request_binding
from optima.domain.request_profile import Complexity, RequestProfile, TaskType
from optima.execution import ExecutionRequest


def evaluation() -> EvaluationResult:
    """Build immutable source-run quality evidence."""
    return EvaluationResult(
        evaluator_type="source-deterministic",
        evaluator_valid=True,
        score=0.96,
        threshold=0.80,
        mandatory_checks_passed=True,
        passed=True,
        reasons=("Source contract passed",),
        metadata={"source_run": "run-source-1"},
    )


def candidate(**updates: object) -> CacheCandidate:
    """Build one complete resolved cache candidate."""
    values: dict[str, object] = {
        "source_run_id": "run-source-1",
        "output_text": "Previously accepted output",
        "request_binding": request_binding(),
        "similarity": 1.0,
        "prior_evaluation": evaluation(),
        "contract_compatible": True,
        "safe_to_reuse": True,
    }
    values.update(updates)
    return CacheCandidate.model_validate(values)


def request_binding(*, input_text: str = "Summarize incident ARC-9") -> RequestBinding:
    """Build the complete binding shared by default test fixtures."""
    return build_request_binding(
        input_text=input_text,
        context="Incident ARC-9 is resolved.",
        reference_output=None,
        criteria=(),
        metadata={},
        task_type=TaskType.SUMMARIZATION,
        complexity=Complexity.LOW,
    )


def lookup_request(
    *, input_text: str = "Summarize incident ARC-9"
) -> SemanticCacheLookupRequest:
    """Build one strict lookup request."""
    return SemanticCacheLookupRequest(
        run_id="run-current-1",
        input_text=input_text,
        context="Incident ARC-9 is resolved.",
        quality_contract=QualityContract(
            quality_profile=QualityProfile.HIGH,
            minimum_quality_score=0.90,
            optimization_mode=OptimizationMode.COST,
            risk_tier=RiskTier.LOW,
        ),
        request_profile=RequestProfile(
            task_type=TaskType.SUMMARIZATION,
            complexity=Complexity.LOW,
            input_tokens=100,
            risk_tier=RiskTier.LOW,
            cache_eligible=True,
            has_large_context=False,
        ),
        request_binding=request_binding(input_text=input_text),
    )


def test_in_memory_cache_returns_exact_detached_match_and_truthful_miss() -> None:
    """Resolve exact keys deterministically without changing source evidence."""
    cache = InMemorySemanticCache(
        (
            InMemoryCacheEntry(
                request_binding=request_binding(),
                candidate=candidate(),
            ),
        )
    )

    hit = asyncio.run(cache.lookup(lookup_request()))
    miss = asyncio.run(cache.lookup(lookup_request(input_text="Different request")))

    assert hit.candidate == candidate()
    assert hit.candidate is not candidate()
    assert hit.candidate is not None
    assert hit.candidate.prior_evaluation.threshold == 0.80
    assert hit.candidate.prior_evaluation.metadata == {"source_run": "run-source-1"}
    assert hit.embedding_usage is None
    assert miss.candidate is None
    assert [call.input_text for call in cache.calls] == [
        "Summarize incident ARC-9",
        "Different request",
    ]


def test_in_memory_cache_entry_rejects_candidate_bound_to_another_request() -> None:
    """Reject local exact-match entries whose key and candidate disagree."""
    with pytest.raises(ValueError, match="entry binding"):
        InMemoryCacheEntry(
            request_binding=request_binding(input_text="Different request"),
            candidate=candidate(),
        )


def test_fake_cache_records_calls_and_propagates_operational_failure() -> None:
    """Keep fake errors observable without converting them to cache misses."""
    cache = FakeSemanticCache((candidate(), TimeoutError("cache timeout")))

    assert asyncio.run(cache.lookup(lookup_request())).candidate == candidate()
    with pytest.raises(TimeoutError, match="cache timeout"):
        asyncio.run(cache.lookup(lookup_request(input_text="Second request")))

    assert len(cache.calls) == 2


def test_evaluation_metadata_is_recursively_immutable_and_detached() -> None:
    """Reject nested mutations and detach evidence from caller-owned containers."""
    caller_nested = {"value": 1}
    caller_items = [{"name": "original"}]
    caller_metadata = {
        "nested": caller_nested,
        "items": caller_items,
    }
    values = evaluation().model_dump(mode="python")
    values["metadata"] = caller_metadata
    result = EvaluationResult.model_validate(values)
    caller_nested["value"] = 2
    caller_items[0]["name"] = "changed"

    assert result.metadata == {
        "nested": {"value": 1},
        "items": [{"name": "original"}],
    }
    nested = result.metadata["nested"]
    items = result.metadata["items"]
    assert isinstance(nested, FrozenJsonObject)
    assert isinstance(items, FrozenJsonArray)
    assert isinstance(items[0], FrozenJsonObject)
    with pytest.raises(TypeError):
        nested["value"] = 3
    with pytest.raises(TypeError):
        items[0]["name"] = "mutated"
    with pytest.raises(AttributeError):
        items.append({"name": "appended"})
    with pytest.raises(TypeError):
        dict.__setitem__(nested, "value", 4)
    with pytest.raises(TypeError):
        dict.update(nested, {"value": 5})
    with pytest.raises(TypeError):
        list.append(items, {"name": "base-appended"})
    with pytest.raises(TypeError):
        list.__setitem__(items, 0, {"name": "base-mutated"})
    with pytest.raises(AttributeError):
        items._values = ({"name": "slot-mutated"},)

    assert EvaluationResult.model_validate(result.model_dump(mode="json")) == result


def test_evaluation_model_copy_detaches_and_freezes_nested_metadata() -> None:
    """Revalidate model-copy updates instead of trusting mutable nested values."""
    caller_nested = {"value": 1}
    caller_items = ["original"]
    caller_metadata = {"nested": caller_nested, "items": caller_items}

    result = evaluation().model_copy(update={"metadata": caller_metadata})
    caller_nested["value"] = 2
    caller_items.append("changed")

    assert result.metadata == {
        "nested": {"value": 1},
        "items": ["original"],
    }
    copied_nested = result.metadata["nested"]
    copied_items = result.metadata["items"]
    assert isinstance(copied_nested, FrozenJsonObject)
    assert isinstance(copied_items, FrozenJsonArray)
    with pytest.raises(TypeError):
        copied_nested["value"] = 3
    with pytest.raises(AttributeError):
        copied_items.append("mutated")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_run_id", ""),
        ("output_text", ""),
        ("similarity", -0.01),
        ("similarity", 1.01),
        ("similarity", float("nan")),
    ],
)
def test_cache_candidate_rejects_impossible_resolved_values(
    field: str,
    value: object,
) -> None:
    """Reject malformed identity, payload, and similarity values at the boundary."""
    values = candidate().model_dump(mode="python")
    values[field] = value

    with pytest.raises(ValidationError):
        CacheCandidate.model_validate(values)


@pytest.mark.parametrize(
    "updates",
    [
        {"outcome": SemanticCacheOutcome.MISS, "source_run_id": "run-source-1"},
        {"outcome": SemanticCacheOutcome.LOOKUP_FAILED, "error": None},
        {
            "outcome": SemanticCacheOutcome.REUSED,
            "planner_reason_code": PlannerReasonCode.CACHE_REUSE_UNSAFE,
        },
        {
            "outcome": SemanticCacheOutcome.MATCH_REJECTED,
            "planner_reason_code": PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        },
    ],
)
def test_semantic_cache_evidence_rejects_impossible_combinations(
    updates: dict[str, object],
) -> None:
    """Reject contradictory miss, failure, reuse, and rejection facts."""
    values: dict[str, object] = {
        "outcome": SemanticCacheOutcome.REUSED,
        "lookup_latency_ms": 3,
        "planner_reason_code": PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        "source_run_id": candidate().source_run_id,
        "similarity": candidate().similarity,
        "prior_evaluation": candidate().prior_evaluation,
    }
    values.update(updates)

    with pytest.raises(ValidationError):
        SemanticCacheEvidence.model_validate(values)


def test_execution_request_rejects_substituted_cache_evidence() -> None:
    """Prevent source evidence from a different match replacing the bound payload."""
    bound = candidate()
    assessment = CacheCandidateAssessment.from_candidate(bound)
    plan = ExecutionPlan(
        cache_policy=CachePolicy.USE_CACHED_RESULT,
        context_policy=ContextPolicy.NOT_APPLICABLE,
        verification_required=False,
        optimization_mode=OptimizationMode.COST,
        quality_profile=QualityProfile.HIGH,
        reason_codes=(
            PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
        ),
        human_readable_name="Cached Result",
        decision_evidence=PlannerDecisionEvidence(
            profile_risk_tier=RiskTier.LOW,
            contract_risk_tier=RiskTier.LOW,
            effective_risk_tier=RiskTier.LOW,
            module_states=PlannerModuleStates(
                semantic_cache_enabled=True,
                context_reduction_enabled=False,
                historical_policy_enabled=False,
                foundry_router_comparator_enabled=False,
            ),
            cache_candidate_assessed=True,
        ),
        cache_candidate=bound,
        cache_candidate_assessment=assessment,
        request_binding=bound.request_binding,
    )
    substituted = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.REUSED,
        lookup_latency_ms=2,
        planner_reason_code=PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        source_run_id="run-other-source",
        similarity=bound.similarity,
        prior_evaluation=bound.prior_evaluation,
    )

    with pytest.raises(ValidationError, match="candidate assessment"):
        ExecutionRequest(
            run_id="run-current-1",
            correlation_id="correlation-1",
            input_text="Summarize incident ARC-9",
            context="Incident ARC-9 is resolved.",
            quality_contract=lookup_request().quality_contract,
            request_profile=lookup_request().request_profile,
            execution_plan=plan,
            semantic_cache=substituted,
        )


def test_execution_request_rejects_forged_below_threshold_cache_plan() -> None:
    """Enforce the snapshotted planner threshold for direct runtime callers."""
    below_threshold = candidate().model_copy(update={"similarity": 0.01})
    assessment = CacheCandidateAssessment.from_candidate(below_threshold)
    plan = ExecutionPlan(
        cache_policy=CachePolicy.USE_CACHED_RESULT,
        context_policy=ContextPolicy.NOT_APPLICABLE,
        verification_required=False,
        optimization_mode=OptimizationMode.COST,
        quality_profile=QualityProfile.HIGH,
        reason_codes=(
            PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
        ),
        human_readable_name="Cached Result",
        decision_evidence=PlannerDecisionEvidence(
            profile_risk_tier=RiskTier.LOW,
            contract_risk_tier=RiskTier.LOW,
            effective_risk_tier=RiskTier.LOW,
            module_states=PlannerModuleStates(
                semantic_cache_enabled=True,
                context_reduction_enabled=False,
                historical_policy_enabled=False,
                foundry_router_comparator_enabled=False,
            ),
            cache_similarity_threshold=0.95,
            cache_candidate_assessed=True,
        ),
        cache_candidate=below_threshold,
        cache_candidate_assessment=assessment,
        request_binding=below_threshold.request_binding,
    )
    evidence = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.REUSED,
        lookup_latency_ms=2,
        planner_reason_code=PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        source_run_id=below_threshold.source_run_id,
        similarity=below_threshold.similarity,
        prior_evaluation=below_threshold.prior_evaluation,
        candidate_assessment=assessment,
    )

    with pytest.raises(ValidationError, match="candidate assessment outcome"):
        ExecutionRequest(
            run_id="run-current-1",
            correlation_id="correlation-1",
            input_text="Summarize incident ARC-9",
            context="Incident ARC-9 is resolved.",
            quality_contract=lookup_request().quality_contract,
            request_profile=lookup_request().request_profile,
            execution_plan=plan,
            semantic_cache=evidence,
        )


def test_execution_request_rejects_serialized_cache_plan_rebound_to_request() -> None:
    """Prevent an accepted serialized plan from being attached to other inputs."""
    bound_binding = build_request_binding(
        input_text="Summarize incident ARC-9",
        context="Incident ARC-9 is resolved.",
        reference_output="Incident resolved",
        criteria=("Preserve the incident outcome",),
        metadata={"audience": "operations"},
        task_type=TaskType.SUMMARIZATION,
        complexity=Complexity.LOW,
    )
    bound = candidate(request_binding=bound_binding)
    assessment = CacheCandidateAssessment.from_candidate(bound)
    plan = ExecutionPlan(
        cache_policy=CachePolicy.USE_CACHED_RESULT,
        context_policy=ContextPolicy.NOT_APPLICABLE,
        verification_required=False,
        optimization_mode=OptimizationMode.COST,
        quality_profile=QualityProfile.HIGH,
        reason_codes=(
            PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
        ),
        human_readable_name="Cached Result",
        decision_evidence=PlannerDecisionEvidence(
            profile_risk_tier=RiskTier.LOW,
            contract_risk_tier=RiskTier.LOW,
            effective_risk_tier=RiskTier.LOW,
            module_states=PlannerModuleStates(
                semantic_cache_enabled=True,
                context_reduction_enabled=False,
                historical_policy_enabled=False,
                foundry_router_comparator_enabled=False,
            ),
            cache_candidate_assessed=True,
        ),
        cache_candidate=bound,
        cache_candidate_assessment=assessment,
        request_binding=bound_binding,
    )
    evidence = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.REUSED,
        lookup_latency_ms=2,
        planner_reason_code=PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        source_run_id=bound.source_run_id,
        similarity=bound.similarity,
        prior_evaluation=bound.prior_evaluation,
        candidate_assessment=assessment,
    )
    request = ExecutionRequest(
        run_id="run-current-1",
        correlation_id="correlation-1",
        input_text="Summarize incident ARC-9",
        context="Incident ARC-9 is resolved.",
        reference_output="Incident resolved",
        criteria=("Preserve the incident outcome",),
        metadata={"audience": "operations"},
        quality_contract=lookup_request().quality_contract,
        request_profile=lookup_request().request_profile,
        execution_plan=plan,
        semantic_cache=evidence,
    )
    serialized = request.model_dump(mode="json")
    serialized["input_text"] = "Unrelated request"
    serialized["context"] = "Unrelated context"

    with pytest.raises(ValidationError, match="plan request binding"):
        ExecutionRequest.model_validate(serialized)


def test_enabled_eligible_execution_request_requires_cache_outcome() -> None:
    """Prevent enabled eligible requests from silently bypassing cache lookup."""
    plan = ExecutionPlan(
        cache_policy=CachePolicy.SKIP,
        context_policy=ContextPolicy.KEEP_ORIGINAL,
        model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
        initial_model_role=ModelRole.SMALL,
        verification_required=True,
        escalation_model_role=ModelRole.STRONG,
        optimization_mode=OptimizationMode.COST,
        quality_profile=QualityProfile.HIGH,
        reason_codes=(
            PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED,
            PlannerReasonCode.SMALL_FIRST_SELECTED,
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
        ),
        human_readable_name="Small -> Verify -> Escalate if needed",
        decision_evidence=PlannerDecisionEvidence(
            profile_risk_tier=RiskTier.LOW,
            contract_risk_tier=RiskTier.LOW,
            effective_risk_tier=RiskTier.LOW,
            module_states=PlannerModuleStates(
                semantic_cache_enabled=True,
                context_reduction_enabled=False,
                historical_policy_enabled=False,
                foundry_router_comparator_enabled=False,
            ),
            cache_candidate_assessed=False,
            base_model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            final_model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
        ),
        request_binding=request_binding(),
    )

    with pytest.raises(ValidationError, match="cache outcome evidence"):
        ExecutionRequest(
            run_id="run-current-1",
            correlation_id="correlation-1",
            input_text="Summarize incident ARC-9",
            context="Incident ARC-9 is resolved.",
            quality_contract=lookup_request().quality_contract,
            request_profile=lookup_request().request_profile,
            execution_plan=plan,
            semantic_cache=None,
        )


def test_cache_step_rejects_event_codes_for_another_outcome() -> None:
    """Require the exact event-code set defined for a cache miss."""
    miss = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.MISS,
        lookup_latency_ms=2,
        planner_reason_code=PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED,
    )

    with pytest.raises(ValidationError, match="event codes"):
        ExecutionStep(
            sequence=0,
            step_type=ExecutionStepType.SEMANTIC_CACHE,
            status=ExecutionStatus.SUCCEEDED,
            latency_ms=2,
            event_codes=(
                ExecutionEventCode.CACHE_RESULT_REUSED,
                ExecutionEventCode.QUALITY_CONTRACT_MET,
            ),
            semantic_cache=miss,
        )


@pytest.mark.parametrize(
    "event_codes",
    [
        (),
        (ExecutionEventCode.CACHE_MISS,),
        (ExecutionEventCode.CACHE_LOOKUP_FAILED,),
        (ExecutionEventCode.CACHE_RESULT_REUSED,),
        (
            ExecutionEventCode.QUALITY_CONTRACT_MET,
            ExecutionEventCode.CACHE_RESULT_REUSED,
        ),
        (
            ExecutionEventCode.CACHE_RESULT_REUSED,
            ExecutionEventCode.QUALITY_CONTRACT_MET,
            ExecutionEventCode.QUALITY_CONTRACT_MET,
        ),
    ],
)
def test_reused_cache_step_requires_exact_event_codes(
    event_codes: tuple[ExecutionEventCode, ...],
) -> None:
    """Reject missing, extra, reordered, or contradictory cache-hit events."""
    bound = candidate()
    reused = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.REUSED,
        lookup_latency_ms=2,
        planner_reason_code=PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        source_run_id=bound.source_run_id,
        similarity=bound.similarity,
        prior_evaluation=bound.prior_evaluation,
    )

    with pytest.raises(ValidationError, match="event codes"):
        ExecutionStep(
            sequence=0,
            step_type=ExecutionStepType.SEMANTIC_CACHE,
            status=ExecutionStatus.SUCCEEDED,
            latency_ms=2,
            event_codes=event_codes,
            semantic_cache=reused,
        )
