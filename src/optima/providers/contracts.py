"""Provider-independent model request and response contracts."""

from collections.abc import Mapping
from time import perf_counter
from typing import Annotated, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from optima.domain.execution import ModelRole
from optima.domain.run import ModelUsage

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]


@runtime_checkable
class MonotonicClock(Protocol):
    """Clock abstraction used for monotonic latency measurement."""

    def now(self) -> float:
        """Return current monotonic time in seconds."""


def system_monotonic_time() -> float:
    """Default monotonic clock bound to the standard library timer."""
    return perf_counter()


class ModelProviderRequest(BaseModel):
    """Provider-independent request facts for one model call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: NonEmptyString
    model_role: ModelRole
    input_text: NonEmptyString
    metadata: Mapping[str, str] = Field(default_factory=dict)


class ModelProviderResult(BaseModel):
    """Provider-independent result for one model call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_text: NonEmptyString
    usage: ModelUsage


class ModelProviderCall(BaseModel):
    """Recorded provider call facts for deterministic test assertions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: NonNegativeCount
    request: ModelProviderRequest
    result: ModelProviderResult


@runtime_checkable
class ModelProvider(Protocol):
    """Asynchronous model provider abstraction for conceptual model roles."""

    provider_name: str
    deployment_name: str
    model_role: ModelRole

    async def generate(self, request: ModelProviderRequest) -> ModelProviderResult:
        """Execute one model call and return provider-independent facts."""


class _RequestUsageBoundary(BaseModel):
    """Internal validator shared by fake providers to enforce role/run alignment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: NonEmptyString
    model_role: ModelRole
    usage: ModelUsage

    @model_validator(mode="after")
    def validate_usage_alignment(self) -> "_RequestUsageBoundary":
        """Prevent a provider from reporting usage for the wrong run or role."""
        if self.usage.run_id != self.run_id:
            raise ValueError("usage run_id must match the request run_id")
        if self.usage.model_role is not self.model_role:
            raise ValueError("usage model_role must match the request model_role")
        return self


def validate_usage_alignment(
    *,
    run_id: str,
    model_role: ModelRole,
    usage: ModelUsage,
) -> None:
    """Validate that usage facts align to the request boundary."""
    _RequestUsageBoundary(run_id=run_id, model_role=model_role, usage=usage)
