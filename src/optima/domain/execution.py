"""Execution plan structure and actual execution-step facts."""

from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, NamedTuple

from pydantic import Field, model_validator

from optima.context.contracts import ContextPreservationEvidence
from optima.domain.cache import CacheCandidate, CacheCandidateAssessment
from optima.domain.embedding import EmbeddingAttempt
from optima.domain.evaluation import EvaluationResult
from optima.domain.quality_contract import (
    OptimizationMode,
    QualityProfile,
    QualityScore,
    RiskTier,
)
from optima.domain.request_binding import RequestBinding
from optima.immutable import ImmutableJsonObject, ImmutableModel

NonNegativeMilliseconds = Annotated[int, Field(strict=True, ge=0)]
NonNegativeDecimal = Annotated[
    Decimal,
    Field(ge=Decimal("0"), allow_inf_nan=False),
]
NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
StrictBoolean = Annotated[bool, Field(strict=True)]
Rate = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]


class CachePolicy(StrEnum):
    """Semantic-cache decision represented in an Execution Plan."""

    SKIP = "SKIP"
    USE_CACHED_RESULT = "USE_CACHED_RESULT"


class ContextPolicy(StrEnum):
    """Final context decision represented in an Execution Plan."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    KEEP_ORIGINAL = "KEEP_ORIGINAL"
    REDUCE = "REDUCE"


class ModelPolicy(StrEnum):
    """Planner V1 model-policy choices."""

    SMALL_FIRST_WITH_FALLBACK = "SMALL_FIRST_WITH_FALLBACK"
    STRONG_DIRECT = "STRONG_DIRECT"


class ModelRole(StrEnum):
    """Provider-independent model roles."""

    SMALL = "SMALL"
    STRONG = "STRONG"
    JUDGE = "JUDGE"


class PlannerReasonCode(StrEnum):
    """Reason codes currently defined by Planner V1 specifications."""

    SEMANTIC_CACHE_DISABLED = "SEMANTIC_CACHE_DISABLED"
    CACHE_REQUEST_NOT_ELIGIBLE = "CACHE_REQUEST_NOT_ELIGIBLE"
    CACHE_CANDIDATE_NOT_SUPPLIED = "CACHE_CANDIDATE_NOT_SUPPLIED"
    CACHE_REQUEST_BINDING_MISMATCH = "CACHE_REQUEST_BINDING_MISMATCH"
    CACHE_SIMILARITY_BELOW_THRESHOLD = "CACHE_SIMILARITY_BELOW_THRESHOLD"
    CACHE_PRIOR_EVALUATOR_INVALID = "CACHE_PRIOR_EVALUATOR_INVALID"
    CACHE_PRIOR_EVALUATION_FAILED = "CACHE_PRIOR_EVALUATION_FAILED"
    CACHE_QUALITY_BELOW_CONTRACT_THRESHOLD = "CACHE_QUALITY_BELOW_CONTRACT_THRESHOLD"
    CACHE_CONTRACT_INCOMPATIBLE = "CACHE_CONTRACT_INCOMPATIBLE"
    CACHE_REUSE_UNSAFE = "CACHE_REUSE_UNSAFE"
    CACHE_HIGH_CONFIDENCE_MATCH = "CACHE_HIGH_CONFIDENCE_MATCH"
    CONTEXT_WITHIN_NORMAL_RANGE = "CONTEXT_WITHIN_NORMAL_RANGE"
    CONTEXT_ABOVE_REDUCTION_THRESHOLD = "CONTEXT_ABOVE_REDUCTION_THRESHOLD"
    CONTEXT_REDUCTION_SELECTED = "CONTEXT_REDUCTION_SELECTED"
    CONTEXT_REDUCTION_SKIPPED_HIGH_RISK = "CONTEXT_REDUCTION_SKIPPED_HIGH_RISK"
    CONTEXT_REDUCTION_SKIPPED_QUALITY_MODE = "CONTEXT_REDUCTION_SKIPPED_QUALITY_MODE"
    CONTEXT_REDUCTION_DISABLED = "CONTEXT_REDUCTION_DISABLED"
    SAFE_REDUCER_UNAVAILABLE = "SAFE_REDUCER_UNAVAILABLE"
    LOW_COMPLEXITY = "LOW_COMPLEXITY"
    MEDIUM_COMPLEXITY = "MEDIUM_COMPLEXITY"
    HIGH_COMPLEXITY = "HIGH_COMPLEXITY"
    STANDARD_QUALITY_CONTRACT = "STANDARD_QUALITY_CONTRACT"
    HIGH_QUALITY_CONTRACT = "HIGH_QUALITY_CONTRACT"
    CRITICAL_QUALITY_CONTRACT = "CRITICAL_QUALITY_CONTRACT"
    OPTIMIZATION_MODE_COST = "OPTIMIZATION_MODE_COST"
    OPTIMIZATION_MODE_BALANCED = "OPTIMIZATION_MODE_BALANCED"
    OPTIMIZATION_MODE_QUALITY = "OPTIMIZATION_MODE_QUALITY"
    SMALL_FIRST_SELECTED = "SMALL_FIRST_SELECTED"
    STRONG_MODEL_REQUIRED = "STRONG_MODEL_REQUIRED"
    HIGH_COMPLEXITY_STRONG_DIRECT = "HIGH_COMPLEXITY_STRONG_DIRECT"
    QUALITY_MODE_PREFERS_STRONG = "QUALITY_MODE_PREFERS_STRONG"
    HISTORICAL_SMALL_SUCCESS_HIGH = "HISTORICAL_SMALL_SUCCESS_HIGH"
    HISTORICAL_SMALL_SUCCESS_LOW = "HISTORICAL_SMALL_SUCCESS_LOW"
    HISTORICAL_EVIDENCE_INSUFFICIENT = "HISTORICAL_EVIDENCE_INSUFFICIENT"
    HISTORICAL_EVIDENCE_NEUTRAL = "HISTORICAL_EVIDENCE_NEUTRAL"
    HISTORICAL_POLICY_DISABLED = "HISTORICAL_POLICY_DISABLED"


class HistoricalEvidenceDisposition(StrEnum):
    """How comparable historical statistics affected a planner decision."""

    INSUFFICIENT = "INSUFFICIENT"
    NEUTRAL = "NEUTRAL"
    POSITIVE_CONFIDENCE = "POSITIVE_CONFIDENCE"
    POOR_PERFORMANCE_CONFIDENCE = "POOR_PERFORMANCE_CONFIDENCE"
    POOR_PERFORMANCE_ADJUSTMENT = "POOR_PERFORMANCE_ADJUSTMENT"


class PlannerModuleStates(ImmutableModel):
    """Immutable optional-module state captured when planning."""

    semantic_cache_enabled: StrictBoolean
    context_reduction_enabled: StrictBoolean
    historical_policy_enabled: StrictBoolean
    foundry_router_comparator_enabled: StrictBoolean


class HistoricalDecisionEvidence(ImmutableModel):
    """Comparable historical facts considered by Planner V1."""

    comparable_sample_count: NonNegativeCount
    small_pass_without_escalation_rate: QualityScore
    average_final_quality: QualityScore
    disposition: HistoricalEvidenceDisposition


class PlannerDecisionEvidence(ImmutableModel):
    """Typed pre-execution evidence supporting one planner result."""

    profile_risk_tier: RiskTier
    contract_risk_tier: RiskTier
    effective_risk_tier: RiskTier
    module_states: PlannerModuleStates
    cache_similarity_threshold: Rate = 0.95
    cache_candidate_assessed: StrictBoolean
    historical_statistics: HistoricalDecisionEvidence | None = None
    base_model_policy: ModelPolicy | None = None
    final_model_policy: ModelPolicy | None = None

    @model_validator(mode="after")
    def validate_decision_evidence(self) -> "PlannerDecisionEvidence":
        """Keep derived risk and module-dependent evidence internally consistent."""
        risk_order = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}
        expected_risk = max(
            (self.profile_risk_tier, self.contract_risk_tier),
            key=risk_order.__getitem__,
        )
        if self.effective_risk_tier is not expected_risk:
            raise ValueError("effective risk must be the most severe supplied tier")
        if self.cache_candidate_assessed and not (
            self.module_states.semantic_cache_enabled
        ):
            raise ValueError("disabled semantic cache cannot assess a candidate")
        if self.historical_statistics is not None and not (
            self.module_states.historical_policy_enabled
        ):
            raise ValueError("disabled historical policy cannot record statistics")
        return self


class ExecutionEventCode(StrEnum):
    """Runtime result and escalation events defined by Planner V1."""

    CACHE_RESULT_REUSED = "CACHE_RESULT_REUSED"
    CACHE_MISS = "CACHE_MISS"
    CACHE_MATCH_REJECTED = "CACHE_MATCH_REJECTED"
    CACHE_LOOKUP_FAILED = "CACHE_LOOKUP_FAILED"
    CACHE_LOOKUP_TIMED_OUT = "CACHE_LOOKUP_TIMED_OUT"
    QUALITY_CONTRACT_MET = "QUALITY_CONTRACT_MET"
    QUALITY_THRESHOLD_NOT_MET = "QUALITY_THRESHOLD_NOT_MET"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"
    ESCALATED_TO_STRONG = "ESCALATED_TO_STRONG"
    FINAL_QUALITY_CONTRACT_NOT_MET = "FINAL_QUALITY_CONTRACT_NOT_MET"


class ExecutionStepType(StrEnum):
    """Kinds of actual execution facts shown in a decision trace."""

    SEMANTIC_CACHE = "SEMANTIC_CACHE"
    CONTEXT_REDUCTION = "CONTEXT_REDUCTION"
    MODEL_CALL = "MODEL_CALL"
    QUALITY_EVALUATION = "QUALITY_EVALUATION"
    ESCALATION = "ESCALATION"
    RETURN = "RETURN"


class ExecutionStatus(StrEnum):
    """Outcome of one execution step."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    SKIPPED = "SKIPPED"


