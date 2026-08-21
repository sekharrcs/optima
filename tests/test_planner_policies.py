"""Decision-matrix tests for pure Planner V1 policies."""

from itertools import product

import pytest

from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
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
from optima.domain.request_binding import RequestBinding, build_request_binding
from optima.domain.request_profile import Complexity, RequestProfile, TaskType
from optima.planner.models import (
    CacheCandidate,
    ContextReducerCapability,
    HistoricalPolicyStatistics,
    PlannerThresholds,
)
from optima.planner.policies import (
    apply_historical_policy,
    build_plan_name,
    effective_risk_tier,
    evaluate_cache_policy,
    select_base_model_policy,
    select_context_policy,
)

EXPECTED_MODEL_POLICIES = {
    (
        OptimizationMode.COST,
        QualityProfile.STANDARD,
        Complexity.LOW,
    ): ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    (
        OptimizationMode.COST,
        QualityProfile.STANDARD,
        Complexity.MEDIUM,
    ): ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    (
        OptimizationMode.COST,
        QualityProfile.STANDARD,
        Complexity.HIGH,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.COST,
        QualityProfile.HIGH,
        Complexity.LOW,
    ): ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    (
        OptimizationMode.COST,
        QualityProfile.HIGH,
        Complexity.MEDIUM,
    ): ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    (
        OptimizationMode.COST,
        QualityProfile.HIGH,
        Complexity.HIGH,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.COST,
        QualityProfile.CRITICAL,
        Complexity.LOW,
    ): ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    (
        OptimizationMode.COST,
        QualityProfile.CRITICAL,
        Complexity.MEDIUM,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.COST,
        QualityProfile.CRITICAL,
        Complexity.HIGH,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.BALANCED,
        QualityProfile.STANDARD,
        Complexity.LOW,
    ): ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    (
        OptimizationMode.BALANCED,
        QualityProfile.STANDARD,
        Complexity.MEDIUM,
    ): ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    (
        OptimizationMode.BALANCED,
        QualityProfile.STANDARD,
        Complexity.HIGH,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.BALANCED,
        QualityProfile.HIGH,
        Complexity.LOW,
    ): ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    (
        OptimizationMode.BALANCED,
        QualityProfile.HIGH,
        Complexity.MEDIUM,
    ): ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    (
        OptimizationMode.BALANCED,
        QualityProfile.HIGH,
        Complexity.HIGH,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.BALANCED,
        QualityProfile.CRITICAL,
        Complexity.LOW,
    ): ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    (
        OptimizationMode.BALANCED,
        QualityProfile.CRITICAL,
        Complexity.MEDIUM,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.BALANCED,
        QualityProfile.CRITICAL,
        Complexity.HIGH,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.QUALITY,
        QualityProfile.STANDARD,
        Complexity.LOW,
    ): ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    (
        OptimizationMode.QUALITY,
        QualityProfile.STANDARD,
        Complexity.MEDIUM,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.QUALITY,
        QualityProfile.STANDARD,
        Complexity.HIGH,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.QUALITY,
        QualityProfile.HIGH,
        Complexity.LOW,
    ): ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    (
        OptimizationMode.QUALITY,
        QualityProfile.HIGH,
        Complexity.MEDIUM,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.QUALITY,
        QualityProfile.HIGH,
        Complexity.HIGH,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.QUALITY,
        QualityProfile.CRITICAL,
        Complexity.LOW,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.QUALITY,
        QualityProfile.CRITICAL,
        Complexity.MEDIUM,
    ): ModelPolicy.STRONG_DIRECT,
    (
        OptimizationMode.QUALITY,
        QualityProfile.CRITICAL,
        Complexity.HIGH,
    ): ModelPolicy.STRONG_DIRECT,
}


def profile(*, cache_eligible: bool = True) -> RequestProfile:
    """Build a low-risk request profile for policy tests."""
    return RequestProfile(
        task_type=TaskType.SUMMARIZATION,
        complexity=Complexity.LOW,
        input_tokens=4_000,
        risk_tier=RiskTier.LOW,
        cache_eligible=cache_eligible,
        has_large_context=False,
    )


