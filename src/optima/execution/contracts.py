"""Typed request facts consumed by the plan executor."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from optima.domain.execution import (
    CachePolicy,
    ExecutionPlan,
    SemanticCacheEvidence,
    SemanticCacheOutcome,
)
from optima.domain.quality_contract import QualityContract
from optima.domain.request_profile import RequestProfile

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]


class ExecutionRequest(BaseModel):
    """Complete immutable facts needed to execute one selected plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: NonEmptyString
    correlation_id: NonEmptyString
    input_text: NonEmptyString
    context: NonEmptyString | None = None
    reference_output: NonEmptyString | None = None
    criteria: tuple[NonEmptyString, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    quality_contract: QualityContract
    request_profile: RequestProfile
    execution_plan: ExecutionPlan
    semantic_cache: SemanticCacheEvidence | None = None

    @model_validator(mode="after")
    def validate_plan_binding(self) -> "ExecutionRequest":
        """Bind the selected plan to current request and exact cache evidence."""
        plan = self.execution_plan
        if plan.optimization_mode is not self.quality_contract.optimization_mode:
            raise ValueError("plan optimization mode must match the current contract")
        if self.semantic_cache is not None and (
            self.semantic_cache.planner_reason_code not in plan.reason_codes
        ):
            raise ValueError("cache evidence reason must appear in the selected plan")
        if self.semantic_cache is not None:
            self._validate_cache_outcome_binding(self.semantic_cache)
        if plan.cache_policy is CachePolicy.USE_CACHED_RESULT:
            candidate = plan.cache_candidate
            evidence = self.semantic_cache
            if candidate is None or evidence is None:
                raise ValueError("cache reuse requires bound candidate and evidence")
            if evidence.outcome is not SemanticCacheOutcome.REUSED:
                raise ValueError("cache plan requires a reused runtime outcome")
            if not self.request_profile.cache_eligible:
                raise ValueError("cache reuse requires an eligible request profile")
            if not plan.decision_evidence.module_states.semantic_cache_enabled:
                raise ValueError("cache reuse requires the module to be enabled")
            if self.run_id == candidate.source_run_id:
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
                and candidate.similarity
                >= plan.decision_evidence.cache_similarity_threshold
                and source.score >= self.quality_contract.minimum_quality_score
                and candidate.contract_compatible
                and candidate.safe_to_reuse
            ):
                raise ValueError("bound cache candidate does not satisfy reuse gates")
            return self
        if self.semantic_cache is not None and (
            self.semantic_cache.outcome is SemanticCacheOutcome.REUSED
        ):
            raise ValueError("model plans cannot claim cache reuse")
        return self

    def _validate_cache_outcome_binding(
        self,
        evidence: SemanticCacheEvidence,
    ) -> None:
        """Align runtime outcome with the planner's module and assessment facts."""
        plan_evidence = self.execution_plan.decision_evidence
        disabled = evidence.outcome is SemanticCacheOutcome.DISABLED_BYPASSED
        if disabled is plan_evidence.module_states.semantic_cache_enabled:
            raise ValueError("cache outcome must match the planned module state")
        assessed = evidence.outcome in {
            SemanticCacheOutcome.MATCH_REJECTED,
            SemanticCacheOutcome.REUSED,
        }
        if assessed is not plan_evidence.cache_candidate_assessed:
            raise ValueError("cache outcome must match candidate-assessed evidence")


class UnsupportedExecutionPlanError(ValueError):
    """Raised when a selected plan contains unsupported runtime structure."""


class ContextReductionDependencyError(ValueError):
    """Raised when a REDUCE plan lacks required runtime dependencies or context."""
