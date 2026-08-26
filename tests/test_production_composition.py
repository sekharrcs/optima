"""Tests for production API composition and lifespan resource ownership."""

import asyncio
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from optima.api.production import (
    ProductionComponentBuilders,
    build_production_runtime,
    create_production_app,
)
from optima.cache import FakeSemanticCache, SemanticCacheEmbeddingProvider
from optima.cache.redis import RedisSearchClient
from optima.config import (
    AppSettings,
    CosmosAuthMode,
    FoundryAuthMode,
    ProductionEvaluatorMode,
    RedisAuthMode,
)
from optima.domain.embedding import EmbeddingProfile
from optima.observability.noop import NO_OP_RUN, NoOpRunObservation
from optima.providers import (
    FakeEmbeddingProvider,
    FakeModelProvider,
    FakeProviderResponse,
    build_fake_small_provider,
    build_fake_strong_provider,
)
from optima.storage import InMemoryRunHistoryStore


def production_settings() -> AppSettings:
    """Build complete production settings without reading ambient values."""
    return AppSettings(
        deployment_environment="hackathon",
        production_evaluator_mode=ProductionEvaluatorMode.EXACT_REFERENCE,
        production_require_reference_output=True,
        foundry_base_url="https://optima.openai.azure.com/openai/v1",
        foundry_small_deployment="small",
        foundry_strong_deployment="strong",
        foundry_auth_mode=FoundryAuthMode.MANAGED_IDENTITY,
        foundry_token_scope="https://cognitiveservices.azure.com/.default",
        foundry_managed_identity_client_id="api-client-id",
        cosmos_endpoint="https://optima.documents.azure.com:443/",
        cosmos_database_name="optima",
        cosmos_container_name="runs",
        cosmos_auth_mode=CosmosAuthMode.MANAGED_IDENTITY,
        cosmos_managed_identity_client_id="api-client-id",
        redis_host="optima.eastus2.redis.azure.net",
        redis_index_name="optima-cache-v1",
        redis_embedding_dimension=3,
        redis_embedding_model="embed-model",
        redis_embedding_deployment="embed-deployment",
        redis_auth_mode=RedisAuthMode.MANAGED_IDENTITY,
        redis_object_id="api-object-id",
        redis_managed_identity_client_id="api-client-id",
        application_insights_enabled=True,
        application_insights_connection_string=SecretStr(
            "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
            "IngestionEndpoint=https://eastus2-1.in.applicationinsights.azure.com/"
        ),
        application_insights_deployment_environment="hackathon",
    )


