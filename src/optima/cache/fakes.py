"""Deterministic semantic-cache implementations for tests and local demos."""

from collections.abc import Iterable
from dataclasses import dataclass

from optima.cache.contracts import SemanticCacheLookupRequest
from optima.domain.cache import CacheCandidate


class FakeSemanticCache:
    """Return configured outcomes while recording detached lookup requests."""

    def __init__(
        self,
        outcomes: Iterable[CacheCandidate | None | Exception],
    ) -> None:
        self._outcomes = iter(outcomes)
        self._calls: list[SemanticCacheLookupRequest] = []

    @property
    def calls(self) -> tuple[SemanticCacheLookupRequest, ...]:
        """Return immutable snapshots of requests received so far."""
        return tuple(self._calls)

    async def lookup(
        self,
        request: SemanticCacheLookupRequest,
    ) -> CacheCandidate | None:
        """Record one lookup and return or raise the next configured outcome."""
        self._calls.append(
            SemanticCacheLookupRequest.model_validate(request.model_dump(mode="python"))
        )
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome.detached_copy() if outcome is not None else None


@dataclass(frozen=True)
class InMemoryCacheEntry:
    """One exact request key mapped to a resolved semantic-cache candidate."""

    input_text: str
    context: str | None
    candidate: CacheCandidate


class InMemorySemanticCache:
    """Resolve deterministic exact-match entries without persistence or writes."""

    def __init__(self, entries: Iterable[InMemoryCacheEntry]) -> None:
        self._entries = tuple(entries)
        self._calls: list[SemanticCacheLookupRequest] = []

    @property
    def calls(self) -> tuple[SemanticCacheLookupRequest, ...]:
        """Return immutable snapshots of requests received so far."""
        return tuple(self._calls)

    async def lookup(
        self,
        request: SemanticCacheLookupRequest,
    ) -> CacheCandidate | None:
        """Return the exact matching entry, or a truthful miss."""
        self._calls.append(
            SemanticCacheLookupRequest.model_validate(request.model_dump(mode="python"))
        )
        for entry in self._entries:
            if (
                entry.input_text == request.input_text
                and entry.context == request.context
            ):
                return entry.candidate.detached_copy()
        return None
