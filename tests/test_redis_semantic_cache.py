"""Offline tests for the Redis semantic-cache lookup adapter."""

import asyncio
import struct
import sys
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import time
from typing import Any

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError
from test_semantic_cache import candidate, lookup_request, request_binding

from optima.cache import (
    AzureRedisCredentialProvider,
    AzureRedisToken,
    EmbeddingProviderResult,
    RedisSemanticCacheResources,
    SemanticCacheLookupError,
    build_redis_semantic_cache_resources,
)
from optima.cache.azure_redis import _create_redis_client
from optima.cache.redis import (
    REDIS_CACHE_SCHEMA_VERSION,
    RedisSemanticCache,
    RedisSemanticCacheInvalidResponseError,
)
from optima.config import AppSettings, RedisAuthMode
from optima.domain.cache import CacheCandidate
from optima.domain.embedding import EmbeddingProfile


def embedding_profile(
    dimension: int = 3,
    *,
    model: str = "text-embed-3",
    deployment: str = "optima-embed",
) -> EmbeddingProfile:
    """Build one deterministic embedding profile for offline tests."""
    return EmbeddingProfile(
        model=model,
        deployment=deployment,
        dimension=dimension,
    )


class FakeEmbeddingProvider:
    """Return one configured embedding result and record lookup requests."""

    def __init__(
        self,
        embedding: Sequence[float],
        *,
        profile: EmbeddingProfile | None = None,
        provider: str = "fake-embed",
        input_tokens: int | None = 11,
        request_id: str | None = "embed-req-1",
    ) -> None:
        self.embedding = embedding
        self._profile = (
            profile
            if profile is not None
            else embedding_profile(_safe_dimension(embedding))
        )
        self._provider = provider
        self._input_tokens = input_tokens
        self._request_id = request_id
        self.calls: list[object] = []

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    async def embed(self, request: object) -> EmbeddingProviderResult:
        self.calls.append(request)
        return EmbeddingProviderResult(
            vector=tuple(self.embedding),
            profile=self._profile,
            provider=self._provider,
            input_tokens=self._input_tokens,
            request_id=self._request_id,
        )


def _safe_dimension(embedding: Sequence[float]) -> int:
    try:
        length = len(embedding)
    except TypeError:
        return 1
    return length if length > 0 else 1


def redis_cache(
    redis: object,
    embeddings: FakeEmbeddingProvider,
    *,
    index_name: str = "optima-cache-v1",
    timeout_seconds: float = 1.0,
    profile: EmbeddingProfile | None = None,
) -> RedisSemanticCache:
    """Build one adapter bound to the fake provider's profile by default."""
    return RedisSemanticCache(
        redis,  # type: ignore[arg-type]
        embeddings,
        index_name=index_name,
        embedding_profile=profile if profile is not None else embeddings.profile,
        timeout_seconds=timeout_seconds,
    )


class FakeRedisSearchClient:
    """Return one configured raw response and record exact Redis commands."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[object, ...]] = []

    async def execute_command(self, *args: object) -> object:
        self.calls.append(args)
        return self.response


class FakeRedisConnectionPool:
    """Record renewed tokens sent to redis-py's pool callback."""

    def __init__(self) -> None:
        self.tokens: list[AzureRedisToken] = []

    async def re_auth_callback(self, token: AzureRedisToken) -> None:
        self.tokens.append(token)


class FakeCloseableRedisClient(FakeRedisSearchClient):
    """Capture client cleanup and expose a fake reauthentication pool."""

    def __init__(self, *, fail_close: bool = False) -> None:
        super().__init__([0])
        self.connection_pool = FakeRedisConnectionPool()
        self.close_calls = 0
        self.fail_close = fail_close

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("Redis close failed")


class FakeAzureCredential:
    """Return one stable token and count explicit lifecycle calls."""

    def __init__(self, *, client_id: str | None = None) -> None:
        self.client_id = client_id
        self.get_token_calls: list[tuple[str, ...]] = []
        self.close_calls = 0

    async def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        self.get_token_calls.append(scopes)
        return AccessToken("secret-token", int(time()) + 3600)

    async def close(self) -> None:
        self.close_calls += 1


