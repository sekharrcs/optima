"""Semantic-cache lookup contracts independent of storage providers."""

from typing import Annotated, Protocol, runtime_checkable

from pydantic import Field, model_validator

from optima.domain.cache import CacheCandidate
from optima.domain.quality_contract import QualityContract
from optima.domain.request_binding import RequestBinding, build_request_binding
from optima.domain.request_profile import RequestProfile
from optima.immutable import ImmutableJsonObject, ImmutableModel

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]


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
    ) -> CacheCandidate | None:
        """Return one resolved candidate or a truthful miss."""
        ...