class ContextReductionOutcome(StrEnum):
    """Actual runtime result of a selected context-reduction attempt."""

    APPLIED = "APPLIED"
    FAILED_USING_ORIGINAL = "FAILED_USING_ORIGINAL"


class ContextSource(StrEnum):
    """Context variant supplied to a runtime operation."""

    ORIGINAL = "ORIGINAL"
    REDUCED = "REDUCED"


class SemanticCacheOutcome(StrEnum):
    """Actual semantic-cache outcome for one request."""

    DISABLED_BYPASSED = "DISABLED_BYPASSED"
    INELIGIBLE_BYPASSED = "INELIGIBLE_BYPASSED"
    MISS = "MISS"
    MATCH_REJECTED = "MATCH_REJECTED"
    REUSED = "REUSED"
    LOOKUP_FAILED = "LOOKUP_FAILED"
    LOOKUP_TIMED_OUT = "LOOKUP_TIMED_OUT"


class SemanticCacheOutcomeContract(NamedTuple):
    """Complete domain requirements for one semantic-cache runtime outcome."""

    required_module_enabled: bool
    required_cache_eligible: bool | None
    candidate_assessed: bool
    allowed_planner_reasons: frozenset[PlannerReasonCode]
    cache_policy: CachePolicy
    source_evidence_required: bool
    error_required: bool
    step_status: ExecutionStatus | None
    step_event_codes: tuple[ExecutionEventCode, ...]


