"""Pure display projections for backend-returned OPTIMA evidence."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from optima.comparison import BaselineComparison
from optima.domain.execution import (
    CachePolicy,
    ContextReductionOutcome,
    ExecutionEventCode,
    ExecutionStatus,
    ExecutionStep,
    ExecutionStepType,
    ModelPolicy,
    PlannerReasonCode,
    SemanticCacheOutcome,
)
from optima.domain.run import PricingProvenance, RunResult

PRC = PlannerReasonCode


class ContractState(StrEnum):
    """Three honest states for Quality Contract evidence."""

    MET = "Contract Met"
    NOT_MET = "Contract Not Met"
    UNAVAILABLE = "Contract Unavailable"


@dataclass(frozen=True)
class TraceRow:
    """One ordered execution fact ready for rendering."""

    sequence: int
    step: str
    status: str
    latency: str
    events: tuple[str, ...]
    facts: dict[str, object]
    semantic_cache: dict[str, object] | None
    context_reduction: dict[str, object] | None
    context_source: str | None
    error: str | None


@dataclass(frozen=True)
class DecisionView:
    """The backend decision and measured outcome shown before the answer."""

    plan_name: str
    complexity: str
    components: tuple[str, ...]
    reason_codes: tuple[str, ...]
    reason_explanations: tuple[str, ...]
    quality_profile: str
    threshold: str
    optimization_mode: str
    final_quality: str
    contract_state: ContractState
    escalation: str
    model_calls: int


@dataclass(frozen=True)
class ContextReductionView:
    """Measured context-reduction evidence ready for direct rendering."""

    status: str
    original_tokens: int
    effective_tokens: int
    reduction_percentage: str
    method: str
    token_counter: str
    context_source: str


@dataclass(frozen=True)
class SemanticCacheView:
    """Backend-authored semantic-cache evidence ready for direct rendering."""

    outcome: str
    lookup_latency: str
    source_run_id: str
    similarity: str
    evaluator_type: str
    cached_quality: str
    source_threshold: str
    source_passed: str
    planner_reason: str
    error: str | None


REASON_EXPLANATIONS: dict[PlannerReasonCode, str] = {
    PRC.SEMANTIC_CACHE_DISABLED: "Semantic cache was disabled by configuration.",
    PRC.CACHE_REQUEST_NOT_ELIGIBLE: (
        "The supplied request profile was not eligible for cache reuse."
    ),
    PRC.CACHE_CANDIDATE_NOT_SUPPLIED: (
        "No semantic-cache candidate was supplied to Planner V1."
    ),
    PRC.CACHE_SIMILARITY_BELOW_THRESHOLD: (
        "The cache candidate similarity was below the configured threshold."
    ),
    PRC.CACHE_PRIOR_EVALUATOR_INVALID: (
        "The cached result lacked valid prior evaluator evidence."
    ),
    PRC.CACHE_PRIOR_EVALUATION_FAILED: (
        "The cached result did not pass its prior evaluation."
    ),
    PRC.CACHE_QUALITY_BELOW_CONTRACT_THRESHOLD: (
        "The cached quality score was below the current contract threshold."
    ),
    PRC.CACHE_CONTRACT_INCOMPATIBLE: (
        "The cached result was incompatible with the current Quality Contract."
    ),
    PRC.CACHE_REUSE_UNSAFE: "Planner V1 determined that cache reuse was unsafe.",
    PRC.CACHE_HIGH_CONFIDENCE_MATCH: (
        "A safe, contract-compatible high-confidence cache match was selected."
    ),
    PRC.CONTEXT_WITHIN_NORMAL_RANGE: (
        "The supplied input-token count was within the original-context range."
    ),
    PRC.CONTEXT_ABOVE_REDUCTION_THRESHOLD: (
        "The supplied input-token count reached the reduction threshold."
    ),
    PRC.CONTEXT_REDUCTION_SELECTED: (
        "Planner V1 selected context reduction under the active policy."
    ),
    PRC.CONTEXT_REDUCTION_SKIPPED_HIGH_RISK: (
        "Context reduction was skipped because the effective risk was high."
    ),
    PRC.CONTEXT_REDUCTION_SKIPPED_QUALITY_MODE: (
        "Quality mode kept the original context under its conservative risk rule."
    ),
    PRC.CONTEXT_REDUCTION_DISABLED: (
        "Context reduction was disabled by configuration."
    ),
    PRC.SAFE_REDUCER_UNAVAILABLE: "No task-safe context reducer was available.",
    PRC.LOW_COMPLEXITY: (
        "The supplied request profile classified this request as LOW complexity."
    ),
    PRC.MEDIUM_COMPLEXITY: (
        "The supplied request profile classified this request as MEDIUM complexity."
    ),
    PRC.HIGH_COMPLEXITY: (
        "The supplied request profile classified this request as HIGH complexity."
    ),
    PRC.STANDARD_QUALITY_CONTRACT: (
        "The Standard Quality Profile set the required quality threshold."
    ),
    PRC.HIGH_QUALITY_CONTRACT: (
        "The High Quality Profile set the required quality threshold."
    ),
    PRC.CRITICAL_QUALITY_CONTRACT: (
        "The Critical Quality Profile set the required quality threshold."
    ),
    PRC.OPTIMIZATION_MODE_COST: (
        "Cost mode pursued the most cost-aggressive eligible plan without "
        "lowering quality."
    ),
    PRC.OPTIMIZATION_MODE_BALANCED: (
        "Balanced mode applied the default Planner V1 trade-off policy."
    ),
    PRC.OPTIMIZATION_MODE_QUALITY: (
        "Quality mode weighted quality risk more heavily without changing the "
        "threshold."
    ),
    PRC.SMALL_FIRST_SELECTED: (
        "Planner V1 selected SMALL first with mandatory verification and a "
        "configured STRONG fallback."
    ),
    PRC.STRONG_MODEL_REQUIRED: (
        "Planner V1 required the STRONG model role for this request."
    ),
    PRC.HIGH_COMPLEXITY_STRONG_DIRECT: (
        "HIGH complexity selected strong-direct to avoid an expected-waste "
        "SMALL attempt."
    ),
    PRC.QUALITY_MODE_PREFERS_STRONG: (
        "Quality mode selected strong-direct for this profile and complexity."
    ),
    PRC.HISTORICAL_SMALL_SUCCESS_HIGH: (
        "Comparable history supported confidence in the existing small-first plan."
    ),
    PRC.HISTORICAL_SMALL_SUCCESS_LOW: (
        "Comparable history showed poor small-first performance and favored "
        "strong-direct."
    ),
    PRC.HISTORICAL_EVIDENCE_INSUFFICIENT: (
        "There was not enough comparable history to adjust the deterministic plan."
    ),
    PRC.HISTORICAL_EVIDENCE_NEUTRAL: (
        "Comparable history did not justify changing the deterministic plan."
    ),
    PRC.HISTORICAL_POLICY_DISABLED: (
        "Historical policy was disabled by configuration."
    ),
}

EVENT_EXPLANATIONS: dict[ExecutionEventCode, str] = {
    ExecutionEventCode.CACHE_RESULT_REUSED: "Cached result reused",
    ExecutionEventCode.CACHE_MISS: "Semantic cache miss",
    ExecutionEventCode.CACHE_MATCH_REJECTED: "Cache match rejected by Planner V1",
    ExecutionEventCode.CACHE_LOOKUP_FAILED: "Semantic cache lookup failed",
    ExecutionEventCode.CACHE_LOOKUP_TIMED_OUT: "Semantic cache lookup timed out",
    ExecutionEventCode.QUALITY_CONTRACT_MET: "Quality Contract met",
    ExecutionEventCode.QUALITY_THRESHOLD_NOT_MET: "Quality threshold not met",
    ExecutionEventCode.ESCALATION_REQUIRED: "Escalation required",
    ExecutionEventCode.ESCALATED_TO_STRONG: "Escalated to STRONG",
    ExecutionEventCode.FINAL_QUALITY_CONTRACT_NOT_MET: "Final Quality Contract not met",
}


def contract_state(value: bool | None) -> ContractState:
    """Preserve false separately from unavailable evidence."""
    if value is True:
        return ContractState.MET
    if value is False:
        return ContractState.NOT_MET
    return ContractState.UNAVAILABLE


def reason_explanations(
    reason_codes: tuple[PlannerReasonCode, ...],
) -> tuple[str, ...]:
    """Explain reason codes in their backend-defined order."""
    return tuple(REASON_EXPLANATIONS[code] for code in reason_codes)


def trace_rows(steps: tuple[ExecutionStep, ...]) -> tuple[TraceRow, ...]:
    """Project ordered backend steps without adding inferred steps."""
    return tuple(
        TraceRow(
            sequence=step.sequence,
            step=step.step_type.value.replace("_", " ").title(),
            status=step.status.value.replace("_", " ").title(),
            latency=f"{step.latency_ms} ms",
            events=tuple(EVENT_EXPLANATIONS[event] for event in step.event_codes),
            facts=dict(step.facts),
            semantic_cache=(
                step.semantic_cache.model_dump(mode="json")
                if step.semantic_cache is not None
                else None
            ),
            context_reduction=(
                step.context_reduction.model_dump(mode="json")
                if step.context_reduction is not None
                else None
            ),
            context_source=(
                step.context_source.value if step.context_source is not None else None
            ),
            error=step.error,
        )
        for step in steps
    )


def attempted_model_call_count(steps: tuple[ExecutionStep, ...]) -> int:
    """Count actual model-call attempts from backend execution trace facts."""
    return sum(
        step.step_type is ExecutionStepType.MODEL_CALL
        and step.status is not ExecutionStatus.SKIPPED
        for step in steps
    )


def decision_view(result: RunResult) -> DecisionView:
    """Build the decision card entirely from one validated RunResult."""
    plan = result.execution_plan
    components = (
        f"Cache: {plan.cache_policy.value}",
        f"Context: {plan.context_policy.value}",
        f"Model: {plan.model_policy.value if plan.model_policy else 'NOT_APPLICABLE'}",
        f"Verification: {'REQUIRED' if plan.verification_required else 'NOT_REQUIRED'}",
        "Fallback: "
        + (plan.escalation_model_role.value if plan.escalation_model_role else "NONE"),
    )
    cache = semantic_cache_view(result)
    final_quality = "Unavailable"
    if result.final_evaluation is not None and result.final_evaluation.evaluator_valid:
        final_quality = format_score(result.final_evaluation.score)
    elif cache is not None and result.semantic_cache is not None:
        source = result.semantic_cache.prior_evaluation
        if (
            result.semantic_cache.outcome is SemanticCacheOutcome.REUSED
            and source is not None
            and source.evaluator_valid
        ):
            final_quality = format_score(source.score)
    return DecisionView(
        plan_name=plan.human_readable_name,
        complexity=result.request_profile.complexity.value,
        components=components,
        reason_codes=tuple(code.value for code in plan.reason_codes),
        reason_explanations=reason_explanations(plan.reason_codes),
        quality_profile=result.quality_contract.quality_profile.value.title(),
        threshold=format_score(result.quality_contract.minimum_quality_score),
        optimization_mode=result.quality_contract.optimization_mode.value.title(),
        final_quality=final_quality,
        contract_state=contract_state(result.contract_met),
        escalation="Occurred" if result.escalated else "Not required",
        model_calls=attempted_model_call_count(result.steps),
    )


def outcome_label(result: RunResult) -> str:
    """Use Planner V1 terminology for one actual execution outcome."""
    plan = result.execution_plan
    if plan.cache_policy is CachePolicy.USE_CACHED_RESULT:
        return "Semantic cache"
    if plan.model_policy is ModelPolicy.STRONG_DIRECT:
        label = "Strong direct"
    elif result.escalated:
        label = "Small -> Strong escalation"
    else:
        label = "Small first -> verified without escalation"
    reduction = context_reduction_view(result)
    if reduction is not None and reduction.context_source == "Reduced":
        return f"Context reduce -> {label}"
    if reduction is not None:
        return f"Context reduction failed; original context -> {label}"
    return label


def semantic_cache_view(result: RunResult) -> SemanticCacheView | None:
    """Project only typed cache evidence returned by the backend."""
    evidence = result.semantic_cache
    if evidence is None:
        return None
    source = evidence.prior_evaluation
    return SemanticCacheView(
        outcome=evidence.outcome.value.replace("_", " ").title(),
        lookup_latency=f"{evidence.lookup_latency_ms} ms",
        source_run_id=evidence.source_run_id or "Unavailable",
        similarity=(
            f"{evidence.similarity:.3f}"
            if evidence.similarity is not None
            else "Unavailable"
        ),
        evaluator_type=source.evaluator_type if source is not None else "Unavailable",
        cached_quality=format_score(source.score if source is not None else None),
        source_threshold=format_score(source.threshold if source is not None else None),
        source_passed=(
            "Passed"
            if source is not None and source.passed
            else "Failed"
            if source is not None
            else "Unavailable"
        ),
        planner_reason=evidence.planner_reason_code.value,
        error=evidence.error,
    )


def context_reduction_view(result: RunResult) -> ContextReductionView | None:
    """Project measured reduction facts without inferring from the selected plan."""
    evidence = next(
        (
            step.context_reduction
            for step in result.steps
            if step.context_reduction is not None
        ),
        None,
    )
    if evidence is None:
        return None
    applied = evidence.outcome is ContextReductionOutcome.APPLIED
    percentage = "Unavailable"
    if applied:
        reduction = (
            evidence.original_token_count - evidence.effective_token_count
        ) / evidence.original_token_count
        percentage = f"{reduction * 100:.1f}%"
    return ContextReductionView(
        status="Applied" if applied else "Failed; original context used",
        original_tokens=evidence.original_token_count,
        effective_tokens=evidence.effective_token_count,
        reduction_percentage=percentage,
        method=evidence.method or "Unavailable",
        token_counter=evidence.token_counter_name,
        context_source=evidence.context_source.value.title(),
    )


def format_score(value: float | None) -> str:
    """Format measured quality without converting missing values to zero."""
    return "Unavailable" if value is None else f"{value:.2f}"


def format_cost(
    amount: Decimal | None,
    provenance: PricingProvenance | None,
) -> str:
    """Format exact Decimal cost only with compatible pricing provenance."""
    if amount is None or provenance is None:
        return "Unavailable"
    normalized = format(amount, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return f"{provenance.currency} {normalized} (catalog {provenance.catalog_version})"


def comparison_available(comparison: BaselineComparison | None) -> bool:
    """Return true only for compatibility-validated measured arm evidence."""
    return comparison is not None