def contract(
    *,
    profile_value: QualityProfile = QualityProfile.STANDARD,
    mode: OptimizationMode = OptimizationMode.COST,
    threshold: float = 0.80,
) -> QualityContract:
    """Build a Quality Contract with an explicit current threshold."""
    return QualityContract(
        quality_profile=profile_value,
        minimum_quality_score=threshold,
        optimization_mode=mode,
        risk_tier=RiskTier.LOW,
    )


def evaluation(**updates: object) -> EvaluationResult:
    """Build accepted prior cache evidence with optional overrides."""
    values: dict[str, object] = {
        "evaluator_type": "deterministic",
        "evaluator_valid": True,
        "score": 0.90,
        "threshold": 0.80,
        "mandatory_checks_passed": True,
        "passed": True,
        "reasons": ("Accepted",),
    }
    values.update(updates)
    return EvaluationResult.model_validate(values)


def request_binding(*, input_text: str = "Summarize incident ARC-9") -> RequestBinding:
    """Build the current complete binding used by cache policy tests."""
    return build_request_binding(
        input_text=input_text,
        context="Incident ARC-9 is resolved.",
        reference_output=None,
        criteria=(),
        metadata={},
        task_type=TaskType.SUMMARIZATION,
        complexity=Complexity.LOW,
    )


def candidate(**updates: object) -> CacheCandidate:
    """Build a safe cache candidate with optional overrides."""
    values: dict[str, object] = {
        "source_run_id": "run-1",
        "output_text": "cached output",
        "request_binding": request_binding(),
        "similarity": 0.95,
        "prior_evaluation": evaluation(),
        "contract_compatible": True,
        "safe_to_reuse": True,
    }
    values.update(updates)
    return CacheCandidate.model_validate(values)


@pytest.mark.parametrize(
    ("profile_risk", "contract_risk"),
    list(product(RiskTier, repeat=2)),
)
def test_effective_risk_uses_more_severe_source(
    profile_risk: RiskTier,
    contract_risk: RiskTier,
) -> None:
    """Cover every profile/contract risk combination."""
    severity = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}
    expected = max((profile_risk, contract_risk), key=severity.__getitem__)

    assert effective_risk_tier(profile_risk, contract_risk) is expected


@pytest.mark.parametrize(
    ("mode", "quality_profile", "complexity", "expected"),
    [
        (*combination, expected)
        for combination, expected in EXPECTED_MODEL_POLICIES.items()
    ],
)
def test_complete_base_model_policy_matrix(
    mode: OptimizationMode,
    quality_profile: QualityProfile,
    complexity: Complexity,
    expected: ModelPolicy,
) -> None:
    """Cover all 27 documented base model-policy combinations."""
    decision = select_base_model_policy(
        quality_profile=quality_profile,
        complexity=complexity,
        optimization_mode=mode,
    )

    assert decision.policy is expected
    mode_reasons = {
        PlannerReasonCode.OPTIMIZATION_MODE_COST,
        PlannerReasonCode.OPTIMIZATION_MODE_BALANCED,
        PlannerReasonCode.OPTIMIZATION_MODE_QUALITY,
    }
    assert len(set(decision.reason_codes) & mode_reasons) == 1
    if complexity is Complexity.HIGH:
        assert decision.policy is ModelPolicy.STRONG_DIRECT
        assert PlannerReasonCode.HIGH_COMPLEXITY_STRONG_DIRECT in decision.reason_codes