class RecordingObservability:
    """Record instrumentation and shutdown while keeping run tracing inert."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.instrument_calls = 0

    def start_run(
        self,
        *,
        run_id: str,
        correlation_id: str,
    ) -> NoOpRunObservation:
        return NO_OP_RUN

    def instrument_fastapi(self, application: FastAPI) -> None:
        self.instrument_calls += 1

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def close(self) -> None:
        self.events.append("close:telemetry")


@dataclass
class FoundryResources:
    """Fake paired model resources with one close boundary."""

    events: list[str]
    fail_close: bool = False
    small_provider: FakeModelProvider = field(init=False)
    strong_provider: FakeModelProvider = field(init=False)

    def __post_init__(self) -> None:
        self.small_provider = build_fake_small_provider(
            provider_name="foundry",
            deployment_name="small",
            responses=(
                FakeProviderResponse(
                    output_text="small",
                    input_tokens=1,
                    output_tokens=1,
                ),
            ),
        )
        self.strong_provider = build_fake_strong_provider(
            provider_name="foundry",
            deployment_name="strong",
            responses=(
                FakeProviderResponse(
                    output_text="strong",
                    input_tokens=1,
                    output_tokens=1,
                ),
            ),
        )

    async def aclose(self) -> None:
        self.events.append("close:foundry")
        if self.fail_close:
            raise RuntimeError("foundry close failed")


@dataclass
class EmbeddingResources:
    """Fake embedding resources with one close boundary."""

    events: list[str]
    profile: EmbeddingProfile
    fail_close: bool = False
    provider: FakeEmbeddingProvider = field(init=False)

    def __post_init__(self) -> None:
        self.provider = FakeEmbeddingProvider(profile=self.profile)

    async def aclose(self) -> None:
        self.events.append("close:embedding")
        if self.fail_close:
            raise RuntimeError("embedding close failed")


class RedisClient:
    """Unused command boundary supplied to the injected bootstrap."""

    async def execute_command(self, *args: object) -> object:
        raise AssertionError("Injected bootstrap must own Redis test behavior")


@dataclass
class RedisResources:
    """Fake semantic-cache resources with one close boundary."""

    events: list[str]
    fail_close: bool = False
    cache: FakeSemanticCache = field(init=False)
    client: RedisClient = field(init=False)

    def __post_init__(self) -> None:
        self.cache = FakeSemanticCache(())
        self.client = RedisClient()

    async def aclose(self) -> None:
        self.events.append("close:redis")
        if self.fail_close:
            raise RuntimeError("redis close failed")


@dataclass
class CosmosResources:
    """Fake run-history resources with one close boundary."""

    events: list[str]
    fail_close: bool = False
    store: InMemoryRunHistoryStore = field(init=False)

    def __post_init__(self) -> None:
        self.store = InMemoryRunHistoryStore()

    async def aclose(self) -> None:
        self.events.append("close:cosmos")
        if self.fail_close:
            raise RuntimeError("cosmos close failed")


def component_builders(
    events: list[str],
    *,
    bootstrap_error: Exception | None = None,
    embedding_close_error: bool = False,
    cosmos_close_error: bool = False,
) -> ProductionComponentBuilders:
    """Build an injectable production component graph with event recording."""
    profile = EmbeddingProfile(
        model="embed-model",
        deployment="embed-deployment",
        dimension=3,
    )

    def build_foundry(settings: AppSettings) -> FoundryResources:
        events.append("build:foundry")
        return FoundryResources(events)

    def build_embedding(settings: AppSettings) -> EmbeddingResources:
        events.append("build:embedding")
        return EmbeddingResources(
            events,
            profile,
            fail_close=embedding_close_error,
        )

    def build_redis(
        settings: AppSettings,
        provider: SemanticCacheEmbeddingProvider,
    ) -> RedisResources:
        events.append("build:redis")
        return RedisResources(events)

    async def bootstrap_index(
        client: RedisSearchClient,
        *,
        index_name: str,
        embedding_profile: EmbeddingProfile,
    ) -> None:
        events.append("bootstrap:redis")
        assert index_name == "optima-cache-v1"
        assert embedding_profile == profile
        if bootstrap_error is not None:
            raise bootstrap_error

    def build_cosmos(settings: AppSettings) -> CosmosResources:
        events.append("build:cosmos")
        return CosmosResources(events, fail_close=cosmos_close_error)

    return ProductionComponentBuilders(
        foundry=build_foundry,
        embedding=build_embedding,
        redis=build_redis,
        cosmos=build_cosmos,
        bootstrap_index=bootstrap_index,
    )


def test_production_runtime_constructs_expected_graph_and_closes_once() -> None:
    """Build every production component and close in reverse ownership order."""
    events: list[str] = []
    observability = RecordingObservability(events)
    runtime = asyncio.run(
        build_production_runtime(
            production_settings(),
            observability=observability,
            builders=component_builders(events),
        )
    )

    assert events == [
        "build:foundry",
        "build:embedding",
        "build:redis",
        "bootstrap:redis",
        "build:cosmos",
    ]
    assert runtime.dependencies.settings.deployment_environment == "hackathon"
    assert runtime.dependencies.semantic_cache is not None
    assert runtime.dependencies.run_history_store is not None

    asyncio.run(runtime.aclose())
    asyncio.run(runtime.aclose())

    assert events[-5:] == [
        "close:cosmos",
        "close:redis",
        "close:embedding",
        "close:foundry",
        "close:telemetry",
    ]


def test_production_app_lifespan_builds_once_before_health_is_ready() -> None:
    """Yield readiness only after Redis bootstrap and the complete graph exist."""
    events: list[str] = []
    observability = RecordingObservability(events)
    application = create_production_app(
        settings=production_settings(),
        builders=component_builders(events),
        observability_builder=lambda settings: observability,
    )

    with TestClient(application) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert events.count("build:foundry") == 1
        assert events.index("bootstrap:redis") < events.index("build:cosmos")

    assert observability.instrument_calls == 1
    assert events.count("close:telemetry") == 1


def test_bootstrap_failure_prevents_readiness_and_cleans_partial_startup() -> None:
    """Preserve the startup error after closing every resource already owned."""
    events: list[str] = []
    observability = RecordingObservability(events)
    startup_error = RuntimeError("Redis bootstrap failed")
    application = create_production_app(
        settings=production_settings(),
        builders=component_builders(
            events,
            bootstrap_error=startup_error,
            embedding_close_error=True,
        ),
        observability_builder=lambda settings: observability,
    )

    with pytest.raises(RuntimeError, match="Redis bootstrap failed") as captured:
        with TestClient(application):
            pytest.fail("The application must not become ready")

    assert captured.value is startup_error
    assert events[-4:] == [
        "close:redis",
        "close:embedding",
        "close:foundry",
        "close:telemetry",
    ]
    assert "build:cosmos" not in events


def test_shutdown_cleanup_error_does_not_skip_remaining_resources() -> None:
    """Continue reverse cleanup and telemetry shutdown after a local close failure."""
    events: list[str] = []
    runtime = asyncio.run(
        build_production_runtime(
            production_settings(),
            observability=RecordingObservability(events),
            builders=component_builders(events, cosmos_close_error=True),
        )
    )

    asyncio.run(runtime.aclose())

    assert events[-5:] == [
        "close:cosmos",
        "close:redis",
        "close:embedding",
        "close:foundry",
        "close:telemetry",
    ]
