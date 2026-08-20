"""Semantic-cache lookup contracts independent of storage providers."""

from typing import Annotated, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from optima.domain.cache import CacheCandidate
from optima.domain.quality_contract import QualityContract
from optima.domain.request_profile import RequestProfile

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]


class SemanticCacheLookupRequest(BaseModel):
    """Immutable request facts available to one semantic-cache lookup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: NonEmptyString
    input_text: NonEmptyString
    context: NonEmptyString | None = None
    quality_contract: QualityContract
    request_profile: RequestProfile
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


@runtime_checkable
class SemanticCache(Protocol):
    """Resolve at most one candidate without making planner decisions."""

    async def lookup(
        self,
        request: SemanticCacheLookupRequest,
    ) -> CacheCandidate | None:
        """Return one resolved candidate or a truthful miss."""
        ...
