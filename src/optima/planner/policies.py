"""Pure deterministic policy functions composed by Planner V1."""

from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    HistoricalDecisionEvidence,
    HistoricalEvidenceDisposition,
    ModelPolicy,
    PlannerReasonCode,
)
from optima.domain.quality_contract import (
    OptimizationMode,
    QualityContract,
    QualityProfile,
    RiskTier,
)
from optima.domain.request_profile import Complexity, RequestProfile
from optima.planner.models import (
    CacheCandidate,
    CacheDecision,
    ContextDecision,
    ContextReducerCapability,
    HistoricalDecision,
    HistoricalPolicyStatistics,
    ModelDecision,
    PlannerThresholds,
)

_RISK_SEVERITY = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}

_COMPLEXITY_REASONS = {
    Complexity.LOW: PlannerReasonCode.LOW_COMPLEXITY,
    Complexity.MEDIUM: PlannerReasonCode.MEDIUM_COMPLEXITY,
    Complexity.HIGH: PlannerReasonCode.HIGH_COMPLEXITY,
}

_QUALITY_REASONS = {
    QualityProfile.STANDARD: PlannerReasonCode.STANDARD_QUALITY_CONTRACT,
    QualityProfile.HIGH: PlannerReasonCode.HIGH_QUALITY_CONTRACT,
    QualityProfile.CRITICAL: PlannerReasonCode.CRITICAL_QUALITY_CONTRACT,
}

_MODE_REASONS = {
    OptimizationMode.COST: PlannerReasonCode.OPTIMIZATION_MODE_COST,
    OptimizationMode.BALANCED: PlannerReasonCode.OPTIMIZATION_MODE_BALANCED,
    OptimizationMode.QUALITY: PlannerReasonCode.OPTIMIZATION_MODE_QUALITY,
}


def effective_risk_tier(
    profile_risk: RiskTier,
    contract_risk: RiskTier,
) -> RiskTier:
    """Return the more severe supplied risk classification."""
    return max((profile_risk, contract_risk), key=_RISK_SEVERITY.__getitem__)


def evaluate_cache_policy(
    *,
    enabled: bool,
    profile: RequestProfile,
    candidate: CacheCandidate | None,
    contract: QualityContract,
    thresholds: PlannerThresholds,
) -> CacheDecision:
    """Select safe cache reuse or the first controlling rejection reason."""
    if not enabled:
        return CacheDecision(
            policy=CachePolicy.SKIP,
            candidate_assessed=False,
            reason_code=PlannerReasonCode.SEMANTIC_CACHE_DISABLED,
        )
    if not profile.cache_eligible:
        return CacheDecision(
            policy=CachePolicy.SKIP,
            candidate_assessed=False,
            reason_code=PlannerReasonCode.CACHE_REQUEST_NOT_ELIGIBLE,
        )
    if candidate is None:
        return CacheDecision(
            policy=CachePolicy.SKIP,
            candidate_assessed=False,
            reason_code=PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED,
        )

    reason: PlannerReasonCode | None = None
    if candidate.similarity < thresholds.cache_similarity_threshold:
        reason = PlannerReasonCode.CACHE_SIMILARITY_BELOW_THRESHOLD
    elif not candidate.prior_evaluation.evaluator_valid:
        reason = PlannerReasonCode.CACHE_PRIOR_EVALUATOR_INVALID
    elif not candidate.prior_evaluation.passed:
        reason = PlannerReasonCode.CACHE_PRIOR_EVALUATION_FAILED
    elif candidate.prior_evaluation.score < contract.minimum_quality_score:
        reason = PlannerReasonCode.CACHE_QUALITY_BELOW_CONTRACT_THRESHOLD
    elif not candidate.contract_compatible:
        reason = PlannerReasonCode.CACHE_CONTRACT_INCOMPATIBLE
    elif not candidate.safe_to_reuse:
        reason = PlannerReasonCode.CACHE_REUSE_UNSAFE

    if reason is not None:
        return CacheDecision(
            policy=CachePolicy.SKIP,
            candidate_assessed=True,
            reason_code=reason,
        )
    return CacheDecision(
        policy=CachePolicy.USE_CACHED_RESULT,
        candidate_assessed=True,
        reason_code=PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
    )