@pytest.mark.parametrize(
    ("enabled", "cache_eligible", "candidate_value", "expected_reason", "assessed"),
    [
        (False, True, candidate(), PlannerReasonCode.SEMANTIC_CACHE_DISABLED, False),
        (True, False, candidate(), PlannerReasonCode.CACHE_REQUEST_NOT_ELIGIBLE, False),
        (True, True, None, PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED, False),
        (
            True,
            True,
            candidate(similarity=0.949),
            PlannerReasonCode.CACHE_SIMILARITY_BELOW_THRESHOLD,
            True,
        ),
        (
            True,
            True,
            candidate(prior_evaluation=evaluation(evaluator_valid=False, passed=False)),
            PlannerReasonCode.CACHE_PRIOR_EVALUATOR_INVALID,
            True,
        ),
        (
            True,
            True,
            candidate(prior_evaluation=evaluation(score=0.79, passed=False)),
            PlannerReasonCode.CACHE_PRIOR_EVALUATION_FAILED,
            True,
        ),
        (
            True,
            True,
            candidate(prior_evaluation=evaluation(score=0.85, threshold=0.80)),
            PlannerReasonCode.CACHE_QUALITY_BELOW_CONTRACT_THRESHOLD,
            True,
        ),
        (
            True,
            True,
            candidate(contract_compatible=False),
            PlannerReasonCode.CACHE_CONTRACT_INCOMPATIBLE,
            True,
        ),
        (
            True,
            True,
            candidate(safe_to_reuse=False),
            PlannerReasonCode.CACHE_REUSE_UNSAFE,
            True,
        ),
    ],
)
def test_cache_rejection_taxonomy(
    enabled: bool,
    cache_eligible: bool,
    candidate_value: CacheCandidate | None,
    expected_reason: PlannerReasonCode,
    assessed: bool,
) -> None:
    """Return the first controlling reason for each cache rejection path."""
    decision = evaluate_cache_policy(
        enabled=enabled,
        profile=profile(cache_eligible=cache_eligible),
        request_binding=request_binding(),
        candidate=candidate_value,
        contract=contract(threshold=0.90),
        thresholds=PlannerThresholds(),
    )

    assert decision.policy is CachePolicy.SKIP
    assert decision.reason_code is expected_reason
    assert decision.candidate_assessed is assessed


@pytest.mark.parametrize("similarity", [0.95, 1.0])
@pytest.mark.parametrize("quality", [0.90, 1.0])
def test_cache_accepts_inclusive_similarity_and_quality_thresholds(
    similarity: float,
    quality: float,
) -> None:
    """Accept equality at both cache policy thresholds."""
    decision = evaluate_cache_policy(
        enabled=True,
        profile=profile(),
        request_binding=request_binding(),
        candidate=candidate(
            similarity=similarity,
            prior_evaluation=evaluation(score=quality, threshold=0.80),
        ),
        contract=contract(threshold=0.90),
        thresholds=PlannerThresholds(),
    )

    assert decision.policy is CachePolicy.USE_CACHED_RESULT
    assert decision.reason_code is PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH


def test_cache_accepts_candidate_with_exact_request_binding() -> None:
    """Accept a candidate only when its source binding equals the current request."""
    current_binding = request_binding()

    decision = evaluate_cache_policy(
        enabled=True,
        profile=profile(),
        request_binding=current_binding,
        candidate=candidate(request_binding=current_binding),
        contract=contract(),
        thresholds=PlannerThresholds(),
    )

    assert decision.policy is CachePolicy.USE_CACHED_RESULT
    assert decision.reason_code is PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH


def test_cache_rejects_candidate_with_different_request_binding() -> None:
    """Reject source evidence produced for a different complete request."""
    decision = evaluate_cache_policy(
        enabled=True,
        profile=profile(),
        request_binding=request_binding(input_text="Different current request"),
        candidate=candidate(),
        contract=contract(),
        thresholds=PlannerThresholds(),
    )

    assert decision.policy is CachePolicy.SKIP
    assert decision.candidate_assessed is True
    assert decision.reason_code is PlannerReasonCode.CACHE_REQUEST_BINDING_MISMATCH


@pytest.mark.parametrize(
    "candidate_updates",
    [
        {"similarity": 0.01},
        {
            "prior_evaluation": evaluation(
                evaluator_valid=False,
                passed=False,
            )
        },
        {"prior_evaluation": evaluation(score=0.79, passed=False)},
        {"prior_evaluation": evaluation(score=0.79, threshold=0.70)},
        {"contract_compatible": False},
        {"safe_to_reuse": False},
    ],
)
def test_cache_binding_mismatch_precedes_every_later_candidate_defect(
    candidate_updates: dict[str, object],
) -> None:
    """Stop at source binding mismatch before assessing unrelated evidence."""
    decision = evaluate_cache_policy(
        enabled=True,
        profile=profile(),
        request_binding=request_binding(input_text="Different current request"),
        candidate=candidate(**candidate_updates),
        contract=contract(),
        thresholds=PlannerThresholds(),
    )

    assert decision.policy is CachePolicy.SKIP
    assert decision.candidate_assessed is True
    assert decision.reason_code is PlannerReasonCode.CACHE_REQUEST_BINDING_MISMATCH