_SEMANTIC_CACHE_OUTCOME_CONTRACTS = MappingProxyType(
    {
        SemanticCacheOutcome.DISABLED_BYPASSED: SemanticCacheOutcomeContract(
            required_module_enabled=False,
            required_cache_eligible=None,
            candidate_assessed=False,
            allowed_planner_reasons=frozenset(
                {PlannerReasonCode.SEMANTIC_CACHE_DISABLED}
            ),
            cache_policy=CachePolicy.SKIP,
            source_evidence_required=False,
            error_required=False,
            step_status=None,
            step_event_codes=(),
        ),
        SemanticCacheOutcome.INELIGIBLE_BYPASSED: SemanticCacheOutcomeContract(
            required_module_enabled=True,
            required_cache_eligible=False,
            candidate_assessed=False,
            allowed_planner_reasons=frozenset(
                {PlannerReasonCode.CACHE_REQUEST_NOT_ELIGIBLE}
            ),
            cache_policy=CachePolicy.SKIP,
            source_evidence_required=False,
            error_required=False,
            step_status=None,
            step_event_codes=(),
        ),
        SemanticCacheOutcome.MISS: SemanticCacheOutcomeContract(
            required_module_enabled=True,
            required_cache_eligible=True,
            candidate_assessed=False,
            allowed_planner_reasons=frozenset(
                {PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED}
            ),
            cache_policy=CachePolicy.SKIP,
            source_evidence_required=False,
            error_required=False,
            step_status=ExecutionStatus.SUCCEEDED,
            step_event_codes=(ExecutionEventCode.CACHE_MISS,),
        ),
        SemanticCacheOutcome.MATCH_REJECTED: SemanticCacheOutcomeContract(
            required_module_enabled=True,
            required_cache_eligible=True,
            candidate_assessed=True,
            allowed_planner_reasons=frozenset(
                {
                    PlannerReasonCode.CACHE_REQUEST_BINDING_MISMATCH,
                    PlannerReasonCode.CACHE_SIMILARITY_BELOW_THRESHOLD,
                    PlannerReasonCode.CACHE_PRIOR_EVALUATOR_INVALID,
                    PlannerReasonCode.CACHE_PRIOR_EVALUATION_FAILED,
                    PlannerReasonCode.CACHE_QUALITY_BELOW_CONTRACT_THRESHOLD,
                    PlannerReasonCode.CACHE_CONTRACT_INCOMPATIBLE,
                    PlannerReasonCode.CACHE_REUSE_UNSAFE,
                }
            ),
            cache_policy=CachePolicy.SKIP,
            source_evidence_required=True,
            error_required=False,
            step_status=ExecutionStatus.SUCCEEDED,
            step_event_codes=(ExecutionEventCode.CACHE_MATCH_REJECTED,),
        ),
        SemanticCacheOutcome.REUSED: SemanticCacheOutcomeContract(
            required_module_enabled=True,
            required_cache_eligible=True,
            candidate_assessed=True,
            allowed_planner_reasons=frozenset(
                {PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH}
            ),
            cache_policy=CachePolicy.USE_CACHED_RESULT,
            source_evidence_required=True,
            error_required=False,
            step_status=ExecutionStatus.SUCCEEDED,
            step_event_codes=(
                ExecutionEventCode.CACHE_RESULT_REUSED,
                ExecutionEventCode.QUALITY_CONTRACT_MET,
            ),
        ),
        SemanticCacheOutcome.LOOKUP_FAILED: SemanticCacheOutcomeContract(
            required_module_enabled=True,
            required_cache_eligible=True,
            candidate_assessed=False,
            allowed_planner_reasons=frozenset(
                {PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED}
            ),
            cache_policy=CachePolicy.SKIP,
            source_evidence_required=False,
            error_required=True,
            step_status=ExecutionStatus.FAILED,
            step_event_codes=(ExecutionEventCode.CACHE_LOOKUP_FAILED,),
        ),
        SemanticCacheOutcome.LOOKUP_TIMED_OUT: SemanticCacheOutcomeContract(
            required_module_enabled=True,
            required_cache_eligible=True,
            candidate_assessed=False,
            allowed_planner_reasons=frozenset(
                {PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED}
            ),
            cache_policy=CachePolicy.SKIP,
            source_evidence_required=False,
            error_required=True,
            step_status=ExecutionStatus.TIMED_OUT,
            step_event_codes=(ExecutionEventCode.CACHE_LOOKUP_TIMED_OUT,),
        ),
    }
)