@dataclass
class RedisClientCreation:
    configuration: object
    password: str | None
    credential_provider: AzureRedisCredentialProvider | None
    client: FakeCloseableRedisClient


def redis_response(
    resolved: CacheCandidate,
    *,
    distance: str = "0.03",
    profile: EmbeddingProfile | None = None,
    fields_update: dict[bytes, object] | None = None,
) -> list[object]:
    """Encode one candidate in the documented Redis hash response shape."""
    resolved_profile = profile if profile is not None else embedding_profile(1)
    fields: dict[bytes, object] = {
        b"schema_version": str(REDIS_CACHE_SCHEMA_VERSION).encode(),
        b"source_run_id": resolved.source_run_id.encode(),
        b"output_text": resolved.output_text.encode(),
        b"request_binding_json": resolved.request_binding.model_dump_json().encode(),
        b"prior_evaluation_json": resolved.prior_evaluation.model_dump_json().encode(),
        b"contract_compatible": str(resolved.contract_compatible).lower().encode(),
        b"safe_to_reuse": str(resolved.safe_to_reuse).lower().encode(),
        b"embedding_profile": resolved_profile.identity.encode(),
        b"vector_distance": distance.encode(),
    }
    if fields_update is not None:
        fields.update(fields_update)
    flattened = [item for pair in fields.items() for item in pair]
    return [1, b"optima:cache:source-1", flattened]


def test_redis_lookup_runs_one_bounded_knn_query_and_decodes_candidate() -> None:
    """Retrieve evidence with exact FLOAT32 bytes and no policy threshold."""
    resolved = candidate(similarity=0.01)
    redis = FakeRedisSearchClient(
        redis_response(resolved, distance="0.99", profile=embedding_profile(3))
    )
    embeddings = FakeEmbeddingProvider((0.25, -0.5, 1.0))
    cache = redis_cache(redis, embeddings)

    result = asyncio.run(cache.lookup(lookup_request()))

    assert result.candidate == resolved
    assert result.embedding_attempt is not None
    assert result.embedding_attempt.outbound_attempted is True
    assert result.embedding_attempt.usage is not None
    assert result.embedding_attempt.usage.input_tokens == 11
    assert len(embeddings.calls) == 1
    assert len(redis.calls) == 1
    command = redis.calls[0]
    assert command[:3] == (
        "FT.SEARCH",
        "optima-cache-v1",
        "(@schema_version:{1} @task_type:{SUMMARIZATION} @complexity:{LOW} "
        f"@embedding_profile:{{{embedding_profile(3).identity}}})"
        "=>[KNN 1 @embedding $query_vector AS vector_distance]",
    )
    assert command[3:6] == ("PARAMS", 2, "query_vector")
    assert command[6] == struct.pack("<3f", 0.25, -0.5, 1.0)
    assert command[-5:] == ("LIMIT", 0, 1, "DIALECT", 2)


@pytest.mark.parametrize(
    ("distance", "expected_similarity"),
    [("1", 0.0), ("1.5", 0.0), ("2", 0.0)],
)
def test_redis_lookup_returns_nonpositive_similarity_for_planner_assessment(
    distance: str,
    expected_similarity: float,
) -> None:
    """Keep every valid cosine candidate available to Planner V1."""
    cache = redis_cache(
        FakeRedisSearchClient(redis_response(candidate(), distance=distance)),
        FakeEmbeddingProvider((1.0,)),
    )

    result = asyncio.run(cache.lookup(lookup_request()))

    assert result.candidate is not None
    assert result.candidate.similarity == expected_similarity


