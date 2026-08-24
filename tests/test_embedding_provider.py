"""Offline tests for the Foundry/APIM embedding provider."""

import asyncio
from collections.abc import Callable

import httpx
import pytest
from azure.core.credentials import AccessToken, TokenCredential
from test_semantic_cache import lookup_request

from optima.api.dependencies import build_foundry_embedding_provider
from optima.cache import EmbeddingProviderResult
from optima.config import AppSettings, FoundryAuthMode, RedisAuthMode
from optima.domain.embedding import EmbeddingProfile
from optima.providers import (
    ApiKeyAuthentication,
    FoundryEmbeddingProvider,
    FoundryProviderError,
)

BASE_URL = "https://example-resource.openai.azure.com/openai/v1"

Handler = Callable[[httpx.Request], httpx.Response]


def embedding_profile(dimension: int = 3) -> EmbeddingProfile:
    """Build one deterministic embedding profile for offline tests."""
    return EmbeddingProfile(
        model="text-embed-3",
        deployment="optima-embed",
        dimension=dimension,
    )


def embeddings_response(**updates: object) -> dict[str, object]:
    """Build one Azure OpenAI v1 embeddings response body."""
    body: dict[str, object] = {
        "object": "list",
        "data": [
            {
                "object": "embedding",
                "index": 0,
                "embedding": [0.1, -0.2, 0.3],
            }
        ],
        "model": "text-embed-3",
        "usage": {"prompt_tokens": 7, "total_tokens": 7},
    }
    body.update(updates)
    return body


def make_provider(
    handler: Handler,
    *,
    profile: EmbeddingProfile | None = None,
) -> FoundryEmbeddingProvider:
    """Build one embedding provider bound to a mock transport."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FoundryEmbeddingProvider(
        base_url=BASE_URL,
        profile=profile if profile is not None else embedding_profile(),
        authentication=ApiKeyAuthentication("fake-key"),
        client=client,
    )


async def _embed(provider: FoundryEmbeddingProvider) -> EmbeddingProviderResult:
    try:
        return await provider.embed(lookup_request())
    finally:
        await provider._client.aclose()


def test_embedding_provider_returns_vector_profile_and_usage() -> None:
    """Map one valid embeddings response to typed vector, profile, and usage."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=embeddings_response(),
            headers={"apim-request-id": "req-embed-1"},
        )

    provider = make_provider(handler)
    result = asyncio.run(_embed(provider))

    assert result.vector == (0.1, -0.2, 0.3)
    assert result.profile == embedding_profile()
    assert result.provider == "microsoft-foundry-apim"
    assert result.request_id == "req-embed-1"
    assert result.input_tokens == 7
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/openai/v1/embeddings")
    assert b'"model":"optima-embed"' in requests[0].content
    assert b'"input":"Summarize incident ARC-9"' in requests[0].content


def test_embedding_provider_reports_missing_usage_without_fabrication() -> None:
    """Return no input-token count when the provider omits usage."""
    provider = make_provider(
        lambda request: httpx.Response(200, json=embeddings_response(usage=None))
    )
    result = asyncio.run(_embed(provider))
    assert result.input_tokens is None


@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
        {"data": [{"index": 0, "embedding": [0.1, 0.2, True]}]},
        {"data": [{"index": 1, "embedding": [0.1, 0.2, 0.3]}]},
        {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                {"index": 1, "embedding": [0.4, 0.5, 0.6]},
            ]
        },
        {"data": []},
        {
            "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
            "usage": {"prompt_tokens": True},
        },
        {
            "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
            "usage": {"prompt_tokens": -1},
        },
    ],
)
def test_embedding_provider_rejects_malformed_responses(
    body: dict[str, object],
) -> None:
    """Fail closed on wrong dimension, index, count, and malformed usage."""
    payload = embeddings_response()
    payload.update(body)
    provider = make_provider(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(FoundryProviderError, match="INVALID_RESPONSE|invalid"):
        asyncio.run(_embed(provider))


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_embedding_provider_rejects_non_finite_vector_values(token: str) -> None:
    """Reject non-finite embedding values even when the parser accepts them."""
    content = (
        '{"object":"list","data":[{"object":"embedding","index":0,'
        '"embedding":[0.1,0.2,' + token + ']}],"model":"text-embed-3",'
        '"usage":{"prompt_tokens":7}}'
    ).encode()
    provider = make_provider(
        lambda request: httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json"},
        )
    )
    with pytest.raises(FoundryProviderError):
        asyncio.run(_embed(provider))


def test_embedding_provider_maps_timeout_to_builtin_timeout() -> None:
    """Map a transport timeout to the shared TimeoutError contract."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    provider = make_provider(handler)
    with pytest.raises(TimeoutError):
        asyncio.run(_embed(provider))


@pytest.mark.parametrize("status", [408, 504])
def test_embedding_provider_maps_gateway_timeout_status_to_timeout(
    status: int,
) -> None:
    """Map upstream 408/504 to TimeoutError for the typed fallback path."""
    provider = make_provider(lambda request: httpx.Response(status))
    with pytest.raises(TimeoutError):
        asyncio.run(_embed(provider))


def test_embedding_provider_maps_server_error_without_leakage() -> None:
    """Map a 500 to a safe categorized error without leaking the body."""
    provider = make_provider(
        lambda request: httpx.Response(500, json={"secret": "leak"})
    )
    with pytest.raises(FoundryProviderError) as excinfo:
        asyncio.run(_embed(provider))
    assert "leak" not in str(excinfo.value)


class FakeTokenCredential(TokenCredential):
    """Return one non-secret fake token and record close calls."""

    def __init__(self) -> None:
        self.closed = False

    def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        return AccessToken("fake-entra-token", 4_102_444_800)

    def close(self) -> None:
        self.closed = True


def _embedding_settings(**updates: object) -> AppSettings:
    values: dict[str, object] = {
        "foundry_base_url": BASE_URL,
        "foundry_small_deployment": "small",
        "foundry_strong_deployment": "strong",
        "foundry_auth_mode": FoundryAuthMode.API_KEY,
        "foundry_api_key": "fake-key",
        "redis_host": "optima.eastus.redis.azure.net",
        "redis_index_name": "optima-cache-v1",
        "redis_embedding_dimension": 3,
        "redis_embedding_model": "text-embed-3",
        "redis_embedding_deployment": "optima-embed",
        "redis_auth_mode": RedisAuthMode.ACCESS_KEY,
        "redis_access_key": "fake-access-key",
    }
    values.update(updates)
    return AppSettings.model_validate(values)


def test_build_embedding_provider_binds_redis_profile() -> None:
    """Compose one embedding provider whose profile matches the cache index."""
    resources = build_foundry_embedding_provider(
        _embedding_settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=embeddings_response())
        ),
    )
    try:
        assert resources.provider.profile == embedding_profile()
        assert resources.credential is None
    finally:
        asyncio.run(resources.aclose())


def test_build_embedding_provider_requires_redis_profile() -> None:
    """Refuse to compose an embedding provider without a Redis cache profile."""
    with pytest.raises(ValueError, match="Redis semantic-cache settings"):
        build_foundry_embedding_provider(
            _embedding_settings(
                redis_host=None,
                redis_index_name=None,
                redis_embedding_dimension=None,
                redis_embedding_model=None,
                redis_embedding_deployment=None,
                redis_auth_mode=None,
                redis_access_key=None,
            )
        )
