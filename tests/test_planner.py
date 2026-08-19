"""Composition and invariant tests for deterministic Planner V1."""

import pytest

from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    ExecutionPlan,
    ModelPolicy,
    ModelRole,
    PlannerReasonCode,
)
from optima.domain.quality_contract import (
    OptimizationMode,
    QualityContract,
    QualityProfile,
    RiskTier,
)
from optima.domain.request_profile import Complexity, RequestProfile, TaskType
from optima.planner import (
    CacheCandidate,
    ContextReducerCapability,
    HistoricalPolicyStatistics,
    ModuleConfiguration,
    PlannerCapabilities,
    PlannerInput,
    PlanningFailure,
    PlanningFailureCode,
    select_plan,
)


def request_profile(**updates: object) -> RequestProfile:
    """Build a cache-eligible low-risk request profile."""
    values: dict[str, object] = {
        "task_type": TaskType.SUMMARIZATION,
        "complexity": Complexity.LOW,
        "input_tokens": 1_000,
        "risk_tier": RiskTier.LOW,
        "cache_eligible": True,
        "has_large_context": False,
    }
    values.update(updates)
    return RequestProfile.model_validate(values)


def quality_contract(**updates: object) -> QualityContract:
    """Build a Standard Cost Quality Contract."""
    values: dict[str, object] = {
        "quality_profile": QualityProfile.STANDARD,
        "minimum_quality_score": 0.80,
        "optimization_mode": OptimizationMode.COST,
        "risk_tier": RiskTier.LOW,
    }
    values.update(updates)
    return QualityContract.model_validate(values)


def modules(**updates: object) -> ModuleConfiguration:
    """Build enabled optional module configuration."""
    values: dict[str, object] = {
        "semantic_cache_enabled": True,
        "context_reduction_enabled": True,
        "historical_policy_enabled": True,
        "foundry_router_comparator_enabled": False,
    }
    values.update(updates)
    return ModuleConfiguration.model_validate(values)


def accepted_evaluation() -> EvaluationResult:
    """Build valid prior cache quality evidence."""
    return EvaluationResult(
        evaluator_type="deterministic",
        evaluator_valid=True,
        score=0.90,
        threshold=0.80,
        mandatory_checks_passed=True,
        passed=True,
        reasons=("Accepted",),
    )


def cache_candidate(**updates: object) -> CacheCandidate:
    """Build a safe high-confidence cache candidate."""
    values: dict[str, object] = {
        "source_run_id": "run-1",
        "similarity": 0.95,
        "prior_evaluation": accepted_evaluation(),
        "contract_compatible": True,
        "safe_to_reuse": True,
    }
    values.update(updates)
    return CacheCandidate.model_validate(values)


def planner_input(**updates: object) -> PlannerInput:
    """Build complete default planner input without a cache hit or history."""
    values: dict[str, object] = {
        "request_profile": request_profile(),
        "quality_contract": quality_contract(),
        "modules": modules(),
        "reducer_capability": ContextReducerCapability(
            available=True,
            task_safe=True,
            approved_for_critical_high_risk=False,
        ),
    }
    values.update(updates)
    return PlannerInput.model_validate(values)


def require_plan(result: ExecutionPlan | PlanningFailure) -> ExecutionPlan:
    """Narrow a planner result to a successful plan for assertions."""
    assert isinstance(result, ExecutionPlan)
    return result


def test_safe_cache_hit_short_circuits_model_and_context_planning() -> None:
    """Return only previously accepted compatible cache evidence."""
    plan = require_plan(select_plan(planner_input(cache_candidate=cache_candidate())))

    assert plan.cache_policy is CachePolicy.USE_CACHED_RESULT
    assert plan.context_policy is ContextPolicy.NOT_APPLICABLE
    assert plan.model_policy is None
    assert plan.human_readable_name == "Cached Result"
    assert plan.decision_evidence.cache_candidate_assessed is True


