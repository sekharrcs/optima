"""Semantic-cache lookup contracts independent of storage providers."""

import math
from typing import Annotated, Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator

from optima.domain.cache import CacheCandidate
from optima.domain.embedding import EmbeddingAttempt, EmbeddingProfile
from optima.domain.quality_contract import QualityContract
from optima.domain.request_binding import RequestBinding, build_request_binding
from optima.domain.request_profile import RequestProfile
from optima.immutable import ImmutableJsonObject, ImmutableModel

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]


class SemanticCacheLookupRequest(ImmutableModel):
    """Immutable request facts available to one semantic-cache lookup."""

    run_id: NonEmptyString
    input_text: NonEmptyString
    context: NonEmptyString | None = None
    reference_output: NonEmptyString | None = None
    criteria: tuple[NonEmptyString, ...] = ()
    quality_contract: QualityContract
    request_profile: RequestProfile
    metadata: ImmutableJsonObject = Field(default_factory=dict)
    request_binding: RequestBinding

    @model_validator(mode="after")
    def validate_request_binding(self) -> "SemanticCacheLookupRequest":
        """Require the binding to match every generation and evaluation fact."""
        expected = build_request_binding(
            input_text=self.input_text,
            context=self.context,
            reference_output=self.reference_output,
            criteria=self.criteria,
            metadata=self.metadata,
            task_type=self.request_profile.task_type,
            complexity=self.request_profile.complexity,
        )
        if self.request_binding != expected:
            raise ValueError("request binding must match the lookup request")
        return self


@runtime_checkable
class SemanticCache(Protocol):
    """Resolve at most one candidate without making planner decisions."""

    async def lookup(
        self,
        request: SemanticCacheLookupRequest,
    ) -> "SemanticCacheLookupResult":
        """Return one resolved candidate and any embedding usage consumed."""
        ...


class EmbeddingProviderResult(ImmutableModel):
    """One embedding vector with its declared profile and optional usage."""

    vector: tuple[float, ...]
    profile: EmbeddingProfile
    provider: NonEmptyString
    request_id: NonEmptyString | None = None
    input_tokens: NonNegativeCount | None = None

    @field_validator("vector", mode="before")
    @classmethod
    def validate_vector(cls, value: object) -> tuple[float, ...]:
        """Require a non-empty sequence of finite, non-boolean real numbers."""
        if isinstance(value, str | bytes | bytearray):
            raise ValueError("embedding vector must be a numeric sequence")
        try:
            elements = list(value)  # type: ignore[call-overload]
        except TypeError as error:
            raise ValueError("embedding vector must be a numeric sequence") from error
        if not elements:
            raise ValueError("embedding vector must not be empty")
        numbers: list[float] = []
        for element in elements:
            if isinstance(element, bool) or not isinstance(element, int | float):
                raise ValueError("embedding vector values must be numeric")
            number = float(element)
            if not math.isfinite(number):
                raise ValueError("embedding vector values must be finite")
            numbers.append(number)
        return tuple(numbers)


class SemanticCacheLookupResult(ImmutableModel):
    """One resolved candidate and the embedding attempt the lookup made."""

    candidate: CacheCandidate | None = None
    embedding_attempt: EmbeddingAttempt | None = None


class SemanticCacheLookupError(Exception):
    """A cache lookup failure that may carry an already-made embedding attempt."""

    def __init__(
        self,
        embedding_attempt: EmbeddingAttempt | None = None,
        *,
        message: str = "semantic-cache lookup failed",
    ) -> None:
        super().__init__(message)
        self.embedding_attempt = embedding_attempt


class SemanticCacheLookupTimeout(TimeoutError):
    """A cache lookup timeout that may carry an already-made embedding attempt."""

    def __init__(self, embedding_attempt: EmbeddingAttempt | None = None) -> None:
        super().__init__("semantic-cache lookup timed out")
        self.embedding_attempt = embedding_attempt


class EmbeddingProviderError(Exception):
    """An embedding-provider failure recording whether a request went outbound.

    ``outbound_attempted`` is the paid-consumption safety signal: ``False`` means
    the failure provably happened before any outbound provider request, so no
    consumption is possible; ``True`` means the request may have reached the paid
    provider even though no usage was measured.
    """

    def __init__(
        self,
        message: str = "embedding provider request failed",
        *,
        outbound_attempted: bool,
    ) -> None:
        super().__init__(message)
        self.outbound_attempted = outbound_attempted


class EmbeddingProviderTimeout(TimeoutError):
    """An embedding-provider timeout; a timed-out request may have gone outbound."""

    def __init__(
        self,
        message: str = "embedding provider request timed out",
        *,
        outbound_attempted: bool = True,
    ) -> None:
        super().__init__(message)
        self.outbound_attempted = outbound_attempted


@runtime_checkable
class SemanticCacheEmbeddingProvider(Protocol):
    """Produce one provider-independent embedding for a lookup request."""

    @property
    def profile(self) -> EmbeddingProfile:
        """Return the embedding profile this provider produces."""
        ...

    async def embed(
        self,
        request: SemanticCacheLookupRequest,
    ) -> EmbeddingProviderResult:
        """Return the vector, profile, and usage for one lookup request."""
        ...
