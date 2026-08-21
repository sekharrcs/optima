"""Provider-independent context reduction and token measurement contracts."""

from typing import Annotated, Protocol, runtime_checkable

from pydantic import Field, model_validator

from optima.immutable import ImmutableModel

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]
StrictBoolean = Annotated[bool, Field(strict=True)]


class ContextReductionRequest(ImmutableModel):
    """Original task and context supplied to one reducer invocation."""

    run_id: NonEmptyString
    input_text: NonEmptyString
    context: NonEmptyString


class ContextPreservationEvidence(ImmutableModel):
    """Deterministic source-segment evidence emitted by an extractive reducer."""

    source_order_preserved: StrictBoolean
    original_segment_count: PositiveCount
    retained_segment_indexes: Annotated[
        tuple[NonNegativeCount, ...], Field(min_length=1)
    ]
    removed_duplicate_count: NonNegativeCount
    removed_irrelevant_count: NonNegativeCount
    task_terms_used: tuple[NonEmptyString, ...] = ()

    @model_validator(mode="after")
    def validate_segment_accounting(self) -> "ContextPreservationEvidence":
        """Require ordered, unique, in-range indexes and complete accounting."""
        indexes = self.retained_segment_indexes
        if tuple(sorted(set(indexes))) != indexes:
            raise ValueError("retained segment indexes must be unique and ascending")
        if indexes[-1] >= self.original_segment_count:
            raise ValueError("retained segment indexes must reference source segments")
        if (
            len(indexes) + self.removed_duplicate_count + self.removed_irrelevant_count
            != self.original_segment_count
        ):
            raise ValueError("segment counts must account for every source segment")
        return self


class ContextReductionResult(ImmutableModel):
    """Reduced context plus reducer-reported measured evidence."""

    reduced_context: NonEmptyString
    original_token_count: PositiveCount
    reduced_token_count: PositiveCount
    reducer_name: NonEmptyString
    method: NonEmptyString
    token_counter_name: NonEmptyString
    preservation: ContextPreservationEvidence

    @model_validator(mode="after")
    def validate_measured_reduction(self) -> "ContextReductionResult":
        """Reject results that claim reduction without fewer measured tokens."""
        if self.reduced_token_count >= self.original_token_count:
            raise ValueError("reduced context must contain fewer measured tokens")
        return self


@runtime_checkable
class TokenCounter(Protocol):
    """Authoritative provider-neutral token measurement boundary."""

    counter_name: str

    def count(self, text: str) -> int:
        """Count tokens in one string using the named deterministic method."""


@runtime_checkable
class ContextReducer(Protocol):
    """Asynchronous provider-independent context reduction boundary."""

    reducer_name: str

    async def reduce(
        self,
        request: ContextReductionRequest,
    ) -> ContextReductionResult:
        """Return reduced context with measured preservation evidence."""