@pytest.mark.parametrize(
    ("mode", "tokens", "expected"),
    [
        (OptimizationMode.COST, 3_999, ContextPolicy.KEEP_ORIGINAL),
        (OptimizationMode.COST, 4_000, ContextPolicy.REDUCE),
        (OptimizationMode.COST, 7_999, ContextPolicy.REDUCE),
        (OptimizationMode.COST, 8_000, ContextPolicy.REDUCE),
        (OptimizationMode.BALANCED, 3_999, ContextPolicy.KEEP_ORIGINAL),
        (OptimizationMode.BALANCED, 4_000, ContextPolicy.KEEP_ORIGINAL),
        (OptimizationMode.BALANCED, 7_999, ContextPolicy.KEEP_ORIGINAL),
        (OptimizationMode.BALANCED, 8_000, ContextPolicy.REDUCE),
        (OptimizationMode.QUALITY, 3_999, ContextPolicy.KEEP_ORIGINAL),
        (OptimizationMode.QUALITY, 4_000, ContextPolicy.KEEP_ORIGINAL),
        (OptimizationMode.QUALITY, 7_999, ContextPolicy.KEEP_ORIGINAL),
        (OptimizationMode.QUALITY, 8_000, ContextPolicy.REDUCE),
    ],
)
def test_context_threshold_boundaries(
    mode: OptimizationMode,
    tokens: int,
    expected: ContextPolicy,
) -> None:
    """Apply inclusive 4,000 and 8,000 token thresholds by mode."""
    decision = select_context_policy(
        enabled=True,
        input_tokens=tokens,
        quality_profile=QualityProfile.STANDARD,
        optimization_mode=mode,
        effective_risk=RiskTier.LOW,
        reducer=ContextReducerCapability(
            available=True,
            task_safe=True,
            approved_for_critical_high_risk=False,
        ),
        thresholds=PlannerThresholds(),
    )

    assert decision.policy is expected


@pytest.mark.parametrize(
    ("enabled", "available", "task_safe", "expected_reason"),
    [
        (False, True, True, PlannerReasonCode.CONTEXT_REDUCTION_DISABLED),
        (True, False, True, PlannerReasonCode.SAFE_REDUCER_UNAVAILABLE),
        (True, True, False, PlannerReasonCode.SAFE_REDUCER_UNAVAILABLE),
    ],
)
def test_context_module_and_reducer_gates(
    enabled: bool,
    available: bool,
    task_safe: bool,
    expected_reason: PlannerReasonCode,
) -> None:
    """Keep original context when a module or safety gate blocks reduction."""
    decision = select_context_policy(
        enabled=enabled,
        input_tokens=8_000,
        quality_profile=QualityProfile.STANDARD,
        optimization_mode=OptimizationMode.COST,
        effective_risk=RiskTier.LOW,
        reducer=ContextReducerCapability(
            available=available,
            task_safe=task_safe,
            approved_for_critical_high_risk=True,
        ),
        thresholds=PlannerThresholds(),
    )

    assert decision.policy is ContextPolicy.KEEP_ORIGINAL
    assert decision.reason_codes == (expected_reason,)


@pytest.mark.parametrize("mode", list(OptimizationMode))
@pytest.mark.parametrize("approved", [False, True])
def test_critical_high_risk_requires_explicit_reducer_approval(
    mode: OptimizationMode,
    approved: bool,
) -> None:
    """Apply the explicit CRITICAL/HIGH exception in every mode."""
    decision = select_context_policy(
        enabled=True,
        input_tokens=8_000,
        quality_profile=QualityProfile.CRITICAL,
        optimization_mode=mode,
        effective_risk=RiskTier.HIGH,
        reducer=ContextReducerCapability(
            available=True,
            task_safe=True,
            approved_for_critical_high_risk=approved,
        ),
        thresholds=PlannerThresholds(),
    )

    assert decision.policy is (
        ContextPolicy.REDUCE if approved else ContextPolicy.KEEP_ORIGINAL
    )
    if approved:
        assert PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_HIGH_RISK not in (
            decision.reason_codes
        )
        assert PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_QUALITY_MODE not in (
            decision.reason_codes
        )
    elif mode is OptimizationMode.QUALITY:
        assert decision.reason_codes == (
            PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_HIGH_RISK,
            PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_QUALITY_MODE,
        )
    else:
        assert decision.reason_codes == (
            PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_HIGH_RISK,
        )