@pytest.mark.parametrize(
    ("distance", "expected_similarity"),
    [("0", 1.0), ("0.25", 0.75), ("0.5", 0.5)],
)
def test_redis_lookup_maps_positive_cosine_distance_to_similarity(
    distance: str,
    expected_similarity: float,
) -> None:
    """Preserve the exact cosine boundary at distance 0 and interior distances."""
    cache = redis_cache(
        FakeRedisSearchClient(redis_response(candidate(), distance=distance)),
        FakeEmbeddingProvider((1.0,)),
    )

    result = asyncio.run(cache.lookup(lookup_request()))

    assert result.candidate is not None
    assert result.candidate.similarity == expected_similarity


def test_redis_lookup_returns_binding_mismatch_for_planner_assessment() -> None:
    """Return mismatched evidence so Planner V1 owns the final binding gate."""
    mismatched = candidate(request_binding=request_binding(input_text="Other input"))
    cache = redis_cache(
        FakeRedisSearchClient(redis_response(mismatched)),
        FakeEmbeddingProvider((1.0,)),
    )

    result = asyncio.run(cache.lookup(lookup_request()))

    assert result.candidate is not None
    assert result.candidate.request_binding == mismatched.request_binding
    assert result.candidate.request_binding != lookup_request().request_binding


def test_redis_lookup_returns_truthful_miss() -> None:
    """Map the exact empty Redis search shape to no candidate."""
    redis = FakeRedisSearchClient([0])
    cache = redis_cache(redis, FakeEmbeddingProvider((1.0,)))

    result = asyncio.run(cache.lookup(lookup_request()))
    assert result.candidate is None
    assert result.embedding_attempt is not None
    assert result.embedding_attempt.usage is not None
    assert len(redis.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        [0, b"unexpected"],
        [1],
        [2, b"key", []],
        [1, b"key", [b"schema_version"]],
        redis_response(candidate(), fields_update={b"schema_version": b"999"}),
        redis_response(candidate(), fields_update={b"vector_distance": b"nan"}),
        redis_response(candidate(), fields_update={b"safe_to_reuse": b"1"}),
        redis_response(candidate(), fields_update={b"prior_evaluation_json": b"{}"}),
        redis_response(candidate(), fields_update={b"embedding_profile": b"0" * 64}),
    ],
)
def test_redis_lookup_rejects_malformed_or_unsupported_evidence(
    response: object,
) -> None:
    """Fail closed instead of returning partial or fabricated cache evidence."""
    cache = redis_cache(
        FakeRedisSearchClient(response),
        FakeEmbeddingProvider((1.0,)),
    )

    with pytest.raises(RedisSemanticCacheInvalidResponseError):
        asyncio.run(cache.lookup(lookup_request()))


@pytest.mark.parametrize(
    "embedding",
    [
        (),
        (0.0,),
        (float("nan"),),
        (float("inf"),),
        (1e100,),
        (True,),
    ],
)
def test_redis_lookup_rejects_invalid_vectors_before_redis(
    embedding: Sequence[float],
) -> None:
    """Reject unsafe vector values before issuing a Redis command."""
    redis = FakeRedisSearchClient([0])
    cache = redis_cache(redis, FakeEmbeddingProvider(embedding))

    with pytest.raises(SemanticCacheLookupError):
        asyncio.run(cache.lookup(lookup_request()))
    assert redis.calls == []


def test_redis_lookup_timeout_is_observable_to_api_fallback() -> None:
    """Propagate built-in TimeoutError for the existing typed API outcome."""

    class SlowEmbeddingProvider:
        profile = embedding_profile(1)

        async def embed(self, request: Any) -> EmbeddingProviderResult:
            await asyncio.sleep(0.01)
            return EmbeddingProviderResult(
                vector=(1.0,),
                profile=embedding_profile(1),
                provider="fake-embed",
            )

    cache = RedisSemanticCache(
        FakeRedisSearchClient([0]),
        SlowEmbeddingProvider(),
        index_name="optima-cache-v1",
        embedding_profile=embedding_profile(1),
        timeout_seconds=0.001,
    )

    with pytest.raises(TimeoutError):
        asyncio.run(cache.lookup(lookup_request()))


