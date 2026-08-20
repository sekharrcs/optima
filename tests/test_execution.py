"""Tests for pre-execution plans and actual execution-step facts."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from optima.context import ContextPreservationEvidence
from optima.domain.cache import CacheCandidate
from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    ContextReductionEvidence,
    ContextReductionOutcome,
    ContextSource,
    ExecutionPlan,
    ExecutionStatus,
    ExecutionStep,
    ExecutionStepType,
    ModelPolicy,
    ModelRole,
    PlannerDecisionEvidence,
    PlannerModuleStates,
    PlannerReasonCode,
)
from optima.domain.quality_contract import OptimizationMode, RiskTier


def decision_evidence(
    *,
    base_model_policy: ModelPolicy | None,
    final_model_policy: ModelPolicy | None,
    cache_candidate_assessed: bool = False,
) -> PlannerDecisionEvidence:
    """Build valid typed planner evidence for execution-plan tests."""
    return PlannerDecisionEvidence(
        profile_risk_tier=RiskTier.LOW,
        contract_risk_tier=RiskTier.MEDIUM,
        effective_risk_tier=RiskTier.MEDIUM,
        module_states=PlannerModuleStates(
            semantic_cache_enabled=True,
            context_reduction_enabled=True,
            historical_policy_enabled=True,
            foundry_router_comparator_enabled=False,
        ),
        cache_candidate_assessed=cache_candidate_assessed,
        base_model_policy=base_model_policy,
        final_model_policy=final_model_policy,
    )


def small_first_plan(**updates: object) -> ExecutionPlan:
    """Build a valid small-first plan with optional test overrides."""
    values: dict[str, object] = {
        "cache_policy": CachePolicy.SKIP,
        "context_policy": ContextPolicy.KEEP_ORIGINAL,
        "model_policy": ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
        "initial_model_role": ModelRole.SMALL,
        "verification_required": True,
        "escalation_model_role": ModelRole.STRONG,
        "optimization_mode": OptimizationMode.COST,
        "reason_codes": (
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
            PlannerReasonCode.SMALL_FIRST_SELECTED,
        ),
        "human_readable_name": "Small -> Verify -> Escalate if needed",
        "decision_evidence": decision_evidence(
            base_model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            final_model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
        ),
    }
    values.update(updates)
    return ExecutionPlan.model_validate(values)


def strong_direct_plan(**updates: object) -> ExecutionPlan:
    """Build a valid strong-direct plan with optional test overrides."""
    values: dict[str, object] = {
        "cache_policy": CachePolicy.SKIP,
        "context_policy": ContextPolicy.REDUCE,
        "model_policy": ModelPolicy.STRONG_DIRECT,
        "initial_model_role": ModelRole.STRONG,
        "verification_required": True,
        "escalation_model_role": None,
        "optimization_mode": OptimizationMode.BALANCED,
        "reason_codes": (
            PlannerReasonCode.OPTIMIZATION_MODE_BALANCED,
            PlannerReasonCode.STRONG_MODEL_REQUIRED,
        ),
        "human_readable_name": "Context Reduce -> Strong -> Verify",
        "decision_evidence": decision_evidence(
            base_model_policy=ModelPolicy.STRONG_DIRECT,
            final_model_policy=ModelPolicy.STRONG_DIRECT,
        ),
    }
    values.update(updates)
    return ExecutionPlan.model_validate(values)


def semantic_cache_plan(**updates: object) -> ExecutionPlan:
    """Build a valid semantic-cache plan with optional test overrides."""
    values: dict[str, object] = {
        "cache_policy": CachePolicy.USE_CACHED_RESULT,
        "context_policy": ContextPolicy.NOT_APPLICABLE,
        "model_policy": None,
        "initial_model_role": None,
        "verification_required": False,
        "escalation_model_role": None,
        "optimization_mode": OptimizationMode.QUALITY,
        "reason_codes": (
            PlannerReasonCode.OPTIMIZATION_MODE_QUALITY,
            PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        ),
        "human_readable_name": "Semantic Cache Hit",
        "decision_evidence": decision_evidence(
            base_model_policy=None,
            final_model_policy=None,
            cache_candidate_assessed=True,
        ),
        "cache_candidate": CacheCandidate(
            source_run_id="run-source-1",
            output_text="cached output",
            similarity=0.99,
            prior_evaluation=EvaluationResult(
                evaluator_type="deterministic",
                evaluator_valid=True,
                score=0.95,
                threshold=0.90,
                mandatory_checks_passed=True,
                passed=True,
                reasons=("Accepted",),
            ),
            contract_compatible=True,
            safe_to_reuse=True,
        ),
    }
    values.update(updates)
    return ExecutionPlan.model_validate(values)


def test_small_first_plan_carries_required_strong_fallback() -> None:
    """Represent the complete V1 small-first-with-fallback structure."""
    plan = small_first_plan(expected_quality_score=0.91, expected_cost=Decimal("0.02"))

    assert plan.initial_model_role is ModelRole.SMALL
    assert plan.verification_required is True
    assert plan.escalation_model_role is ModelRole.STRONG
    assert plan.expected_cost == Decimal("0.02")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_model_role", ModelRole.STRONG),
        ("verification_required", False),
        ("escalation_model_role", None),
        ("escalation_model_role", ModelRole.SMALL),
    ],
)
def test_small_first_plan_rejects_missing_structural_guarantees(
    field: str,
    value: object,
) -> None:
    """Reject small-first plans that cannot perform verified strong fallback."""
    with pytest.raises(ValidationError):
        small_first_plan(**{field: value})


def test_strong_direct_plan_has_no_fallback() -> None:
    """Represent direct strong execution with mandatory verification."""
    plan = strong_direct_plan()

    assert plan.initial_model_role is ModelRole.STRONG
    assert plan.verification_required is True
    assert plan.escalation_model_role is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_model_role", ModelRole.SMALL),
        ("verification_required", False),
        ("escalation_model_role", ModelRole.STRONG),
    ],
)
def test_strong_direct_plan_rejects_invalid_shape(field: str, value: object) -> None:
    """Reject strong-direct plans with small-first semantics."""
    with pytest.raises(ValidationError):
        strong_direct_plan(**{field: value})


def test_semantic_cache_plan_bypasses_context_and_models() -> None:
    """Represent safe cache reuse without pre-execution model facts."""
    plan = semantic_cache_plan()

    assert plan.context_policy is ContextPolicy.NOT_APPLICABLE
    assert plan.model_policy is None
    assert plan.initial_model_role is None
    assert plan.verification_required is False
    assert plan.cache_candidate is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("context_policy", ContextPolicy.KEEP_ORIGINAL),
        ("model_policy", ModelPolicy.STRONG_DIRECT),
        ("initial_model_role", ModelRole.STRONG),
        ("verification_required", True),
        ("escalation_model_role", ModelRole.STRONG),
        ("reason_codes", (PlannerReasonCode.OPTIMIZATION_MODE_QUALITY,)),
        ("cache_candidate", None),
    ],
)
def test_semantic_cache_plan_rejects_model_execution_shape(
    field: str,
    value: object,
) -> None:
    """Reject cache-hit plans that contain model or context execution."""
    with pytest.raises(ValidationError):
        semantic_cache_plan(**{field: value})


@pytest.mark.parametrize(
    ("mode", "mode_code"),
    [
        (OptimizationMode.COST, PlannerReasonCode.OPTIMIZATION_MODE_COST),
        (OptimizationMode.BALANCED, PlannerReasonCode.OPTIMIZATION_MODE_BALANCED),
        (OptimizationMode.QUALITY, PlannerReasonCode.OPTIMIZATION_MODE_QUALITY),
    ],
)
def test_plan_requires_exact_matching_optimization_mode_reason(
    mode: OptimizationMode,
    mode_code: PlannerReasonCode,
) -> None:
    """Carry exactly one reason code matching the selected mode."""
    plan = small_first_plan(
        optimization_mode=mode,
        reason_codes=(mode_code, PlannerReasonCode.SMALL_FIRST_SELECTED),
    )

    assert mode_code in plan.reason_codes


@pytest.mark.parametrize(
    "reason_codes",
    [
        (PlannerReasonCode.SMALL_FIRST_SELECTED,),
        (
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
            PlannerReasonCode.OPTIMIZATION_MODE_BALANCED,
        ),
        (
            PlannerReasonCode.OPTIMIZATION_MODE_BALANCED,
            PlannerReasonCode.SMALL_FIRST_SELECTED,
        ),
        (
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
        ),
    ],
)
def test_plan_rejects_invalid_reason_code_sets(
    reason_codes: tuple[PlannerReasonCode, ...],
) -> None:
    """Reject missing, mismatched, multiple, or duplicate mode reasons."""
    with pytest.raises(ValidationError):
        small_first_plan(reason_codes=reason_codes)


def test_execution_plan_rejects_measured_runtime_fields() -> None:
    """Keep actual quality, usage, and latency outside pre-execution plans."""
    with pytest.raises(ValidationError):
        small_first_plan(actual_quality_score=0.9)


def test_model_plan_rejects_final_policy_evidence_mismatch() -> None:
    """Keep the selected model policy aligned with typed decision evidence."""
    with pytest.raises(ValidationError, match="final evidence policy"):
        small_first_plan(
            decision_evidence=decision_evidence(
                base_model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
                final_model_policy=ModelPolicy.STRONG_DIRECT,
            )
        )


def test_cache_plan_rejects_unassessed_candidate_evidence() -> None:
    """Require evidence that an accepted cache candidate was assessed."""
    with pytest.raises(ValidationError, match="candidate assessment"):
        semantic_cache_plan(
            decision_evidence=decision_evidence(
                base_model_policy=None,
                final_model_policy=None,
                cache_candidate_assessed=False,
            )
        )


def test_model_plan_rejects_resolved_cache_payload() -> None:
    """Prevent a rejected candidate from leaking into model execution."""
    with pytest.raises(ValidationError, match="cannot carry a cache candidate"):
        small_first_plan(cache_candidate=semantic_cache_plan().cache_candidate)


@pytest.mark.parametrize("expected_cost", [Decimal("-0.01"), Decimal("NaN")])
def test_execution_plan_rejects_invalid_estimated_cost(
    expected_cost: Decimal,
) -> None:
    """Reject negative or non-finite pre-execution monetary estimates."""
    with pytest.raises(ValidationError):
        small_first_plan(expected_cost=expected_cost)


def test_execution_step_preserves_actual_structured_facts() -> None:
    """Represent measured applied reduction through canonical typed evidence."""
    step = ExecutionStep(
        sequence=1,
        step_type=ExecutionStepType.CONTEXT_REDUCTION,
        status=ExecutionStatus.SUCCEEDED,
        latency_ms=12,
        context_reduction=ContextReductionEvidence(
            outcome=ContextReductionOutcome.APPLIED,
            original_token_count=9000,
            effective_token_count=3000,
            reducer_name="extractive-v1",
            method="EXTRACTIVE",
            token_counter_name="counter-v1",
            context_source=ContextSource.REDUCED,
            preservation=ContextPreservationEvidence(
                source_order_preserved=True,
                original_segment_count=2,
                retained_segment_indexes=(0,),
                removed_duplicate_count=0,
                removed_irrelevant_count=1,
            ),
        ),
    )

    assert step.context_reduction is not None
    assert step.context_reduction.original_token_count == 9000
    assert step.context_reduction.effective_token_count == 3000
    assert step.error is None


@pytest.mark.parametrize("status", [ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT])
def test_failed_execution_step_requires_error(status: ExecutionStatus) -> None:
    """Require an error when a step fails or times out."""
    with pytest.raises(ValidationError):
        ExecutionStep(
            sequence=0,
            step_type=ExecutionStepType.MODEL_CALL,
            status=status,
            latency_ms=10,
        )


def test_successful_execution_step_rejects_error() -> None:
    """Do not attach an error to a successful execution fact."""
    with pytest.raises(ValidationError):
        ExecutionStep(
            sequence=0,
            step_type=ExecutionStepType.RETURN,
            status=ExecutionStatus.SUCCEEDED,
            latency_ms=0,
            error="unexpected",
        )