if frozenset(_SEMANTIC_CACHE_OUTCOME_CONTRACTS) != frozenset(SemanticCacheOutcome):
    raise RuntimeError("semantic-cache outcome contract table must be exhaustive")


def semantic_cache_outcome_contract(
    outcome: SemanticCacheOutcome,
) -> SemanticCacheOutcomeContract:
    """Return the complete immutable contract for one cache outcome."""
    return _SEMANTIC_CACHE_OUTCOME_CONTRACTS[outcome]


class SemanticCacheEvidence(ImmutableModel):
    """Typed lookup, planner-assessment, and reuse facts for one request."""

    outcome: SemanticCacheOutcome
    lookup_latency_ms: NonNegativeMilliseconds
    planner_reason_code: PlannerReasonCode
    source_run_id: NonEmptyString | None = None
    similarity: Rate | None = None
    prior_evaluation: EvaluationResult | None = None
    candidate_assessment: CacheCandidateAssessment | None = None
    embedding_attempt: EmbeddingAttempt | None = None
    error: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "SemanticCacheEvidence":
        """Reject impossible combinations of cache outcome and evidence."""
        contract = semantic_cache_outcome_contract(self.outcome)
        if self.planner_reason_code not in contract.allowed_planner_reasons:
            raise ValueError("cache outcome and planner reason must agree")
        source_values = (
            self.source_run_id,
            self.similarity,
            self.prior_evaluation,
        )
        has_all_source = all(value is not None for value in source_values)
        has_any_source = any(value is not None for value in source_values)
        if contract.source_evidence_required is not has_all_source:
            raise ValueError("cache outcome has invalid source evidence")
        if not contract.source_evidence_required and has_any_source:
            raise ValueError("cache outcome cannot contain partial source evidence")
        if contract.error_required is not (self.error is not None):
            raise ValueError("cache outcome has invalid error evidence")
        if self.candidate_assessment is not None and (
            self.source_run_id != self.candidate_assessment.source_run_id
            or self.similarity != self.candidate_assessment.similarity
            or self.prior_evaluation != self.candidate_assessment.prior_evaluation
        ):
            raise ValueError("cache source evidence must match candidate assessment")
        if contract.step_status is None and self.lookup_latency_ms != 0:
            raise ValueError("cache bypass cannot claim lookup latency")
        if contract.step_status is None and self.embedding_attempt is not None:
            raise ValueError("cache bypass cannot claim an embedding attempt")
        return self