def test_redis_lookup_propagates_client_failure_without_fabricating_hit() -> None:
    """Fail closed on a Redis error so the API records a truthful lookup failure."""

    class FailingRedisSearchClient:
        def __init__(self) -> None:
            self.calls = 0

        async def execute_command(self, *args: object) -> object:
            self.calls += 1
            raise RuntimeError("no such index: optima-cache-v1")

    redis = FailingRedisSearchClient()
    embeddings = FakeEmbeddingProvider((1.0,))
    cache = redis_cache(redis, embeddings)

    with pytest.raises(SemanticCacheLookupError) as excinfo:
        asyncio.run(cache.lookup(lookup_request()))
    assert redis.calls == 1
    assert len(embeddings.calls) == 1
    assert excinfo.value.embedding_attempt is not None
    assert excinfo.value.embedding_attempt.usage is not None
    assert excinfo.value.embedding_attempt.usage.input_tokens == 11


def redis_settings(auth_mode: RedisAuthMode, **updates: object) -> AppSettings:
    """Build complete Redis settings for one explicit authentication mode."""
    values: dict[str, object] = {
        "redis_host": "optima.eastus.redis.azure.net",
        "redis_index_name": "optima-cache-v1",
        "redis_embedding_dimension": 3,
        "redis_embedding_model": "text-embed-3",
        "redis_embedding_deployment": "optima-embed",
        "redis_auth_mode": auth_mode,
    }
    values.update(updates)
    return AppSettings.model_validate(values)


def test_azure_redis_token_exposes_only_configured_object_id_claim() -> None:
    """Never derive the Redis username from an unverified JWT payload."""
    token = AzureRedisToken(
        value="secret-token",
        object_id="configured-object-id",
        expires_at_ms=(time() + 3600) * 1000,
        received_at_ms=time() * 1000,
    )

    assert token.try_get("oid") == "configured-object-id"
    assert token.try_get("sub") is None
    assert token.get_value() == "secret-token"
    assert "secret-token" not in repr(token)
    assert token.is_expired() is False


def test_azure_redis_credential_provider_acquires_scope_and_stops_once() -> None:
    """Acquire one token without retry and deterministically cancel renewal."""
    credential = FakeAzureCredential()
    provider = AzureRedisCredentialProvider(
        lambda: credential,
        "configured-object-id",
    )

    async def exercise_provider() -> tuple[tuple[str, str], bool]:
        credentials = await provider.get_credentials_async()
        streaming_before_close = provider.is_streaming()
        await provider.aclose()
        await provider.aclose()
        return credentials, streaming_before_close

    credentials, streaming_before_close = asyncio.run(exercise_provider())

    assert credentials == ("configured-object-id", "secret-token")
    assert credential.get_token_calls == [("https://redis.azure.com/.default",)]
    assert credential.close_calls == 1
    assert streaming_before_close is True
    assert provider.is_streaming() is False


def test_azure_redis_credential_provider_waits_for_initial_token_before_close() -> None:
    """Do not close Azure Identity while initial token acquisition is active."""

    class BlockingAzureCredential(FakeAzureCredential):
        def __init__(self) -> None:
            super().__init__()
            self.request_started = asyncio.Event()
            self.release_request = asyncio.Event()

        async def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
            self.request_started.set()
            await self.release_request.wait()
            return await super().get_token(*scopes, **kwargs)

    credential = BlockingAzureCredential()
    provider = AzureRedisCredentialProvider(
        lambda: credential,
        "configured-object-id",
    )

    async def exercise_provider() -> None:
        credentials_task = asyncio.create_task(provider.get_credentials_async())
        await credential.request_started.wait()
        close_task = asyncio.create_task(provider.aclose())
        await asyncio.sleep(0)

        assert close_task.done() is False
        assert credential.close_calls == 0

        credential.release_request.set()
        assert await credentials_task == (
            "configured-object-id",
            "secret-token",
        )
        await close_task

        assert credential.close_calls == 1
        with pytest.raises(RuntimeError, match="provider is closed"):
            await provider.get_credentials_async()

    asyncio.run(exercise_provider())


