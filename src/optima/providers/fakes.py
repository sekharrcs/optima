"""Deterministic fake model providers for local tests and development."""

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from optima.domain.execution import ModelRole
from optima.domain.run import ModelUsage
from optima.providers.contracts import (
    ModelProvider,
    ModelProviderCall,
    ModelProviderRequest,
    ModelProviderResult,
    MonotonicClock,
    system_monotonic_time,
    validate_usage_alignment,
)

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]


class FakeProviderResponse(BaseModel):
    """One deterministic fake response template."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_text: NonEmptyString
    input_tokens: NonNegativeCount
    output_tokens: NonNegativeCount
    cached_tokens: NonNegativeCount | None = None
    request_id: NonEmptyString | None = None
    calculated_cost: Decimal | None = None


class FakeModelProvider(ModelProvider):
    """Deterministic in-memory provider with repeatable responses."""

    provider_name: str
    deployment_name: str
    model_role: ModelRole

    def __init__(
        self,
        *,
        provider_name: str,
        deployment_name: str,
        model_role: ModelRole,
        responses: tuple[FakeProviderResponse, ...],
        clock: MonotonicClock | None = None,
    ) -> None:
        if not responses:
            raise ValueError("fake providers require at least one configured response")
        self.provider_name = provider_name
        self.deployment_name = deployment_name
        self.model_role = model_role
        self._responses = responses
        self._clock = clock
        self._call_index = 0
        self._calls: list[ModelProviderCall] = []

    @property
    def calls(self) -> tuple[ModelProviderCall, ...]:
        """Return calls in the exact order they were executed."""
        return tuple(self._calls)

    async def generate(self, request: ModelProviderRequest) -> ModelProviderResult:
        """Return the next deterministic response and measured usage facts."""
        if request.model_role is not self.model_role:
            raise ValueError(
                "request model role does not match provider role: "
                f"expected {self.model_role.value}, got {request.model_role.value}"
            )

        clock_now = (
            self._clock.now if self._clock is not None else system_monotonic_time
        )
        started_at = clock_now()
        response = self._responses[self._call_index % len(self._responses)]
        self._call_index += 1
        finished_at = clock_now()

        latency_ms = max(0, int(round((finished_at - started_at) * 1000)))
        request_id = response.request_id or (
            f"{self.provider_name}-{self.model_role.value.lower()}-{self._call_index}"
        )

        usage = ModelUsage(
            request_id=request_id,
            run_id=request.run_id,
            provider=self.provider_name,
            deployment=self.deployment_name,
            model_role=self.model_role,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cached_tokens=response.cached_tokens,
            latency_ms=latency_ms,
            calculated_cost=response.calculated_cost,
        )
        validate_usage_alignment(
            run_id=request.run_id,
            model_role=request.model_role,
            usage=usage,
        )

        result = ModelProviderResult(output_text=response.output_text, usage=usage)
        self._calls.append(
            ModelProviderCall(
                sequence=len(self._calls),
                request=request,
                result=result,
            )
        )
        return result


def build_fake_small_provider(
    *,
    provider_name: str,
    deployment_name: str,
    responses: tuple[FakeProviderResponse, ...],
    clock: MonotonicClock | None = None,
) -> FakeModelProvider:
    """Build a deterministic fake provider that always reports SMALL usage."""
    return FakeModelProvider(
        provider_name=provider_name,
        deployment_name=deployment_name,
        model_role=ModelRole.SMALL,
        responses=responses,
        clock=clock,
    )


def build_fake_strong_provider(
    *,
    provider_name: str,
    deployment_name: str,
    responses: tuple[FakeProviderResponse, ...],
    clock: MonotonicClock | None = None,
) -> FakeModelProvider:
    """Build a deterministic fake provider that always reports STRONG usage."""
    return FakeModelProvider(
        provider_name=provider_name,
        deployment_name=deployment_name,
        model_role=ModelRole.STRONG,
        responses=responses,
        clock=clock,
    )
