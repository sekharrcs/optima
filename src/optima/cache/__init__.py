"""Provider-independent semantic-cache contracts and local implementations."""

from optima.cache.contracts import SemanticCache, SemanticCacheLookupRequest
from optima.cache.fakes import (
    FakeSemanticCache,
    InMemoryCacheEntry,
    InMemorySemanticCache,
)

__all__ = [
    "FakeSemanticCache",
    "InMemoryCacheEntry",
    "InMemorySemanticCache",
    "SemanticCache",
    "SemanticCacheLookupRequest",
]