@pytest.mark.parametrize(
    ("auth_mode", "updates", "expected_credential", "expected_client_id"),
    [
        (
            RedisAuthMode.ACCESS_KEY,
            {"redis_access_key": "fake-access-key"},
            "ACCESS_KEY",
            None,
        ),
        (
            RedisAuthMode.AZURE_CLI,
            {"redis_object_id": "cli-object-id"},
            "AZURE_CLI",
            None,
        ),
        (
            RedisAuthMode.MANAGED_IDENTITY,
            {"redis_object_id": "system-object-id"},
            "MANAGED_IDENTITY",
            None,
        ),
        (
            RedisAuthMode.MANAGED_IDENTITY,
            {
                "redis_object_id": "user-object-id",
                "redis_managed_identity_client_id": "user-client-id",
            },
            "MANAGED_IDENTITY",
            "user-client-id",
        ),
    ],
)
def test_redis_composition_uses_only_selected_authentication(
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: RedisAuthMode,
    updates: dict[str, object],
    expected_credential: str,
    expected_client_id: str | None,
) -> None:
    """Create only the selected key or identity and wire one closeable client."""
    created_credentials: list[tuple[str, FakeAzureCredential]] = []
    client_creations: list[RedisClientCreation] = []

    def cli_credential() -> FakeAzureCredential:
        credential = FakeAzureCredential()
        created_credentials.append(("AZURE_CLI", credential))
        return credential

    def managed_credential(*, client_id: str | None) -> FakeAzureCredential:
        credential = FakeAzureCredential(client_id=client_id)
        created_credentials.append(("MANAGED_IDENTITY", credential))
        return credential

    def create_client(
        configuration: object,
        *,
        password: str | None,
        credential_provider: AzureRedisCredentialProvider | None,
    ) -> FakeCloseableRedisClient:
        client = FakeCloseableRedisClient()
        client_creations.append(
            RedisClientCreation(
                configuration=configuration,
                password=password,
                credential_provider=credential_provider,
                client=client,
            )
        )
        return client

    monkeypatch.setattr("optima.cache.azure_redis.AzureCliCredential", cli_credential)
    monkeypatch.setattr(
        "optima.cache.azure_redis.ManagedIdentityCredential", managed_credential
    )
    monkeypatch.setattr("optima.cache.azure_redis._create_redis_client", create_client)

    resources = build_redis_semantic_cache_resources(
        redis_settings(auth_mode, **updates),
        FakeEmbeddingProvider((0.25, -0.5, 1.0)),
    )

    assert len(client_creations) == 1
    creation = client_creations[0]
    if expected_credential == "ACCESS_KEY":
        assert creation.password == "fake-access-key"
        assert creation.credential_provider is None
        assert created_credentials == []
    else:
        assert creation.password is None
        assert creation.credential_provider is resources.credential_provider
        assert created_credentials == []

    async def exercise_resources() -> None:
        if resources.credential_provider is not None:
            await resources.credential_provider.get_credentials_async()
        await resources.aclose()
        await resources.aclose()

    asyncio.run(exercise_resources())
    assert creation.client.close_calls == 1
    if expected_credential == "ACCESS_KEY":
        assert created_credentials == []
    else:
        assert [kind for kind, _ in created_credentials] == [expected_credential]
        assert created_credentials[0][1].client_id == expected_client_id
        assert created_credentials[0][1].close_calls == 1


