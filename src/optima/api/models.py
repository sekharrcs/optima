"""Strict HTTP request and structured error contracts."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from optima.domain.quality_contract import OptimizationMode, QualityProfile, RiskTier
from optima.domain.request_profile import RequestProfile

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
PositiveMilliseconds = Annotated[int, Field(strict=True, gt=0)]


class RunRequest(BaseModel):
    """Public input for one planned and measured OPTIMA execution."""

    model_config = ConfigDict(extra="forbid")

    input_text: NonEmptyString
    context: NonEmptyString | None = None
    request_profile: RequestProfile
    quality_profile: QualityProfile
    optimization_mode: OptimizationMode
    risk_tier: RiskTier
    max_latency_ms: PositiveMilliseconds | None = None
    reference_output: NonEmptyString | None = None
    criteria: tuple[NonEmptyString, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ApiError(BaseModel):
    """Stable machine-readable API error detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: NonEmptyString
    message: NonEmptyString
    facts: dict[str, JsonValue] = Field(default_factory=dict)