class ContextReductionEvidence(ImmutableModel):
    """Measured context-reduction facts for one runtime attempt."""

    outcome: ContextReductionOutcome
    original_token_count: NonNegativeCount
    effective_token_count: NonNegativeCount
    reducer_name: NonEmptyString
    method: NonEmptyString | None = None
    token_counter_name: NonEmptyString
    context_source: ContextSource
    preservation: ContextPreservationEvidence | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ContextReductionEvidence":
        """Align applied and recovered outcomes with their measured evidence."""
        if self.outcome is ContextReductionOutcome.APPLIED:
            if self.context_source is not ContextSource.REDUCED:
                raise ValueError("applied reduction must use reduced context")
            if self.effective_token_count >= self.original_token_count:
                raise ValueError("applied reduction must reduce measured tokens")
            if self.method is None or self.preservation is None:
                raise ValueError("applied reduction requires method and preservation")
            return self
        if self.context_source is not ContextSource.ORIGINAL:
            raise ValueError("failed reduction must use original context")
        if self.effective_token_count != self.original_token_count:
            raise ValueError("failed reduction cannot claim changed token counts")
        if self.preservation is not None:
            raise ValueError("failed reduction cannot claim preservation evidence")
        return self


class ExecutionPlan(ImmutableModel):
    """Provider-independent pre-execution plan selected by Planner V1."""

    cache_policy: CachePolicy
    context_policy: ContextPolicy
    model_policy: ModelPolicy | None = None
    initial_model_role: ModelRole | None = None
    verification_required: Annotated[bool, Field(strict=True)]
    escalation_model_role: ModelRole | None = None
    optimization_mode: OptimizationMode
    quality_profile: QualityProfile
    reason_codes: Annotated[tuple[PlannerReasonCode, ...], Field(min_length=1)]
    human_readable_name: NonEmptyString
    decision_evidence: PlannerDecisionEvidence
    cache_candidate: CacheCandidate | None = None
    cache_candidate_assessment: CacheCandidateAssessment | None = None
    request_binding: RequestBinding
    expected_quality_score: QualityScore | None = None
    expected_cost: NonNegativeDecimal | None = None

    @model_validator(mode="after")
    def validate_plan_shape(self) -> "ExecutionPlan":
        """Enforce cache, small-first, strong-direct, and reason-code invariants."""
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason codes must be unique")
        if self.decision_evidence.cache_candidate_assessed is not (
            self.cache_candidate_assessment is not None
        ):
            raise ValueError(
                "candidate-assessed evidence requires one candidate assessment"
            )

        expected_mode_code = {
            OptimizationMode.COST: PlannerReasonCode.OPTIMIZATION_MODE_COST,
            OptimizationMode.BALANCED: PlannerReasonCode.OPTIMIZATION_MODE_BALANCED,
            OptimizationMode.QUALITY: PlannerReasonCode.OPTIMIZATION_MODE_QUALITY,
        }[self.optimization_mode]
        mode_codes = {
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
            PlannerReasonCode.OPTIMIZATION_MODE_BALANCED,
            PlannerReasonCode.OPTIMIZATION_MODE_QUALITY,
        }
        selected_mode_codes = set(self.reason_codes) & mode_codes
        if selected_mode_codes != {expected_mode_code}:
            raise ValueError(
                "reason codes must contain exactly the selected optimization mode"
            )

        cache_reason_codes = {
            reason
            for reason in self.reason_codes
            if reason.value.startswith("CACHE_")
            or reason is PlannerReasonCode.SEMANTIC_CACHE_DISABLED
        }
        if len(cache_reason_codes) > 1:
            raise ValueError(
                "execution plans cannot contain contradictory cache reasons"
            )

        if self.cache_policy is CachePolicy.USE_CACHED_RESULT:
            if self.context_policy is not ContextPolicy.NOT_APPLICABLE:
                raise ValueError("semantic-cache plans require context NOT_APPLICABLE")
            if any(
                value is not None
                for value in (
                    self.model_policy,
                    self.initial_model_role,
                    self.escalation_model_role,
                )
            ):
                raise ValueError(
                    "semantic-cache plans cannot contain model policy or roles"
                )
            if self.verification_required:
                raise ValueError(
                    "semantic-cache plans reuse existing valid evaluation evidence"
                )
            if PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH not in self.reason_codes:
                raise ValueError("semantic-cache plans require a cache-match reason")
            if not self.decision_evidence.cache_candidate_assessed:
                raise ValueError("semantic-cache plans require candidate assessment")
            if self.cache_candidate is None:
                raise ValueError("semantic-cache plans require the resolved candidate")
            if self.cache_candidate_assessment is None:
                raise ValueError("semantic-cache plans require candidate assessment")
            if self.request_binding != self.cache_candidate.request_binding:
                raise ValueError(
                    "semantic-cache plan request binding must match the candidate"
                )
            if self.cache_candidate_assessment != (
                CacheCandidateAssessment.from_candidate(self.cache_candidate)
            ):
                raise ValueError(
                    "semantic-cache candidate must match its planner assessment"
                )
            cache_reason_codes = {
                reason
                for reason in self.reason_codes
                if reason.value.startswith("CACHE_")
                or reason is PlannerReasonCode.SEMANTIC_CACHE_DISABLED
            }
            if cache_reason_codes != {PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH}:
                raise ValueError(
                    "semantic-cache reuse requires exactly one accepted cache reason"
                )
            if any(
                policy is not None
                for policy in (
                    self.decision_evidence.base_model_policy,
                    self.decision_evidence.final_model_policy,
                )
            ):
                raise ValueError("semantic-cache evidence cannot contain model policy")
            return self

        if self.cache_candidate is not None:
            raise ValueError("model-executed plans cannot carry a cache candidate")
        if self.context_policy is ContextPolicy.NOT_APPLICABLE:
            raise ValueError("model-executed plans require a context policy")
        if self.decision_evidence.base_model_policy is None:
            raise ValueError("model-executed plans require a base model policy")
        if self.decision_evidence.final_model_policy is not self.model_policy:
            raise ValueError("final evidence policy must match the execution plan")
        if self.model_policy is ModelPolicy.SMALL_FIRST_WITH_FALLBACK:
            if self.initial_model_role is not ModelRole.SMALL:
                raise ValueError("small-first plans must start with SMALL")
            if not self.verification_required:
                raise ValueError("small-first plans require verification")
            if self.escalation_model_role is not ModelRole.STRONG:
                raise ValueError("small-first plans require a STRONG fallback")
            return self
        if self.model_policy is ModelPolicy.STRONG_DIRECT:
            if self.initial_model_role is not ModelRole.STRONG:
                raise ValueError("strong-direct plans must start with STRONG")
            if not self.verification_required:
                raise ValueError("strong-direct plans require verification")
            if self.escalation_model_role is not None:
                raise ValueError("strong-direct plans cannot contain a fallback")
            return self
        raise ValueError("model-executed plans require a Planner V1 model policy")


