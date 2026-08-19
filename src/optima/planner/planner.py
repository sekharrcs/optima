"""Deterministic orchestration for composable Planner V1 policies."""

from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    ExecutionPlan,
    ModelPolicy,
    ModelRole,
    PlannerDecisionEvidence,
    PlannerModuleStates,
    PlannerReasonCode,
)
from optima.domain.quality_contract import OptimizationMode
from optima.planner.models import (
    PlannerCapabilities,
    PlannerInput,
    PlannerResult,
    PlanningFailure,
    PlanningFailureCode,
)
from optima.planner.policies import (
    apply_historical_policy,
    build_plan_name,
    effective_risk_tier,
    evaluate_cache_policy,
    model_policy_reason_codes,
    select_base_model_policy,
    select_context_policy,
)

_MODE_REASONS = {
    OptimizationMode.COST: PlannerReasonCode.OPTIMIZATION_MODE_COST,
    OptimizationMode.BALANCED: PlannerReasonCode.OPTIMIZATION_MODE_BALANCED,
    OptimizationMode.QUALITY: PlannerReasonCode.OPTIMIZATION_MODE_QUALITY,
}


def select_plan(planner_input: PlannerInput) -> PlannerResult:
    """Select a valid composable plan or return a typed structural failure."""
    profile = planner_input.request_profile
    contract = planner_input.quality_contract
    effective_risk = effective_risk_tier(
        profile.risk_tier,
        contract.risk_tier,
    )
    module_states = PlannerModuleStates.model_validate(
        planner_input.modules.model_dump()
    )
    cache_decision = evaluate_cache_policy(
        enabled=planner_input.modules.semantic_cache_enabled,
        profile=profile,
        candidate=planner_input.cache_candidate,
        contract=contract,
        thresholds=planner_input.thresholds,
    )
    if cache_decision.policy is CachePolicy.USE_CACHED_RESULT:
        evidence = PlannerDecisionEvidence(
            profile_risk_tier=profile.risk_tier,
            contract_risk_tier=contract.risk_tier,
            effective_risk_tier=effective_risk,
            module_states=module_states,
            cache_candidate_assessed=cache_decision.candidate_assessed,
        )
        return ExecutionPlan(
            cache_policy=CachePolicy.USE_CACHED_RESULT,
            context_policy=ContextPolicy.NOT_APPLICABLE,
            model_policy=None,
            initial_model_role=None,
            verification_required=False,
            escalation_model_role=None,
            optimization_mode=contract.optimization_mode,
            reason_codes=(
                cache_decision.reason_code,
                _MODE_REASONS[contract.optimization_mode],
            ),
            human_readable_name=build_plan_name(
                cache_policy=CachePolicy.USE_CACHED_RESULT,
                context_policy=ContextPolicy.NOT_APPLICABLE,
                model_policy=None,
            ),
            decision_evidence=evidence,
        )

    context_decision = select_context_policy(
        enabled=planner_input.modules.context_reduction_enabled,
        input_tokens=profile.input_tokens,
        quality_profile=contract.quality_profile,
        optimization_mode=contract.optimization_mode,
        effective_risk=effective_risk,
        reducer=planner_input.reducer_capability,
        thresholds=planner_input.thresholds,
    )
    base_model_decision = select_base_model_policy(
        quality_profile=contract.quality_profile,
        complexity=profile.complexity,
        optimization_mode=contract.optimization_mode,
    )
    history_decision = apply_historical_policy(
        enabled=planner_input.modules.historical_policy_enabled,
        base_policy=base_model_decision.policy,
        complexity=profile.complexity,
        optimization_mode=contract.optimization_mode,
        contract_threshold=contract.minimum_quality_score,
        statistics=planner_input.historical_statistics,
        thresholds=planner_input.thresholds,
    )
    evidence = PlannerDecisionEvidence(
        profile_risk_tier=profile.risk_tier,
        contract_risk_tier=contract.risk_tier,
        effective_risk_tier=effective_risk,
        module_states=module_states,
        cache_candidate_assessed=cache_decision.candidate_assessed,
        historical_statistics=history_decision.evidence,
        base_model_policy=base_model_decision.policy,
        final_model_policy=history_decision.final_policy,
    )
    failure = _validate_capabilities(
        capabilities=planner_input.capabilities,
        final_policy=history_decision.final_policy,
        evidence=evidence,
    )
    if failure is not None:
        return failure

    final_model_reason_codes = model_policy_reason_codes(
        quality_profile=contract.quality_profile,
        complexity=profile.complexity,
        optimization_mode=contract.optimization_mode,
        model_policy=history_decision.final_policy,
    )
    reason_codes = _ordered_unique(
        (cache_decision.reason_code,)
        + context_decision.reason_codes
        + final_model_reason_codes
        + history_decision.reason_codes
    )
    is_small_first = (
        history_decision.final_policy is ModelPolicy.SMALL_FIRST_WITH_FALLBACK
    )
    return ExecutionPlan(
        cache_policy=CachePolicy.SKIP,
        context_policy=context_decision.policy,
        model_policy=history_decision.final_policy,
        initial_model_role=ModelRole.SMALL if is_small_first else ModelRole.STRONG,
        verification_required=True,
        escalation_model_role=ModelRole.STRONG if is_small_first else None,
        optimization_mode=contract.optimization_mode,
        reason_codes=reason_codes,
        human_readable_name=build_plan_name(
            cache_policy=CachePolicy.SKIP,
            context_policy=context_decision.policy,
            model_policy=history_decision.final_policy,
        ),
        decision_evidence=evidence,
    )


def _validate_capabilities(
    *,
    capabilities: PlannerCapabilities,
    final_policy: ModelPolicy,
    evidence: PlannerDecisionEvidence,
) -> PlanningFailure | None:
    """Reject model plans missing mandatory conceptual capabilities."""
    if not capabilities.evaluator_configured:
        return PlanningFailure(
            code=PlanningFailureCode.EVALUATOR_NOT_CONFIGURED,
            message="Quality verification is not configured",
            decision_evidence=evidence,
        )
    if not capabilities.strong_model_configured:
        return PlanningFailure(
            code=PlanningFailureCode.STRONG_MODEL_NOT_CONFIGURED,
            message="The required STRONG model role is not configured",
            decision_evidence=evidence,
        )
    if (
        final_policy is ModelPolicy.SMALL_FIRST_WITH_FALLBACK
        and not capabilities.small_model_configured
    ):
        return PlanningFailure(
            code=PlanningFailureCode.INITIAL_MODEL_NOT_CONFIGURED,
            message="The selected SMALL initial model role is not configured",
            decision_evidence=evidence,
        )
    return None


def _ordered_unique(
    reason_codes: tuple[PlannerReasonCode, ...],
) -> tuple[PlannerReasonCode, ...]:
    """Preserve deterministic reason order while removing duplicates."""
    return tuple(dict.fromkeys(reason_codes))
