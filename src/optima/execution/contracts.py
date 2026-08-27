"""Typed request facts consumed by the plan executor."""

from typing import Annotated

from pydantic import Field, model_validator

from optima.domain.execution import (
    ExecutionPlan,
    SemanticCacheEvidence,
    validate_semantic_cache_binding,
)
from optima.domain.quality_contract import QualityContract
from optima.domain.request_binding import build_request_binding
from optima.domain.request_profile import RequestProfile
from optima.immutable import ImmutableJsonObject, ImmutableModel

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]


class ExecutionRequest(ImmutableModel):
    """Complete immutable facts needed to execute one selected plan."""

    run_id: NonEmptyString
    correlation_id: NonEmptyString
    input_text: NonEmptyString
    context: NonEmptyString | None = None
    reference_output: NonEmptyString | None = None
    criteria: tuple[NonEmptyString, ...] = ()
    metadata: ImmutableJsonObject = Field(default_factory=dict)
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
        current_binding = build_request_binding(
            input_text=self.input_text,
            context=self.context,
            reference_output=self.reference_output,
            criteria=self.criteria,
            metadata=self.metadata,
            task_type=self.request_profile.task_type,
            complexity=self.request_profile.complexity,
        )
        if plan.request_binding != current_binding:
            raise ValueError(
                "execution plan request binding must match current request"
            )
        if plan.quality_profile is not self.quality_contract.quality_profile:
            raise ValueError("execution plan must match the current Quality Contract")
        if (
            plan.decision_evidence.profile_risk_tier
            is not self.request_profile.risk_tier
            or plan.decision_evidence.contract_risk_tier
            is not self.quality_contract.risk_tier
        ):
            raise ValueError("execution plan risk evidence must match current facts")
        validate_semantic_cache_binding(
            plan=plan,
            cache_eligible=self.request_profile.cache_eligible,
            evidence=self.semantic_cache,
            run_id=self.run_id,
            minimum_quality_score=self.quality_contract.minimum_quality_score,
            request_binding=current_binding,
            grounding_required=self.quality_contract.grounding_required,
        )
        return self


class UnsupportedExecutionPlanError(ValueError):
    """Raised when a selected plan contains unsupported runtime structure."""


class ContextReductionDependencyError(ValueError):
    """Raised when a REDUCE plan lacks required runtime dependencies or context."""
