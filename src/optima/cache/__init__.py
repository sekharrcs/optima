"""Provider-independent semantic-cache contracts and local implementations."""

from optima.cache.azure_redis import (
    AZURE_MANAGED_REDIS_PORT,
    AZURE_MANAGED_REDIS_SCOPE,
    AzureRedisCredentialProvider,
    AzureRedisToken,
    RedisSemanticCacheResources,
    build_redis_semantic_cache_resources,
)
from optima.cache.bootstrap import (
    RedisIndexCompatibilityError,
    ensure_redis_semantic_cache_index,
    redis_index_bootstrap_lock_key,
    redis_index_contract_key,
)
from optima.cache.contracts import (
    EmbeddingProviderError,
    EmbeddingProviderResult,
    EmbeddingProviderTimeout,
    SemanticCache,
    SemanticCacheEmbeddingProvider,
    SemanticCacheLookupError,
    SemanticCacheLookupRequest,
    SemanticCacheLookupResult,
    SemanticCacheLookupTimeout,
)
from optima.cache.fakes import (
    FakeSemanticCache,
    InMemoryCacheEntry,
    InMemorySemanticCache,
)
from optima.cache.redis import (
    RedisSearchClient,
    RedisSemanticCache,
    RedisSemanticCacheInvalidResponseError,
)

__all__ = [
    "AZURE_MANAGED_REDIS_PORT",
    "AZURE_MANAGED_REDIS_SCOPE",
    "AzureRedisCredentialProvider",
    "AzureRedisToken",
    "EmbeddingProviderError",
    "EmbeddingProviderResult",
    "EmbeddingProviderTimeout",
    "FakeSemanticCache",
    "InMemoryCacheEntry",
    "InMemorySemanticCache",
    "RedisSearchClient",
    "RedisIndexCompatibilityError",
    "RedisSemanticCache",
    "RedisSemanticCacheInvalidResponseError",
    "RedisSemanticCacheResources",
    "SemanticCache",
    "SemanticCacheEmbeddingProvider",
    "SemanticCacheLookupError",
    "SemanticCacheLookupRequest",
    "SemanticCacheLookupResult",
    "SemanticCacheLookupTimeout",
    "build_redis_semantic_cache_resources",
    "ensure_redis_semantic_cache_index",
    "redis_index_bootstrap_lock_key",
    "redis_index_contract_key",
]