def test_redis_composition_failure_does_not_create_azure_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep Azure resources unallocated until Redis requests authentication."""
    credential_calls = 0

    def cli_credential() -> FakeAzureCredential:
        nonlocal credential_calls
        credential_calls += 1
        return FakeAzureCredential()

    def fail_client_creation(*args: object, **kwargs: object) -> object:
        raise RuntimeError("Redis client construction failed")

    monkeypatch.setattr("optima.cache.azure_redis.AzureCliCredential", cli_credential)
    monkeypatch.setattr(
        "optima.cache.azure_redis._create_redis_client", fail_client_creation
    )

    with pytest.raises(RuntimeError, match="Redis client construction failed"):
        build_redis_semantic_cache_resources(
            redis_settings(
                RedisAuthMode.AZURE_CLI,
                redis_object_id="cli-object-id",
            ),
            FakeEmbeddingProvider((0.25, -0.5, 1.0)),
        )

    assert credential_calls == 0


def test_redis_resources_close_identity_when_client_close_fails() -> None:
    """Release Azure Identity even when Redis transport cleanup fails."""
    client = FakeCloseableRedisClient(fail_close=True)
    credential = FakeAzureCredential()
    provider = AzureRedisCredentialProvider(
        lambda: credential,
        "object-id",
    )
    resources = RedisSemanticCacheResources(
        cache=RedisSemanticCache(
            client,
            FakeEmbeddingProvider((1.0,)),
            index_name="cache",
            embedding_profile=embedding_profile(1),
            timeout_seconds=1.0,
        ),
        client=client,
        credential_provider=provider,
    )

    async def exercise_resources() -> None:
        await provider.get_credentials_async()
        await resources.aclose()

    with pytest.raises(RuntimeError, match="Redis close failed"):
        asyncio.run(exercise_resources())

    assert client.close_calls == 1
    assert credential.close_calls == 1


def test_redis_composition_requires_explicit_configuration() -> None:
    """Keep default local composition free from Redis credential discovery."""
    with pytest.raises(ValueError, match="not configured"):
        build_redis_semantic_cache_resources(
            AppSettings(),
            FakeEmbeddingProvider((1.0,)),
        )


def test_redis_client_uses_tls_resp2_bounded_pool_and_zero_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the security and retry arguments passed to redis-py."""

    class FakeNoBackoff:
        pass

    class FakeRetry:
        def __init__(self, backoff: object, retries: int) -> None:
            self.backoff = backoff
            self.retries = retries

    class CapturingRedis:
        kwargs: dict[str, object]

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    redis_module = types.ModuleType("redis")
    asyncio_module = types.ModuleType("redis.asyncio")
    retry_module = types.ModuleType("redis.asyncio.retry")
    backoff_module = types.ModuleType("redis.backoff")
    asyncio_module.Redis = CapturingRedis  # type: ignore[attr-defined]
    retry_module.Retry = FakeRetry  # type: ignore[attr-defined]
    backoff_module.NoBackoff = FakeNoBackoff  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", redis_module)
    monkeypatch.setitem(sys.modules, "redis.asyncio", asyncio_module)
    monkeypatch.setitem(sys.modules, "redis.asyncio.retry", retry_module)
    monkeypatch.setitem(sys.modules, "redis.backoff", backoff_module)
    configuration = redis_settings(
        RedisAuthMode.ACCESS_KEY,
        redis_access_key="fake-key",
        redis_timeout_seconds=2.5,
        redis_max_connections=7,
    ).redis_semantic_cache_configuration()
    assert configuration is not None

    client = _create_redis_client(
        configuration,
        password="fake-key",
        credential_provider=None,
    )
    kwargs = client.kwargs  # type: ignore[attr-defined]

    assert kwargs == {
        "host": "optima.eastus.redis.azure.net",
        "port": 10_000,
        "password": "fake-key",
        "credential_provider": None,
        "ssl": True,
        "ssl_cert_reqs": "required",
        "ssl_check_hostname": True,
        "socket_timeout": 2.5,
        "socket_connect_timeout": 2.5,
        "max_connections": 7,
        "decode_responses": False,
        "protocol": 2,
        "legacy_responses": True,
        "retry": kwargs["retry"],
        "retry_on_timeout": False,
    }
    retry = kwargs["retry"]
    assert isinstance(retry, FakeRetry)
    assert isinstance(retry.backoff, FakeNoBackoff)
    assert retry.retries == 0