def history(
    *,
    samples: int = 20,
    pass_rate: float = 0.95,
    quality: float = 0.90,
) -> HistoricalPolicyStatistics:
    """Build comparable history at documented defaults."""
    return HistoricalPolicyStatistics(
        comparable_sample_count=samples,
        small_pass_without_escalation_rate=pass_rate,
        average_final_quality=quality,
    )


@pytest.mark.parametrize(
    ("statistics", "expected_policy", "expected_disposition"),
    [
        (
            history(samples=19, pass_rate=0.69),
            ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            HistoricalEvidenceDisposition.INSUFFICIENT,
        ),
        (
            history(samples=20, pass_rate=0.699),
            ModelPolicy.STRONG_DIRECT,
            HistoricalEvidenceDisposition.POOR_PERFORMANCE_ADJUSTMENT,
        ),
        (
            history(samples=20, pass_rate=0.70),
            ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            HistoricalEvidenceDisposition.NEUTRAL,
        ),
        (
            history(samples=20, pass_rate=0.701),
            ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            HistoricalEvidenceDisposition.NEUTRAL,
        ),
        (
            history(samples=20, pass_rate=0.949),
            ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            HistoricalEvidenceDisposition.NEUTRAL,
        ),
        (
            history(samples=20, pass_rate=0.95, quality=0.90),
            ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            HistoricalEvidenceDisposition.POSITIVE_CONFIDENCE,
        ),
        (
            history(samples=20, pass_rate=0.951, quality=0.90),
            ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            HistoricalEvidenceDisposition.POSITIVE_CONFIDENCE,
        ),
        (
            history(samples=20, pass_rate=0.95, quality=0.899),
            ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            HistoricalEvidenceDisposition.NEUTRAL,
        ),
    ],
)
def test_historical_threshold_boundaries(
    statistics: HistoricalPolicyStatistics,
    expected_policy: ModelPolicy,
    expected_disposition: HistoricalEvidenceDisposition,
) -> None:
    """Apply inclusive/exclusive sample, pass-rate, and quality boundaries."""
    decision = apply_historical_policy(
        enabled=True,
        base_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
        complexity=Complexity.LOW,
        optimization_mode=OptimizationMode.COST,
        contract_threshold=0.90,
        statistics=statistics,
        thresholds=PlannerThresholds(),
    )

    assert decision.final_policy is expected_policy
    assert decision.evidence is not None
    assert decision.evidence.disposition is expected_disposition


@pytest.mark.parametrize("complexity", list(Complexity))
def test_history_never_downgrades_strong_direct(
    complexity: Complexity,
) -> None:
    """Keep strong-direct for HIGH and every other base-strong decision."""
    decision = apply_historical_policy(
        enabled=True,
        base_policy=ModelPolicy.STRONG_DIRECT,
        complexity=complexity,
        optimization_mode=OptimizationMode.QUALITY,
        contract_threshold=0.90,
        statistics=history(pass_rate=0.99, quality=0.99),
        thresholds=PlannerThresholds(),
    )

    assert decision.final_policy is ModelPolicy.STRONG_DIRECT


def test_quality_mode_high_risk_skips_reduction_outside_exception() -> None:
    """Keep original context for noncritical HIGH risk in QUALITY mode."""
    decision = select_context_policy(
        enabled=True,
        input_tokens=8_000,
        quality_profile=QualityProfile.HIGH,
        optimization_mode=OptimizationMode.QUALITY,
        effective_risk=RiskTier.HIGH,
        reducer=ContextReducerCapability(
            available=True,
            task_safe=True,
            approved_for_critical_high_risk=True,
        ),
        thresholds=PlannerThresholds(),
    )

    assert decision.policy is ContextPolicy.KEEP_ORIGINAL
    assert decision.reason_codes == (
        PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_HIGH_RISK,
        PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_QUALITY_MODE,
    )


