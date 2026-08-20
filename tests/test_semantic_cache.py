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
from optima.domain.cache import CacheCandidate
from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    ExecutionPlan,
    PlannerDecisionEvidence,
    PlannerModuleStates,
    PlannerReasonCode,
    SemanticCacheEvidence,
    SemanticCacheOutcome,
)
from optima.domain.quality_contract import (
    OptimizationMode,
    QualityContract,
    QualityProfile,
    RiskTier,
)
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


def candidate() -> CacheCandidate:
    """Build one complete resolved cache candidate."""
    return CacheCandidate(
        source_run_id="run-source-1",
        output_text="Previously accepted output",
        similarity=1.0,
        prior_evaluation=evaluation(),
        contract_compatible=True,
        safe_to_reuse=True,
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
    )


def test_in_memory_cache_returns_exact_detached_match_and_truthful_miss() -> None:
    """Resolve exact keys deterministically without changing source evidence."""
    cache = InMemorySemanticCache(
        (
            InMemoryCacheEntry(
                input_text="Summarize incident ARC-9",
                context="Incident ARC-9 is resolved.",
                candidate=candidate(),
            ),
        )
    )

    hit = asyncio.run(cache.lookup(lookup_request()))
    miss = asyncio.run(cache.lookup(lookup_request(input_text="Different request")))

    assert hit == candidate()
    assert hit is not candidate()
    assert hit is not None
    assert hit.prior_evaluation.threshold == 0.80
    assert hit.prior_evaluation.metadata == {"source_run": "run-source-1"}
    assert miss is None
    assert [call.input_text for call in cache.calls] == [
        "Summarize incident ARC-9",
        "Different request",
    ]


def test_fake_cache_records_calls_and_propagates_operational_failure() -> None:
    """Keep fake errors observable without converting them to cache misses."""
    cache = FakeSemanticCache((candidate(), TimeoutError("cache timeout")))

    assert asyncio.run(cache.lookup(lookup_request())) == candidate()
    with pytest.raises(TimeoutError, match="cache timeout"):
        asyncio.run(cache.lookup(lookup_request(input_text="Second request")))

    assert len(cache.calls) == 2


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
    plan = ExecutionPlan(
        cache_policy=CachePolicy.USE_CACHED_RESULT,
        context_policy=ContextPolicy.NOT_APPLICABLE,
        verification_required=False,
        optimization_mode=OptimizationMode.COST,
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
    )
    substituted = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.REUSED,
        lookup_latency_ms=2,
        planner_reason_code=PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        source_run_id="run-other-source",
        similarity=bound.similarity,
        prior_evaluation=bound.prior_evaluation,
    )

    with pytest.raises(ValidationError, match="must match the bound candidate"):
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
    plan = ExecutionPlan(
        cache_policy=CachePolicy.USE_CACHED_RESULT,
        context_policy=ContextPolicy.NOT_APPLICABLE,
        verification_required=False,
        optimization_mode=OptimizationMode.COST,
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
    )
    evidence = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.REUSED,
        lookup_latency_ms=2,
        planner_reason_code=PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        source_run_id=below_threshold.source_run_id,
        similarity=below_threshold.similarity,
        prior_evaluation=below_threshold.prior_evaluation,
    )

    with pytest.raises(ValidationError, match="does not satisfy reuse gates"):
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
