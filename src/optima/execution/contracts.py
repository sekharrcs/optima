"""Typed request facts consumed by the plan executor."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from optima.domain.execution import ExecutionPlan
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


class UnsupportedExecutionPlanError(ValueError):
    """Raised when a selected plan contains runtime outside Slice 5."""