@pytest.mark.parametrize(
    ("candidate_updates", "reason"),
    [
        ({"similarity": 0.90}, PlannerReasonCode.CACHE_SIMILARITY_BELOW_THRESHOLD),
        ({"contract_compatible": False}, PlannerReasonCode.CACHE_CONTRACT_INCOMPATIBLE),
        ({"safe_to_reuse": False}, PlannerReasonCode.CACHE_REUSE_UNSAFE),
    ],
)
def test_rejected_cache_candidate_continues_normal_planning(
    candidate_updates: dict[str, object],
    reason: PlannerReasonCode,
) -> None:
    """Continue to context and model policies after cache rejection."""
    plan = require_plan(
        select_plan(planner_input(cache_candidate=cache_candidate(**candidate_updates)))
    )

    assert plan.cache_policy is CachePolicy.SKIP
    assert plan.model_policy is ModelPolicy.SMALL_FIRST_WITH_FALLBACK
    assert reason in plan.reason_codes
    assert plan.decision_evidence.cache_candidate_assessed is True


@pytest.mark.parametrize(
    ("profile_risk", "contract_risk", "effective"),
    [
        (RiskTier.LOW, RiskTier.HIGH, RiskTier.HIGH),
        (RiskTier.MEDIUM, RiskTier.LOW, RiskTier.MEDIUM),
        (RiskTier.HIGH, RiskTier.MEDIUM, RiskTier.HIGH),
    ],
)
def test_plan_preserves_both_risk_sources_and_effective_value(
    profile_risk: RiskTier,
    contract_risk: RiskTier,
    effective: RiskTier,
) -> None:
    """Expose original and effective risk without requiring equality."""
    plan = require_plan(
        select_plan(
            planner_input(
                request_profile=request_profile(risk_tier=profile_risk),
                quality_contract=quality_contract(risk_tier=contract_risk),
            )
        )
    )

    assert plan.decision_evidence.profile_risk_tier is profile_risk
    assert plan.decision_evidence.contract_risk_tier is contract_risk
    assert plan.decision_evidence.effective_risk_tier is effective


@pytest.mark.parametrize("has_large_context", [False, True])
def test_input_tokens_override_descriptive_large_context_flag(
    has_large_context: bool,
) -> None:
    """Route context from configured token thresholds only."""
    plan = require_plan(
        select_plan(
            planner_input(
                request_profile=request_profile(
                    input_tokens=4_000,
                    has_large_context=has_large_context,
                )
            )
        )
    )

    assert plan.context_policy is ContextPolicy.REDUCE


@pytest.mark.parametrize(
    ("mode", "quality_profile", "complexity"),
    [
        (OptimizationMode.COST, QualityProfile.STANDARD, Complexity.LOW),
        (OptimizationMode.COST, QualityProfile.STANDARD, Complexity.MEDIUM),
        (OptimizationMode.COST, QualityProfile.HIGH, Complexity.LOW),
        (OptimizationMode.COST, QualityProfile.HIGH, Complexity.MEDIUM),
        (OptimizationMode.COST, QualityProfile.CRITICAL, Complexity.LOW),
        (OptimizationMode.BALANCED, QualityProfile.STANDARD, Complexity.LOW),
        (OptimizationMode.BALANCED, QualityProfile.STANDARD, Complexity.MEDIUM),
        (OptimizationMode.BALANCED, QualityProfile.HIGH, Complexity.LOW),
        (OptimizationMode.BALANCED, QualityProfile.HIGH, Complexity.MEDIUM),
        (OptimizationMode.BALANCED, QualityProfile.CRITICAL, Complexity.LOW),
    ],
)
def test_poor_history_adjustment_reasons_describe_final_strong_plan(
    mode: OptimizationMode,
    quality_profile: QualityProfile,
    complexity: Complexity,
) -> None:
    """Explain every eligible poor-history adjustment as final strong-direct."""
    plan = require_plan(
        select_plan(
            planner_input(
                request_profile=request_profile(complexity=complexity),
                quality_contract=quality_contract(
                    quality_profile=quality_profile,
                    optimization_mode=mode,
                ),
                historical_statistics=HistoricalPolicyStatistics(
                    comparable_sample_count=20,
                    small_pass_without_escalation_rate=0.69,
                    average_final_quality=0.85,
                ),
            )
        )
    )

    assert (
        plan.decision_evidence.base_model_policy
        is ModelPolicy.SMALL_FIRST_WITH_FALLBACK
    )
    assert plan.decision_evidence.final_model_policy is ModelPolicy.STRONG_DIRECT
    assert plan.model_policy is ModelPolicy.STRONG_DIRECT
    assert plan.initial_model_role is ModelRole.STRONG
    assert plan.escalation_model_role is None
    assert plan.decision_evidence.historical_statistics is not None
    assert PlannerReasonCode.HISTORICAL_SMALL_SUCCESS_LOW in plan.reason_codes
    assert PlannerReasonCode.STRONG_MODEL_REQUIRED in plan.reason_codes
    assert PlannerReasonCode.SMALL_FIRST_SELECTED not in plan.reason_codes
    assert len(plan.reason_codes) == len(set(plan.reason_codes))
    mode_reasons = {
        PlannerReasonCode.OPTIMIZATION_MODE_COST,
        PlannerReasonCode.OPTIMIZATION_MODE_BALANCED,
        PlannerReasonCode.OPTIMIZATION_MODE_QUALITY,
    }
    assert len(set(plan.reason_codes) & mode_reasons) == 1


