"""Request-aware safety policy for deterministic local context reduction."""

from typing import Annotated, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from optima.context.deterministic import (
    context_segments,
    extract_task_terms,
    is_retainable_segment,
)
from optima.domain.request_profile import Complexity, TaskType

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
StrictBoolean = Annotated[bool, Field(strict=True)]


class ContextReducerSafetyRequest(BaseModel):
    """Current request facts needed to assess deterministic reducer safety."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_text: NonEmptyString
    context: NonEmptyString
    task_type: TaskType
    complexity: Complexity


class ContextReducerSafetyDecision(BaseModel):
    """Typed task-safety result from one named local policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_safe: StrictBoolean
    policy_name: NonEmptyString


@runtime_checkable
class ContextReducerSafetyPolicy(Protocol):
    """Assess reducer safety independently for each current request."""

    policy_name: str

    def evaluate(
        self,
        request: ContextReducerSafetyRequest,
    ) -> ContextReducerSafetyDecision:
        """Return a conservative task-safety decision for the request."""


class DeterministicExtractiveSafetyPolicy:
    """Allow only low-complexity summaries that remove exact duplicate lines."""

    policy_name = "local-deterministic-deduplication-v1"

    def evaluate(
        self,
        request: ContextReducerSafetyRequest,
    ) -> ContextReducerSafetyDecision:
        """Approve only when reduction cannot discard a unique source line."""
        return ContextReducerSafetyDecision(
            task_safe=self._is_supported(request),
            policy_name=self.policy_name,
        )

    def _is_supported(self, request: ContextReducerSafetyRequest) -> bool:
        if request.task_type is not TaskType.SUMMARIZATION:
            return False
        if request.complexity is not Complexity.LOW:
            return False

        segments = context_segments(request.context)
        if len(segments) < 2:
            return False

        task_terms = extract_task_terms(request.input_text)
        seen: set[str] = set()
        duplicate_found = False
        for segment in segments:
            if segment in seen:
                duplicate_found = True
                continue
            seen.add(segment)
            if not is_retainable_segment(segment, task_terms):
                return False
        return duplicate_found