def select_context_policy(
    *,
    enabled: bool,
    input_tokens: int,
    quality_profile: QualityProfile,
    optimization_mode: OptimizationMode,
    effective_risk: RiskTier,
    reducer: ContextReducerCapability,
    thresholds: PlannerThresholds,
) -> ContextDecision:
    """Select a final context policy from gates, safeguards, and token count."""
    if not enabled:
        return ContextDecision(
            policy=ContextPolicy.KEEP_ORIGINAL,
            reason_codes=(PlannerReasonCode.CONTEXT_REDUCTION_DISABLED,),
        )
    if not reducer.available or not reducer.task_safe:
        return ContextDecision(
            policy=ContextPolicy.KEEP_ORIGINAL,
            reason_codes=(PlannerReasonCode.SAFE_REDUCER_UNAVAILABLE,),
        )

    threshold = (
        thresholds.context_reduction_consider_tokens
        if optimization_mode is OptimizationMode.COST
        else thresholds.context_reduction_required_tokens
    )
    if input_tokens < threshold:
        return ContextDecision(
            policy=ContextPolicy.KEEP_ORIGINAL,
            reason_codes=(PlannerReasonCode.CONTEXT_WITHIN_NORMAL_RANGE,),
        )

    critical_high = (
        quality_profile is QualityProfile.CRITICAL and effective_risk is RiskTier.HIGH
    )
    if critical_high and not reducer.approved_for_critical_high_risk:
        reason_codes: tuple[PlannerReasonCode, ...] = (
            PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_HIGH_RISK,
        )
        if optimization_mode is OptimizationMode.QUALITY:
            reason_codes += (PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_QUALITY_MODE,)
        return ContextDecision(
            policy=ContextPolicy.KEEP_ORIGINAL,
            reason_codes=reason_codes,
        )
    if (
        optimization_mode is OptimizationMode.QUALITY
        and effective_risk is RiskTier.HIGH
        and not critical_high
    ):
        return ContextDecision(
            policy=ContextPolicy.KEEP_ORIGINAL,
            reason_codes=(
                PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_HIGH_RISK,
                PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_QUALITY_MODE,
            ),
        )
    return ContextDecision(
        policy=ContextPolicy.REDUCE,
        reason_codes=(
            PlannerReasonCode.CONTEXT_ABOVE_REDUCTION_THRESHOLD,
            PlannerReasonCode.CONTEXT_REDUCTION_SELECTED,
        ),
    )


def select_base_model_policy(
    *,
    quality_profile: QualityProfile,
    complexity: Complexity,
    optimization_mode: OptimizationMode,
) -> ModelDecision:
    """Apply the complete deterministic 27-case Planner V1 model matrix."""
    use_small = complexity is not Complexity.HIGH and (
        (
            optimization_mode in {OptimizationMode.COST, OptimizationMode.BALANCED}
            and not (
                quality_profile is QualityProfile.CRITICAL
                and complexity is Complexity.MEDIUM
            )
        )
        or (
            optimization_mode is OptimizationMode.QUALITY
            and complexity is Complexity.LOW
            and quality_profile is not QualityProfile.CRITICAL
        )
    )
    policy = (
        ModelPolicy.SMALL_FIRST_WITH_FALLBACK
        if use_small
        else ModelPolicy.STRONG_DIRECT
    )
    return ModelDecision(
        policy=policy,
        reason_codes=model_policy_reason_codes(
            quality_profile=quality_profile,
            complexity=complexity,
            optimization_mode=optimization_mode,
            model_policy=policy,
        ),
    )


def model_policy_reason_codes(
    *,
    quality_profile: QualityProfile,
    complexity: Complexity,
    optimization_mode: OptimizationMode,
    model_policy: ModelPolicy,
) -> tuple[PlannerReasonCode, ...]:
    """Explain request facts and the final selected model policy."""
    common_reasons = (
        _COMPLEXITY_REASONS[complexity],
        _QUALITY_REASONS[quality_profile],
        _MODE_REASONS[optimization_mode],
    )
    if model_policy is ModelPolicy.SMALL_FIRST_WITH_FALLBACK:
        return common_reasons + (PlannerReasonCode.SMALL_FIRST_SELECTED,)

    strong_reasons: tuple[PlannerReasonCode, ...] = ()
    if complexity is Complexity.HIGH:
        strong_reasons += (PlannerReasonCode.HIGH_COMPLEXITY_STRONG_DIRECT,)
    elif optimization_mode is OptimizationMode.QUALITY:
        strong_reasons += (PlannerReasonCode.QUALITY_MODE_PREFERS_STRONG,)
    return common_reasons + strong_reasons + (PlannerReasonCode.STRONG_MODEL_REQUIRED,)


