"""Execution plan structure and actual execution-step facts."""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from optima.domain.quality_contract import OptimizationMode, QualityScore, RiskTier

NonNegativeMilliseconds = Annotated[int, Field(strict=True, ge=0)]
NonNegativeDecimal = Annotated[
    Decimal,
    Field(ge=Decimal("0"), allow_inf_nan=False),
]
NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
StrictBoolean = Annotated[bool, Field(strict=True)]


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


class PlannerReasonCode(StrEnum):
    """Reason codes currently defined by Planner V1 specifications."""

    SEMANTIC_CACHE_DISABLED = "SEMANTIC_CACHE_DISABLED"
    CACHE_REQUEST_NOT_ELIGIBLE = "CACHE_REQUEST_NOT_ELIGIBLE"
    CACHE_CANDIDATE_NOT_SUPPLIED = "CACHE_CANDIDATE_NOT_SUPPLIED"
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


class PlannerModuleStates(BaseModel):
    """Immutable optional-module state captured when planning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    semantic_cache_enabled: StrictBoolean
    context_reduction_enabled: StrictBoolean
    historical_policy_enabled: StrictBoolean
    foundry_router_comparator_enabled: StrictBoolean


class HistoricalDecisionEvidence(BaseModel):
    """Comparable historical facts considered by Planner V1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    comparable_sample_count: NonNegativeCount
    small_pass_without_escalation_rate: QualityScore
    average_final_quality: QualityScore
    disposition: HistoricalEvidenceDisposition


class PlannerDecisionEvidence(BaseModel):
    """Typed pre-execution evidence supporting one planner result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_risk_tier: RiskTier
    contract_risk_tier: RiskTier
    effective_risk_tier: RiskTier
    module_states: PlannerModuleStates
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


class ExecutionPlan(BaseModel):
    """Provider-independent pre-execution plan selected by Planner V1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cache_policy: CachePolicy
    context_policy: ContextPolicy
    model_policy: ModelPolicy | None = None
    initial_model_role: ModelRole | None = None
    verification_required: Annotated[bool, Field(strict=True)]
    escalation_model_role: ModelRole | None = None
    optimization_mode: OptimizationMode
    reason_codes: Annotated[tuple[PlannerReasonCode, ...], Field(min_length=1)]
    human_readable_name: NonEmptyString
    decision_evidence: PlannerDecisionEvidence
    expected_quality_score: QualityScore | None = None
    expected_cost: NonNegativeDecimal | None = None

    @model_validator(mode="after")
    def validate_plan_shape(self) -> "ExecutionPlan":
        """Enforce cache, small-first, strong-direct, and reason-code invariants."""
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason codes must be unique")

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
            if any(
                policy is not None
                for policy in (
                    self.decision_evidence.base_model_policy,
                    self.decision_evidence.final_model_policy,
                )
            ):
                raise ValueError("semantic-cache evidence cannot contain model policy")
            return self

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


class ExecutionStep(BaseModel):
    """One ordered actual fact emitted while executing a plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: Annotated[int, Field(strict=True, ge=0)]
    step_type: ExecutionStepType
    status: ExecutionStatus
    latency_ms: NonNegativeMilliseconds
    event_codes: tuple[ExecutionEventCode, ...] = ()
    facts: dict[str, JsonValue] = Field(default_factory=dict)
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
        return self
