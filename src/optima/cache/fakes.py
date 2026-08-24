"""Deterministic semantic-cache implementations for tests and local demos."""

from collections.abc import Iterable
from dataclasses import dataclass

from optima.cache.contracts import (
    SemanticCacheLookupRequest,
    SemanticCacheLookupResult,
)
from optima.domain.cache import CacheCandidate
from optima.domain.request_binding import RequestBinding


class FakeSemanticCache:
    """Return configured outcomes while recording detached lookup requests."""

    def __init__(
        self,
        outcomes: Iterable[
            CacheCandidate | None | SemanticCacheLookupResult | Exception
        ],
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
    ) -> SemanticCacheLookupResult:
        """Record one lookup and return or raise the next configured outcome."""
        self._calls.append(
            SemanticCacheLookupRequest.model_validate(request.model_dump(mode="python"))
        )
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, SemanticCacheLookupResult):
            return SemanticCacheLookupResult.model_validate(outcome)
        candidate = (
            CacheCandidate.model_validate(outcome) if outcome is not None else None
        )
        return SemanticCacheLookupResult(candidate=candidate)


@dataclass(frozen=True)
class InMemoryCacheEntry:
    """One exact request key mapped to a resolved semantic-cache candidate."""

    request_binding: RequestBinding
    candidate: CacheCandidate

    def __post_init__(self) -> None:
        if self.request_binding != self.candidate.request_binding:
            raise ValueError("cache entry binding must match its candidate")


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
    ) -> SemanticCacheLookupResult:
        """Return the exact matching entry, or a truthful miss."""
        self._calls.append(
            SemanticCacheLookupRequest.model_validate(request.model_dump(mode="python"))
        )
        for entry in self._entries:
            if entry.request_binding == request.request_binding:
                return SemanticCacheLookupResult(
                    candidate=CacheCandidate.model_validate(entry.candidate)
                )
        return SemanticCacheLookupResult()