class ExecutionStep(ImmutableModel):
    """One ordered actual fact emitted while executing a plan."""

    sequence: Annotated[int, Field(strict=True, ge=0)]
    step_type: ExecutionStepType
    status: ExecutionStatus
    latency_ms: NonNegativeMilliseconds
    event_codes: tuple[ExecutionEventCode, ...] = ()
    facts: ImmutableJsonObject = Field(default_factory=dict)
    semantic_cache: SemanticCacheEvidence | None = None
    context_reduction: ContextReductionEvidence | None = None
    context_source: ContextSource | None = None
    error: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_error_state(self) -> "ExecutionStep":
        """Preserve errors for failed steps without inventing them for success."""
        has_error = self.error is not None
        requires_error = self.status in {
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT,
        }
        if has_error != requires_error:
            raise ValueError("failed or timed-out steps require an error exclusively")
        if self.step_type is ExecutionStepType.CONTEXT_REDUCTION:
            if (
                self.context_reduction is None
                or self.semantic_cache is not None
                or self.context_source is not None
            ):
                raise ValueError(
                    "context-reduction steps require reduction evidence exclusively"
                )
            applied = self.context_reduction.outcome is ContextReductionOutcome.APPLIED
            if applied is not (self.status is ExecutionStatus.SUCCEEDED):
                raise ValueError("reduction outcome and step status must agree")
            return self
        if self.context_reduction is not None:
            raise ValueError(
                "only context-reduction steps can carry reduction evidence"
            )
        if self.step_type is ExecutionStepType.SEMANTIC_CACHE:
            if self.semantic_cache is None or self.context_source is not None:
                raise ValueError(
                    "semantic-cache steps require cache evidence exclusively"
                )
            contract = semantic_cache_outcome_contract(self.semantic_cache.outcome)
            if contract.step_status is None:
                raise ValueError("cache bypass cannot produce an execution step")
            if self.status is not contract.step_status:
                raise ValueError("cache outcome and execution status must agree")
            if self.event_codes != contract.step_event_codes:
                raise ValueError("cache outcome and event codes must agree exactly")
            if self.latency_ms != self.semantic_cache.lookup_latency_ms:
                raise ValueError("cache step latency must match lookup evidence")
            if self.error != self.semantic_cache.error:
                raise ValueError("cache step error must match lookup evidence")
            return self
        if self.semantic_cache is not None:
            raise ValueError("only semantic-cache steps can carry cache evidence")
        if self.context_source is not None and (
            self.step_type is not ExecutionStepType.MODEL_CALL
        ):
            raise ValueError("only model-call steps can identify effective context")
        return self