def test_redis_lookup_records_embedding_usage_on_hit() -> None:
    """Record the embedding request so a cache hit is not falsely free."""
    resolved = candidate(similarity=0.01)
    embeddings = FakeEmbeddingProvider((1.0,), input_tokens=13, request_id="apim-req-9")
    cache = redis_cache(
        FakeRedisSearchClient(redis_response(resolved, profile=embeddings.profile)),
        embeddings,
    )

    result = asyncio.run(cache.lookup(lookup_request()))

    assert result.candidate is not None
    assert result.candidate.source_run_id == resolved.source_run_id
    assert result.embedding_attempt is not None
    usage = result.embedding_attempt.usage
    assert usage is not None
    assert usage.run_id == "run-current-1"
    assert usage.provider == "fake-embed"
    assert usage.deployment == "optima-embed"
    assert usage.embedding_profile == embeddings.profile.identity
    assert usage.input_tokens == 13
    assert usage.request_id == "apim-req-9"
    assert usage.calculated_cost is None


def test_redis_lookup_rejects_provider_profile_mismatch() -> None:
    """Refuse a provider whose returned profile is not the configured profile."""
    redis = FakeRedisSearchClient([0])
    embeddings = FakeEmbeddingProvider((1.0,), profile=embedding_profile(1))
    cache = redis_cache(
        redis,
        embeddings,
        profile=embedding_profile(2),
    )

    with pytest.raises(SemanticCacheLookupError, match="profile") as excinfo:
        asyncio.run(cache.lookup(lookup_request()))
    assert redis.calls == []
    assert excinfo.value.embedding_attempt is not None
    assert excinfo.value.embedding_attempt.usage is not None


def test_redis_composition_rejects_mismatched_embedding_profile() -> None:
    """Fail fast when the provider and Redis index profiles disagree."""
    with pytest.raises(ValueError, match="profile"):
        build_redis_semantic_cache_resources(
            redis_settings(
                RedisAuthMode.ACCESS_KEY,
                redis_access_key="fake-access-key",
            ),
            FakeEmbeddingProvider(
                (0.25, -0.5, 1.0),
                profile=embedding_profile(3, model="other-model"),
            ),
        )


def test_embedding_profile_identity_is_deterministic_and_distinct() -> None:
    """Bind identity to model, deployment, and dimension without collisions."""
    base = embedding_profile(3)
    assert base.identity == embedding_profile(3).identity
    assert len(base.identity) == 64
    assert base.identity != embedding_profile(4).identity
    assert base.identity != embedding_profile(3, model="other-model").identity
    assert base.identity != embedding_profile(3, deployment="other-deploy").identity


@pytest.mark.parametrize(
    "value",
    ["bad model!", "with space", "tag{injection", "pipe|value", "semi;colon"],
)
def test_embedding_profile_rejects_injection_values(value: str) -> None:
    """Reject profile tokens that could inject RediSearch query syntax."""
    with pytest.raises(ValueError):
        EmbeddingProfile(model=value, deployment="optima-embed", dimension=3)


def _flaky_token_credential(
    failing_calls: set[int],
    *,
    park: asyncio.Event | None = None,
    park_from: int | None = None,
    error_factory: "Callable[[], Exception]" = lambda: ServiceRequestError(
        "transient token failure"
    ),
) -> "FakeAzureCredential":
    class FlakyAzureCredential(FakeAzureCredential):
        async def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
            call = len(self.get_token_calls) + 1
            self.get_token_calls.append(scopes)
            if park is not None and park_from is not None and call >= park_from:
                await park.wait()
            if call in failing_calls:
                raise error_factory()
            return AccessToken("secret-token", int(time()) + 3600)

    return FlakyAzureCredential()


