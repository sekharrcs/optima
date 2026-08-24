"""Provider abstractions and fake implementations for model execution."""

from optima.providers.contracts import (
    ModelProvider,
    ModelProviderCall,
    ModelProviderRequest,
    ModelProviderResult,
    MonotonicClock,
)
from optima.providers.fakes import (
    FakeEmbeddingProvider,
    FakeModelProvider,
    FakeProviderResponse,
    build_fake_small_provider,
    build_fake_strong_provider,
)
from optima.providers.foundry import (
    FOUNDRY_PROVIDER_NAME,
    ApiKeyAuthentication,
    EntraTokenAuthentication,
    FoundryAuthentication,
    FoundryEmbeddingProvider,
    FoundryModelProvider,
    FoundryProviderError,
)

__all__ = [
    "FakeEmbeddingProvider",
    "FakeModelProvider",
    "FakeProviderResponse",
    "FOUNDRY_PROVIDER_NAME",
    "ApiKeyAuthentication",
    "EntraTokenAuthentication",
    "FoundryAuthentication",
    "FoundryEmbeddingProvider",
    "FoundryModelProvider",
    "FoundryProviderError",
    "ModelProvider",
    "ModelProviderCall",
    "ModelProviderRequest",
    "ModelProviderResult",
    "MonotonicClock",
    "build_fake_small_provider",
    "build_fake_strong_provider",
]