@pytest.mark.parametrize(
    ("enabled", "tokens", "available", "task_safe"),
    [
        (True, 7_999, True, True),
        (False, 8_000, True, True),
        (True, 8_000, False, True),
        (True, 8_000, True, False),
    ],
)
def test_quality_mode_reason_requires_threshold_and_reducer_eligibility(
    enabled: bool,
    tokens: int,
    available: bool,
    task_safe: bool,
) -> None:
    """Do not emit the QUALITY skip reason before conservative risk applies."""
    decision = select_context_policy(
        enabled=enabled,
        input_tokens=tokens,
        quality_profile=QualityProfile.HIGH,
        optimization_mode=OptimizationMode.QUALITY,
        effective_risk=RiskTier.HIGH,
        reducer=ContextReducerCapability(
            available=available,
            task_safe=task_safe,
            approved_for_critical_high_risk=False,
        ),
        thresholds=PlannerThresholds(),
    )

    assert PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_QUALITY_MODE not in (
        decision.reason_codes
    )


@pytest.mark.parametrize("mode", [OptimizationMode.COST, OptimizationMode.BALANCED])
def test_non_quality_high_risk_safeguard_omits_quality_mode_reason(
    mode: OptimizationMode,
) -> None:
    """Keep QUALITY-mode reasoning out of COST and BALANCED safeguards."""
    decision = select_context_policy(
        enabled=True,
        input_tokens=8_000,
        quality_profile=QualityProfile.CRITICAL,
        optimization_mode=mode,
        effective_risk=RiskTier.HIGH,
        reducer=ContextReducerCapability(
            available=True,
            task_safe=True,
            approved_for_critical_high_risk=False,
        ),
        thresholds=PlannerThresholds(),
    )

    assert decision.reason_codes == (
        PlannerReasonCode.CONTEXT_REDUCTION_SKIPPED_HIGH_RISK,
    )


def test_injected_thresholds_control_policy_boundaries() -> None:
    """Use injected thresholds instead of documented default literals."""
    thresholds = PlannerThresholds(
        context_reduction_consider_tokens=3_000,
        context_reduction_required_tokens=7_000,
        history_minimum_samples=10,
        history_small_prefer_pass_rate=0.90,
        history_small_avoid_pass_rate=0.60,
    )
    context_decision = select_context_policy(
        enabled=True,
        input_tokens=3_000,
        quality_profile=QualityProfile.STANDARD,
        optimization_mode=OptimizationMode.COST,
        effective_risk=RiskTier.LOW,
        reducer=ContextReducerCapability(
            available=True,
            task_safe=True,
            approved_for_critical_high_risk=False,
        ),
        thresholds=thresholds,
    )
    history_decision = apply_historical_policy(
        enabled=True,
        base_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
        complexity=Complexity.LOW,
        optimization_mode=OptimizationMode.COST,
        contract_threshold=0.80,
        statistics=history(samples=10, pass_rate=0.59),
        thresholds=thresholds,
    )

    assert context_decision.policy is ContextPolicy.REDUCE
    assert history_decision.final_policy is ModelPolicy.STRONG_DIRECT


@pytest.mark.parametrize(
    ("cache_policy", "context_policy", "model_policy", "expected"),
    [
        (
            CachePolicy.USE_CACHED_RESULT,
            ContextPolicy.NOT_APPLICABLE,
            None,
            "Cached Result",
        ),
        (
            CachePolicy.SKIP,
            ContextPolicy.KEEP_ORIGINAL,
            ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            "Small -> Verify -> Escalate if needed",
        ),
        (
            CachePolicy.SKIP,
            ContextPolicy.REDUCE,
            ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            "Reduce Context -> Small -> Verify -> Escalate if needed",
        ),
        (
            CachePolicy.SKIP,
            ContextPolicy.KEEP_ORIGINAL,
            ModelPolicy.STRONG_DIRECT,
            "Strong -> Verify",
        ),
        (
            CachePolicy.SKIP,
            ContextPolicy.REDUCE,
            ModelPolicy.STRONG_DIRECT,
            "Reduce Context -> Strong -> Verify",
        ),
    ],
)
def test_canonical_plan_names(
    cache_policy: CachePolicy,
    context_policy: ContextPolicy,
    model_policy: ModelPolicy | None,
    expected: str,
) -> None:
    """Generate only approved component-derived names."""
    assert (
        build_plan_name(
            cache_policy=cache_policy,
            context_policy=context_policy,
            model_policy=model_policy,
        )
        == expected
    )
