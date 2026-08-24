"""Deterministic fake model providers for local tests and development."""

import hashlib
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from optima.cache.contracts import (
    EmbeddingProviderResult,
    SemanticCacheLookupRequest,
)
from optima.domain.embedding import EmbeddingProfile
from optima.domain.execution import ModelRole
from optima.domain.run import ModelUsage, PricingProvenance
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
    pricing_provenance: PricingProvenance | None = None


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
            pricing_provenance=response.pricing_provenance,
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


class FakeEmbeddingProvider:
    """Deterministic embedding provider for offline tests and local demos."""

    provider_name: str

    def __init__(
        self,
        *,
        profile: EmbeddingProfile,
        provider_name: str = "fake-embedding",
        input_tokens: int | None = None,
        request_id: str | None = None,
    ) -> None:
        self._profile = profile
        self.provider_name = provider_name
        self._input_tokens = input_tokens
        self._request_id = request_id
        self._calls: list[SemanticCacheLookupRequest] = []

    @property
    def profile(self) -> EmbeddingProfile:
        """Return the embedding profile this provider produces."""
        return self._profile

    @property
    def calls(self) -> tuple[SemanticCacheLookupRequest, ...]:
        """Return immutable snapshots of embedded requests received so far."""
        return tuple(self._calls)

    async def embed(
        self,
        request: SemanticCacheLookupRequest,
    ) -> EmbeddingProviderResult:
        """Return a deterministic finite non-zero vector for the request text."""
        self._calls.append(request)
        return EmbeddingProviderResult(
            vector=_deterministic_vector(request.input_text, self._profile.dimension),
            profile=self._profile,
            provider=self.provider_name,
            request_id=self._request_id,
            input_tokens=self._input_tokens,
        )


def _deterministic_vector(text: str, dimension: int) -> tuple[float, ...]:
    """Derive one stable finite non-zero vector from the input text."""
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
        for byte in digest:
            values.append(byte / 255.0 * 2.0 - 1.0)
            if len(values) == dimension:
                break
        counter += 1
    if not any(values):
        values[0] = 1.0
    return tuple(values)