def validate_semantic_cache_binding(
    *,
    plan: ExecutionPlan,
    cache_eligible: bool,
    evidence: SemanticCacheEvidence | None,
    run_id: str,
    minimum_quality_score: float,
    request_binding: RequestBinding | None = None,
) -> SemanticCacheOutcomeContract | None:
    """Validate one plan/profile/cache-evidence boundary consistently."""
    plan_evidence = plan.decision_evidence
    module_enabled = plan_evidence.module_states.semantic_cache_enabled
    if evidence is None:
        if module_enabled and cache_eligible:
            raise ValueError(
                "enabled cache-eligible plans require cache outcome evidence"
            )
        if plan.cache_policy is CachePolicy.USE_CACHED_RESULT:
            raise ValueError("cache reuse requires cache outcome evidence")
        return None

    contract = semantic_cache_outcome_contract(evidence.outcome)
    if evidence.planner_reason_code not in plan.reason_codes:
        raise ValueError("cache evidence reason must appear in the selected plan")
    if module_enabled is not contract.required_module_enabled:
        raise ValueError("cache outcome must match the planned module state")
    if (
        contract.required_cache_eligible is not None
        and cache_eligible is not contract.required_cache_eligible
    ):
        raise ValueError("cache outcome must match profile eligibility")
    if plan_evidence.cache_candidate_assessed is not contract.candidate_assessed:
        raise ValueError("cache outcome must match candidate-assessed evidence")
    if plan.cache_policy is not contract.cache_policy:
        raise ValueError("cache outcome must match the selected cache policy")

    assessment = plan.cache_candidate_assessment
    if contract.candidate_assessed:
        if assessment is None or evidence.candidate_assessment != assessment:
            raise ValueError(
                "cache evidence must match the planner candidate assessment"
            )
        if request_binding is None:
            raise ValueError("candidate assessment requires current request binding")
        expected_reason = _cache_assessment_reason(
            assessment=assessment,
            request_binding=request_binding,
            similarity_threshold=plan_evidence.cache_similarity_threshold,
            minimum_quality_score=minimum_quality_score,
        )
        if evidence.planner_reason_code is not expected_reason:
            raise ValueError("cache evidence must match candidate assessment outcome")
    elif assessment is not None or evidence.candidate_assessment is not None:
        raise ValueError("unassessed cache outcomes cannot carry candidate assessment")

    if evidence.outcome is not SemanticCacheOutcome.REUSED:
        return contract

    candidate = plan.cache_candidate
    if candidate is None:
        raise ValueError("cache reuse requires a bound candidate")
    if request_binding is None or plan.request_binding is None:
        raise ValueError("cache reuse requires a current request binding")
    if (
        plan.request_binding != candidate.request_binding
        or request_binding != plan.request_binding
    ):
        raise ValueError("cache reuse request binding must match the bound candidate")
    if run_id == candidate.source_run_id:
        raise ValueError("current run cannot be its own cache source")
    if (
        evidence.source_run_id != candidate.source_run_id
        or evidence.similarity != candidate.similarity
        or evidence.prior_evaluation != candidate.prior_evaluation
    ):
        raise ValueError("cache evidence must match the bound candidate")
    source = candidate.prior_evaluation
    if not (
        source.evaluator_valid
        and source.passed
        and source.mandatory_checks_passed
        and candidate.similarity >= plan_evidence.cache_similarity_threshold
        and source.score >= minimum_quality_score
        and candidate.contract_compatible
        and candidate.safe_to_reuse
    ):
        raise ValueError("bound cache candidate does not satisfy reuse gates")
    return contract


def _cache_assessment_reason(
    *,
    assessment: CacheCandidateAssessment,
    request_binding: RequestBinding,
    similarity_threshold: float,
    minimum_quality_score: float,
) -> PlannerReasonCode:
    """Return the first controlling Planner V1 cache-assessment reason."""
    source = assessment.prior_evaluation
    if assessment.request_binding != request_binding:
        return PlannerReasonCode.CACHE_REQUEST_BINDING_MISMATCH
    if assessment.similarity < similarity_threshold:
        return PlannerReasonCode.CACHE_SIMILARITY_BELOW_THRESHOLD
    if not source.evaluator_valid:
        return PlannerReasonCode.CACHE_PRIOR_EVALUATOR_INVALID
    if not source.passed:
        return PlannerReasonCode.CACHE_PRIOR_EVALUATION_FAILED
    if source.score < minimum_quality_score:
        return PlannerReasonCode.CACHE_QUALITY_BELOW_CONTRACT_THRESHOLD
    if not assessment.contract_compatible:
        return PlannerReasonCode.CACHE_CONTRACT_INCOMPATIBLE
    if not assessment.safe_to_reuse:
        return PlannerReasonCode.CACHE_REUSE_UNSAFE
    return PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH
