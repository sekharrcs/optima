"""Immutable typed inputs and intermediate decisions for Planner V1."""

from enum import StrEnum
from typing import Annotated

from pydantic import Field, model_validator

from optima.domain.cache import CacheCandidate as CacheCandidate
from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    ExecutionPlan,
    HistoricalDecisionEvidence,
    ModelPolicy,
    PlannerDecisionEvidence,
    PlannerReasonCode,
)
from optima.domain.quality_contract import QualityContract, QualityScore
from optima.domain.request_binding import RequestBinding
from optima.domain.request_profile import RequestProfile
from optima.immutable import ImmutableModel

StrictBoolean = Annotated[bool, Field(strict=True)]
NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]
Rate = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]


class ModuleConfiguration(ImmutableModel):
    """Optional optimizer-module states supplied to Planner V1."""

    semantic_cache_enabled: StrictBoolean
    context_reduction_enabled: StrictBoolean
    historical_policy_enabled: StrictBoolean
    foundry_router_comparator_enabled: StrictBoolean


class PlannerThresholds(ImmutableModel):
    """Typed configurable thresholds used by deterministic policies."""

    cache_similarity_threshold: Rate = 0.95
    context_reduction_consider_tokens: PositiveCount = 4_000
    context_reduction_required_tokens: PositiveCount = 8_000
    history_minimum_samples: PositiveCount = 20
    history_small_prefer_pass_rate: Rate = 0.95
    history_small_avoid_pass_rate: Rate = 0.70

    @model_validator(mode="after")
    def validate_threshold_order(self) -> "PlannerThresholds":
        """Require coherent context and historical-policy thresholds."""
        if (
            self.context_reduction_consider_tokens
            > self.context_reduction_required_tokens
        ):
            raise ValueError("context consider tokens must not exceed required tokens")
        if self.history_small_avoid_pass_rate >= self.history_small_prefer_pass_rate:
            raise ValueError("history avoid rate must be below prefer rate")
        return self


class ContextReducerCapability(ImmutableModel):
    """Configured reducer availability and task-specific safety facts."""

    available: StrictBoolean
    task_safe: StrictBoolean
    approved_for_critical_high_risk: StrictBoolean


class HistoricalPolicyStatistics(ImmutableModel):
    """Comparable aggregate evidence supplied without storage concerns."""

    comparable_sample_count: NonNegativeCount
    small_pass_without_escalation_rate: Rate
    average_final_quality: QualityScore


class PlannerCapabilities(ImmutableModel):
    """Conceptual execution capabilities available to satisfy a plan."""

    small_model_configured: StrictBoolean = True
    strong_model_configured: StrictBoolean = True
    evaluator_configured: StrictBoolean = True


class PlannerInput(ImmutableModel):
    """Complete validated input to deterministic Planner V1 selection."""

    request_profile: RequestProfile
    request_binding: RequestBinding
    quality_contract: QualityContract
    modules: ModuleConfiguration
    thresholds: PlannerThresholds = Field(default_factory=PlannerThresholds)
    reducer_capability: ContextReducerCapability
    capabilities: PlannerCapabilities = Field(default_factory=PlannerCapabilities)
    cache_candidate: CacheCandidate | None = None
    historical_statistics: HistoricalPolicyStatistics | None = None

    @model_validator(mode="after")
    def validate_request_binding(self) -> "PlannerInput":
        """Bind planner profile identity to the canonical request digest."""
        if (
            self.request_binding.task_type is not self.request_profile.task_type
            or self.request_binding.complexity is not self.request_profile.complexity
        ):
            raise ValueError("request binding must match the planner request profile")
        return self


class CacheDecision(ImmutableModel):
    """Pure semantic-cache policy output."""

    policy: CachePolicy
    candidate_assessed: StrictBoolean
    reason_code: PlannerReasonCode


class ContextDecision(ImmutableModel):
    """Pure context policy output."""

    policy: ContextPolicy
    reason_codes: tuple[PlannerReasonCode, ...]


class ModelDecision(ImmutableModel):
    """Pure base model policy output."""

    policy: ModelPolicy
    reason_codes: tuple[PlannerReasonCode, ...]


class HistoricalDecision(ImmutableModel):
    """Bounded historical-policy output."""

    final_policy: ModelPolicy
    reason_codes: tuple[PlannerReasonCode, ...]
    evidence: HistoricalDecisionEvidence | None = None


class PlanningFailureCode(StrEnum):
    """Structural reasons Planner V1 cannot form a compliant plan."""

    EVALUATOR_NOT_CONFIGURED = "EVALUATOR_NOT_CONFIGURED"
    STRONG_MODEL_NOT_CONFIGURED = "STRONG_MODEL_NOT_CONFIGURED"
    INITIAL_MODEL_NOT_CONFIGURED = "INITIAL_MODEL_NOT_CONFIGURED"


class PlanningFailure(ImmutableModel):
    """Typed failure returned instead of a knowingly invalid plan."""

    code: PlanningFailureCode
    message: NonEmptyString
    decision_evidence: PlannerDecisionEvidence


type PlannerResult = ExecutionPlan | PlanningFailure