def test_positive_history_retains_small_first_selection_reason() -> None:
    """Keep the small-first reason when positive history retains that policy."""
    plan = require_plan(
        select_plan(
            planner_input(
                historical_statistics=HistoricalPolicyStatistics(
                    comparable_sample_count=20,
                    small_pass_without_escalation_rate=0.95,
                    average_final_quality=0.80,
                )
            )
        )
    )

    assert plan.model_policy is ModelPolicy.SMALL_FIRST_WITH_FALLBACK
    assert PlannerReasonCode.SMALL_FIRST_SELECTED in plan.reason_codes
    assert PlannerReasonCode.HISTORICAL_SMALL_SUCCESS_HIGH in plan.reason_codes


def test_base_strong_direct_selection_reasons_remain_unchanged() -> None:
    """Preserve normal strong-direct reasons when history makes no adjustment."""
    plan = require_plan(
        select_plan(
            planner_input(
                request_profile=request_profile(complexity=Complexity.HIGH),
            )
        )
    )

    assert plan.model_policy is ModelPolicy.STRONG_DIRECT
    assert PlannerReasonCode.STRONG_MODEL_REQUIRED in plan.reason_codes
    assert PlannerReasonCode.HIGH_COMPLEXITY_STRONG_DIRECT in plan.reason_codes
    assert PlannerReasonCode.SMALL_FIRST_SELECTED not in plan.reason_codes


@pytest.mark.parametrize("mode", list(OptimizationMode))
@pytest.mark.parametrize("quality_profile", list(QualityProfile))
def test_high_complexity_remains_strong_after_history(
    mode: OptimizationMode,
    quality_profile: QualityProfile,
) -> None:
    """Prove all nine HIGH cases remain strong under positive history."""
    plan = require_plan(
        select_plan(
            planner_input(
                request_profile=request_profile(complexity=Complexity.HIGH),
                quality_contract=quality_contract(
                    quality_profile=quality_profile,
                    optimization_mode=mode,
                ),
                historical_statistics=HistoricalPolicyStatistics(
                    comparable_sample_count=20,
                    small_pass_without_escalation_rate=0.99,
                    average_final_quality=0.99,
                ),
            )
        )
    )

    assert plan.model_policy is ModelPolicy.STRONG_DIRECT
    assert PlannerReasonCode.HIGH_COMPLEXITY_STRONG_DIRECT in plan.reason_codes


@pytest.mark.parametrize(
    ("mode", "quality_profile", "complexity"),
    [
        (OptimizationMode.COST, QualityProfile.STANDARD, Complexity.LOW),
        (OptimizationMode.COST, QualityProfile.STANDARD, Complexity.MEDIUM),
        (OptimizationMode.COST, QualityProfile.HIGH, Complexity.LOW),
        (OptimizationMode.COST, QualityProfile.HIGH, Complexity.MEDIUM),
        (OptimizationMode.COST, QualityProfile.CRITICAL, Complexity.LOW),
        (OptimizationMode.BALANCED, QualityProfile.STANDARD, Complexity.LOW),
        (OptimizationMode.BALANCED, QualityProfile.STANDARD, Complexity.MEDIUM),
        (OptimizationMode.BALANCED, QualityProfile.HIGH, Complexity.LOW),
        (OptimizationMode.BALANCED, QualityProfile.HIGH, Complexity.MEDIUM),
        (OptimizationMode.BALANCED, QualityProfile.CRITICAL, Complexity.LOW),
        (OptimizationMode.QUALITY, QualityProfile.STANDARD, Complexity.LOW),
        (OptimizationMode.QUALITY, QualityProfile.HIGH, Complexity.LOW),
    ],
)
def test_every_base_small_first_case_has_verified_strong_fallback(
    mode: OptimizationMode,
    quality_profile: QualityProfile,
    complexity: Complexity,
) -> None:
    """Prove the fallback invariant for every small-first matrix entry."""
    plan = require_plan(
        select_plan(
            planner_input(
                request_profile=request_profile(complexity=complexity),
                quality_contract=quality_contract(
                    quality_profile=quality_profile,
                    optimization_mode=mode,
                ),
            )
        )
    )

    assert plan.model_policy is ModelPolicy.SMALL_FIRST_WITH_FALLBACK
    assert plan.initial_model_role is ModelRole.SMALL
    assert plan.verification_required is True
    assert plan.escalation_model_role is ModelRole.STRONG