def test_token_renewal_retries_transient_failure_then_succeeds() -> None:
    """A transient token failure must not permanently disable renewal."""
    park = asyncio.Event()
    credential = _flaky_token_credential({2}, park=park, park_from=4)
    sleeps: list[float] = []
    delivered: list[AzureRedisToken] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    provider = AzureRedisCredentialProvider(
        lambda: credential,
        "object-id",
        max_renewal_attempts=3,
        sleep=fake_sleep,
        jitter=lambda base: 0.0,
    )

    async def on_next(token: AzureRedisToken) -> None:
        delivered.append(token)

    async def exercise() -> None:
        provider.on_next(on_next)
        await provider.get_credentials_async()
        for _ in range(500):
            if delivered:
                break
            await asyncio.sleep(0)
        assert delivered
        park.set()
        await provider.aclose()

    asyncio.run(exercise())

    assert len(delivered) == 1
    assert len(sleeps) >= 2
    assert credential.close_calls == 1


def test_token_renewal_stops_immediately_on_non_transient_error() -> None:
    """A non-transient authentication failure must stop renewal without retry."""
    credential = _flaky_token_credential(
        {2, 3, 4, 5, 6},
        error_factory=lambda: ClientAuthenticationError("bad credential"),
    )
    errors: list[Exception] = []

    async def fake_sleep(delay: float) -> None:
        return None

    provider = AzureRedisCredentialProvider(
        lambda: credential,
        "object-id",
        max_renewal_attempts=3,
        sleep=fake_sleep,
        jitter=lambda base: 0.0,
    )

    async def on_error(error: Exception) -> None:
        errors.append(error)

    async def exercise() -> None:
        provider.on_error(on_error)
        await provider.get_credentials_async()
        for _ in range(500):
            if not provider.is_streaming():
                break
            await asyncio.sleep(0)
        assert provider.is_streaming() is False
        await provider.aclose()

    asyncio.run(exercise())

    assert len(errors) == 1
    assert isinstance(errors[0], ClientAuthenticationError)
    # Only the initial success plus one non-transient failure; no retries.
    assert len(credential.get_token_calls) == 2


def test_token_renewal_stops_after_exhausting_bounded_attempts() -> None:
    """Give up renewal after the configured bounded attempts are exhausted."""
    credential = _flaky_token_credential({2, 3, 4, 5, 6})
    errors: list[Exception] = []

    async def fake_sleep(delay: float) -> None:
        return None

    provider = AzureRedisCredentialProvider(
        lambda: credential,
        "object-id",
        max_renewal_attempts=3,
        sleep=fake_sleep,
        jitter=lambda base: 0.0,
    )

    async def on_error(error: Exception) -> None:
        errors.append(error)

    async def exercise() -> None:
        provider.on_error(on_error)
        await provider.get_credentials_async()
        for _ in range(500):
            if not provider.is_streaming():
                break
            await asyncio.sleep(0)
        assert provider.is_streaming() is False
        await provider.aclose()

    asyncio.run(exercise())

    assert len(errors) == 1
    assert len(credential.get_token_calls) == 4


def test_token_renewal_retries_reauthentication_then_delivers() -> None:
    """A transient reauthentication failure is retried until the pool accepts."""
    park = asyncio.Event()
    credential = _flaky_token_credential(set(), park=park, park_from=3)
    delivered: list[AzureRedisToken] = []
    errors: list[Exception] = []
    reauth_calls = {"count": 0}

    async def fake_sleep(delay: float) -> None:
        return None

    async def failing_on_next(token: AzureRedisToken) -> None:
        reauth_calls["count"] += 1
        if reauth_calls["count"] == 1:
            raise RuntimeError("reauthentication failed")
        delivered.append(token)

    async def on_error(error: Exception) -> None:
        errors.append(error)

    provider = AzureRedisCredentialProvider(
        lambda: credential,
        "object-id",
        sleep=fake_sleep,
        jitter=lambda base: 0.0,
    )

    async def exercise() -> None:
        provider.on_next(failing_on_next)
        provider.on_error(on_error)
        await provider.get_credentials_async()
        for _ in range(500):
            if delivered:
                break
            await asyncio.sleep(0)
        assert delivered
        park.set()
        await provider.aclose()

    asyncio.run(exercise())

    # The token is published only after the pool accepts it on retry.
    assert reauth_calls["count"] == 2
    assert len(delivered) == 1
    assert errors == []