def apply_historical_policy(
    *,
    enabled: bool,
    base_policy: ModelPolicy,
    complexity: Complexity,
    optimization_mode: OptimizationMode,
    contract_threshold: float,
    statistics: HistoricalPolicyStatistics | None,
    thresholds: PlannerThresholds,
) -> HistoricalDecision:
    """Apply at most one deterministic historical-policy adjustment."""
    if not enabled:
        return HistoricalDecision(
            final_policy=base_policy,
            reason_codes=(PlannerReasonCode.HISTORICAL_POLICY_DISABLED,),
        )
    if statistics is None:
        return HistoricalDecision(
            final_policy=base_policy,
            reason_codes=(PlannerReasonCode.HISTORICAL_EVIDENCE_INSUFFICIENT,),
        )
    if statistics.comparable_sample_count < thresholds.history_minimum_samples:
        return _history_decision(
            base_policy,
            statistics,
            PlannerReasonCode.HISTORICAL_EVIDENCE_INSUFFICIENT,
            HistoricalEvidenceDisposition.INSUFFICIENT,
        )

    poor = (
        statistics.small_pass_without_escalation_rate
        < thresholds.history_small_avoid_pass_rate
    )
    eligible_adjustment = (
        base_policy is ModelPolicy.SMALL_FIRST_WITH_FALLBACK
        and complexity is not Complexity.HIGH
        and optimization_mode in {OptimizationMode.COST, OptimizationMode.BALANCED}
    )
    if poor and eligible_adjustment:
        return _history_decision(
            ModelPolicy.STRONG_DIRECT,
            statistics,
            PlannerReasonCode.HISTORICAL_SMALL_SUCCESS_LOW,
            HistoricalEvidenceDisposition.POOR_PERFORMANCE_ADJUSTMENT,
        )
    if poor:
        return _history_decision(
            base_policy,
            statistics,
            PlannerReasonCode.HISTORICAL_SMALL_SUCCESS_LOW,
            HistoricalEvidenceDisposition.POOR_PERFORMANCE_CONFIDENCE,
        )

    positive = (
        base_policy is ModelPolicy.SMALL_FIRST_WITH_FALLBACK
        and statistics.small_pass_without_escalation_rate
        >= thresholds.history_small_prefer_pass_rate
        and statistics.average_final_quality >= contract_threshold
    )
    if positive:
        return _history_decision(
            base_policy,
            statistics,
            PlannerReasonCode.HISTORICAL_SMALL_SUCCESS_HIGH,
            HistoricalEvidenceDisposition.POSITIVE_CONFIDENCE,
        )
    return _history_decision(
        base_policy,
        statistics,
        PlannerReasonCode.HISTORICAL_EVIDENCE_NEUTRAL,
        HistoricalEvidenceDisposition.NEUTRAL,
    )


def _history_decision(
    final_policy: ModelPolicy,
    statistics: HistoricalPolicyStatistics,
    reason_code: PlannerReasonCode,
    disposition: HistoricalEvidenceDisposition,
) -> HistoricalDecision:
    """Build a historical decision preserving all supporting statistics."""
    return HistoricalDecision(
        final_policy=final_policy,
        reason_codes=(reason_code,),
        evidence=HistoricalDecisionEvidence(
            comparable_sample_count=statistics.comparable_sample_count,
            small_pass_without_escalation_rate=(
                statistics.small_pass_without_escalation_rate
            ),
            average_final_quality=statistics.average_final_quality,
            disposition=disposition,
        ),
    )


def build_plan_name(
    *,
    cache_policy: CachePolicy,
    context_policy: ContextPolicy,
    model_policy: ModelPolicy | None,
) -> str:
    """Derive the canonical friendly label from selected components."""
    if cache_policy is CachePolicy.USE_CACHED_RESULT:
        if context_policy is not ContextPolicy.NOT_APPLICABLE or model_policy:
            raise ValueError("cached plans cannot include context or model policy")
        return "Cached Result"
    if context_policy is ContextPolicy.NOT_APPLICABLE or model_policy is None:
        raise ValueError("model plans require context and model policies")

    prefix = "Reduce Context -> " if context_policy is ContextPolicy.REDUCE else ""
    if model_policy is ModelPolicy.SMALL_FIRST_WITH_FALLBACK:
        return f"{prefix}Small -> Verify -> Escalate if needed"
    return f"{prefix}Strong -> Verify"