@pytest.mark.parametrize(
    "updates",
    [
        {"semantic_cache_enabled": False},
        {"context_reduction_enabled": False},
        {"historical_policy_enabled": False},
        {
            "semantic_cache_enabled": False,
            "context_reduction_enabled": False,
            "historical_policy_enabled": False,
        },
    ],
)
def test_module_disabled_combinations_remain_explainable(
    updates: dict[str, object],
) -> None:
    """Honor module gates without removing mandatory verification."""
    configured_modules = modules(**updates)
    plan = require_plan(
        select_plan(
            planner_input(
                modules=configured_modules,
                cache_candidate=cache_candidate(safe_to_reuse=False),
                request_profile=request_profile(input_tokens=8_000),
                historical_statistics=HistoricalPolicyStatistics(
                    comparable_sample_count=20,
                    small_pass_without_escalation_rate=0.69,
                    average_final_quality=0.80,
                ),
            )
        )
    )

    assert plan.verification_required is True
    assert (
        plan.decision_evidence.module_states.model_dump()
        == configured_modules.model_dump()
    )
    if not configured_modules.context_reduction_enabled:
        assert plan.context_policy is ContextPolicy.KEEP_ORIGINAL


def test_foundry_comparator_flag_does_not_change_normal_plan() -> None:
    """Keep the comparison module outside normal OPTIMA routing."""
    disabled = require_plan(select_plan(planner_input()))
    enabled = require_plan(
        select_plan(
            planner_input(modules=modules(foundry_router_comparator_enabled=True))
        )
    )

    assert (
        disabled.model_copy(update={"decision_evidence": enabled.decision_evidence})
        == enabled
    )


@pytest.mark.parametrize(
    ("capabilities", "expected_code"),
    [
        (
            PlannerCapabilities(evaluator_configured=False),
            PlanningFailureCode.EVALUATOR_NOT_CONFIGURED,
        ),
        (
            PlannerCapabilities(strong_model_configured=False),
            PlanningFailureCode.STRONG_MODEL_NOT_CONFIGURED,
        ),
        (
            PlannerCapabilities(small_model_configured=False),
            PlanningFailureCode.INITIAL_MODEL_NOT_CONFIGURED,
        ),
    ],
)
def test_missing_mandatory_capability_returns_typed_failure(
    capabilities: PlannerCapabilities,
    expected_code: PlanningFailureCode,
) -> None:
    """Return structured failure instead of an invalid cheaper plan."""
    result = select_plan(planner_input(capabilities=capabilities))

    assert isinstance(result, PlanningFailure)
    assert result.code is expected_code
    assert (
        result.decision_evidence.final_model_policy
        is ModelPolicy.SMALL_FIRST_WITH_FALLBACK
    )


def test_small_first_plan_preserves_all_structural_and_integrity_guarantees() -> None:
    """Produce verified small-first plans with STRONG fallback and no estimates."""
    plan = require_plan(select_plan(planner_input()))

    assert plan.initial_model_role is ModelRole.SMALL
    assert plan.verification_required is True
    assert plan.escalation_model_role is ModelRole.STRONG
    assert plan.expected_quality_score is None
    assert plan.expected_cost is None
    assert len(plan.reason_codes) == len(set(plan.reason_codes))
    mode_reasons = {
        PlannerReasonCode.OPTIMIZATION_MODE_COST,
        PlannerReasonCode.OPTIMIZATION_MODE_BALANCED,
        PlannerReasonCode.OPTIMIZATION_MODE_QUALITY,
    }
    assert len(set(plan.reason_codes) & mode_reasons) == 1
