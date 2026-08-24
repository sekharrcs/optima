"""Provider-independent semantic-cache contracts and local implementations."""

from optima.cache.azure_redis import (
    AZURE_MANAGED_REDIS_PORT,
    AZURE_MANAGED_REDIS_SCOPE,
    AzureRedisCredentialProvider,
    AzureRedisToken,
    RedisSemanticCacheResources,
    build_redis_semantic_cache_resources,
)
from optima.cache.contracts import SemanticCache, SemanticCacheLookupRequest
from optima.cache.fakes import (
    FakeSemanticCache,
    InMemoryCacheEntry,
    InMemorySemanticCache,
)
from optima.cache.redis import (
    RedisSearchClient,
    RedisSemanticCache,
    RedisSemanticCacheInvalidResponseError,
    SemanticCacheEmbeddingProvider,
)

__all__ = [
    "AZURE_MANAGED_REDIS_PORT",
    "AZURE_MANAGED_REDIS_SCOPE",
    "AzureRedisCredentialProvider",
    "AzureRedisToken",
    "FakeSemanticCache",
    "InMemoryCacheEntry",
    "InMemorySemanticCache",
    "RedisSearchClient",
    "RedisSemanticCache",
    "RedisSemanticCacheInvalidResponseError",
    "RedisSemanticCacheResources",
    "SemanticCache",
    "SemanticCacheEmbeddingProvider",
    "SemanticCacheLookupRequest",
    "build_redis_semantic_cache_resources",
]
