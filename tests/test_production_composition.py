"""Tests for production API composition and lifespan resource ownership."""

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

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
from optima.domain.execution import ModelRole
from optima.domain.run import ModelUsage
from optima.evaluation import (
    DeterministicEvaluator,
    ExactReferenceMeasurement,
    FakeEvaluator,
    LLMJudgeEvaluator,
)
from optima.observability.noop import NO_OP_RUN, NoOpRunObservation
from optima.providers import (
    FOUNDRY_PROVIDER_NAME,
    FakeEmbeddingProvider,
    FakeModelProvider,
    FakeProviderResponse,
    build_fake_small_provider,
    build_fake_strong_provider,
)
from optima.storage import InMemoryRunHistoryStore


def production_settings(**updates: object) -> AppSettings:
    """Build complete production settings without reading ambient values."""
    semantic_cache_enabled = updates.get("semantic_cache_enabled", True)
    values: dict[str, object] = {
        "deployment_environment": "hackathon",
        "production_evaluator_mode": ProductionEvaluatorMode.EXACT_REFERENCE,
        "production_require_reference_output": True,
        "semantic_cache_enabled": semantic_cache_enabled,
        "foundry_base_url": "https://optima.openai.azure.com/openai/v1",
        "foundry_small_deployment": "small",
        "foundry_small_model": "small-model",
        "foundry_small_model_version": "small-version",
        "foundry_strong_deployment": "strong",
        "foundry_strong_model": "strong-model",
        "foundry_strong_model_version": "strong-version",
        "foundry_auth_mode": FoundryAuthMode.MANAGED_IDENTITY,
        "foundry_token_scope": "https://cognitiveservices.azure.com/.default",
        "foundry_managed_identity_client_id": "api-client-id",
        "cosmos_endpoint": "https://optima.documents.azure.com:443/",
        "cosmos_database_name": "optima",
        "cosmos_container_name": "runs",
        "cosmos_auth_mode": CosmosAuthMode.MANAGED_IDENTITY,
        "cosmos_managed_identity_client_id": "api-client-id",
        "application_insights_enabled": True,
        "application_insights_connection_string": SecretStr(
            "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
            "IngestionEndpoint=https://eastus2-1.in.applicationinsights.azure.com/"
        ),
        "application_insights_deployment_environment": "hackathon",
    }
    if semantic_cache_enabled is True:
        values.update(
            {
                "redis_host": "optima.eastus2.redis.azure.net",
                "redis_index_name": "optima-cache-v1",
                "redis_embedding_dimension": 3,
                "redis_embedding_model": "embed-model",
                "redis_embedding_deployment": "embed-deployment",
                "redis_auth_mode": RedisAuthMode.MANAGED_IDENTITY,
                "redis_object_id": "api-object-id",
                "redis_managed_identity_client_id": "api-client-id",
            }
        )
    values.update(updates)
    return AppSettings.model_validate(values)


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


@dataclass
class JudgeResources:
    """Fake judge resources with one close boundary."""

    events: list[str]
    fail_close: bool = False
    provider: FakeModelProvider = field(init=False)

    def __post_init__(self) -> None:
        self.provider = FakeModelProvider(
            provider_name="foundry",
            deployment_name="judge",
            model_role=ModelRole.JUDGE,
            responses=(
                FakeProviderResponse(
                    output_text="{}",
                    input_tokens=1,
                    output_tokens=1,
                ),
            ),
        )

    async def aclose(self) -> None:
        self.events.append("close:judge")
        if self.fail_close:
            raise RuntimeError("judge close failed")


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
    embedding_build_error: Exception | None = None,
    embedding_close_error: bool = False,
    judge_close_error: bool = False,
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
        if embedding_build_error is not None:
            raise embedding_build_error
        return EmbeddingResources(
            events,
            profile,
            fail_close=embedding_close_error,
        )

    def build_judge(settings: AppSettings) -> JudgeResources:
        events.append("build:judge")
        return JudgeResources(events, fail_close=judge_close_error)

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
        judge=build_judge,
        embedding=build_embedding,
        redis=build_redis,
        cosmos=build_cosmos,
        bootstrap_index=bootstrap_index,
    )


def llm_judge_settings(**updates: object) -> AppSettings:
    """Build complete reference-free production settings."""
    values: dict[str, object] = {
        "production_evaluator_mode": ProductionEvaluatorMode.LLM_JUDGE,
        "production_require_reference_output": False,
        "judge_deployment": "judge",
        "judge_model": "judge-model-v1",
        "judge_model_version": "2025-04-14",
    }
    values.update(updates)
    return production_settings(**values)


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


def test_cache_disabled_runtime_never_builds_or_closes_cache_resources() -> None:
    """Construct only active production resources and inject the no-cache path."""
    events: list[str] = []
    runtime = asyncio.run(
        build_production_runtime(
            production_settings(semantic_cache_enabled=False),
            observability=RecordingObservability(events),
            builders=component_builders(events),
        )
    )

    assert events == ["build:foundry", "build:cosmos"]
    assert runtime.dependencies.semantic_cache is None
    assert runtime.dependencies.run_history_store is not None

    asyncio.run(runtime.aclose())
    asyncio.run(runtime.aclose())

    assert events == [
        "build:foundry",
        "build:cosmos",
        "close:cosmos",
        "close:foundry",
        "close:telemetry",
    ]


def test_cache_disabled_runtime_preserves_judge_and_non_cache_lifecycle() -> None:
    """Keep JUDGE, Cosmos, Foundry, and telemetry ownership when cache is disabled."""
    events: list[str] = []
    runtime = asyncio.run(
        build_production_runtime(
            llm_judge_settings(semantic_cache_enabled=False),
            observability=RecordingObservability(events),
            builders=component_builders(events),
        )
    )

    assert isinstance(runtime.dependencies.evaluator, LLMJudgeEvaluator)
    assert runtime.dependencies.semantic_cache is None
    assert events == ["build:foundry", "build:judge", "build:cosmos"]

    asyncio.run(runtime.aclose())

    assert events[-4:] == [
        "close:cosmos",
        "close:judge",
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


def test_production_runtime_uses_reviewed_deterministic_evaluator() -> None:
    """Compose the reviewed exact-reference evaluator without any fake fallback."""
    events: list[str] = []
    runtime = asyncio.run(
        build_production_runtime(
            production_settings(),
            observability=RecordingObservability(events),
            builders=component_builders(events),
        )
    )
    evaluator = runtime.dependencies.evaluator
    asyncio.run(runtime.aclose())

    assert not isinstance(evaluator, FakeEvaluator)
    assert isinstance(evaluator, DeterministicEvaluator)
    assert isinstance(evaluator._measurement, ExactReferenceMeasurement)


def test_production_runtime_builds_and_closes_llm_judge_once() -> None:
    """Own one explicit judge resource for the entire application lifetime."""
    events: list[str] = []
    runtime = asyncio.run(
        build_production_runtime(
            llm_judge_settings(),
            observability=RecordingObservability(events),
            builders=component_builders(events),
        )
    )
    evaluator = runtime.dependencies.evaluator

    asyncio.run(runtime.aclose())
    asyncio.run(runtime.aclose())

    assert not isinstance(evaluator, FakeEvaluator)
    assert isinstance(evaluator, LLMJudgeEvaluator)
    assert events.count("build:judge") == 1
    assert events.count("close:judge") == 1
    assert events[-6:] == [
        "close:cosmos",
        "close:redis",
        "close:embedding",
        "close:judge",
        "close:foundry",
        "close:telemetry",
    ]


def test_partial_startup_failure_closes_owned_judge_resource() -> None:
    """Release judge transport when a later production component fails to build."""
    events: list[str] = []
    startup_error = RuntimeError("embedding build failed")

    with pytest.raises(RuntimeError, match="embedding build failed") as captured:
        asyncio.run(
            build_production_runtime(
                llm_judge_settings(),
                observability=RecordingObservability(events),
                builders=component_builders(
                    events,
                    embedding_build_error=startup_error,
                    judge_close_error=True,
                ),
            )
        )

    assert captured.value is startup_error
    assert events == [
        "build:foundry",
        "build:judge",
        "build:embedding",
        "close:judge",
        "close:foundry",
        "close:telemetry",
    ]


def test_production_runtime_unpriced_catalog_keeps_cost_unavailable() -> None:
    """Keep monetary cost unavailable when no reviewed rates are configured."""
    events: list[str] = []
    runtime = asyncio.run(
        build_production_runtime(
            production_settings(),
            observability=RecordingObservability(events),
            builders=component_builders(events),
        )
    )
    calculated = runtime.dependencies.cost_calculator.calculate(
        ModelUsage(
            run_id="run-cost",
            provider=FOUNDRY_PROVIDER_NAME,
            deployment="small",
            model_role=ModelRole.SMALL,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            latency_ms=5,
        )
    )
    asyncio.run(runtime.aclose())

    assert calculated is None


def test_production_runtime_prices_usage_from_configured_catalog() -> None:
    """Assemble a catalog keyed to the real provider and deployment identities."""
    events: list[str] = []
    settings = production_settings().model_copy(
        update={
            "pricing_catalog_version": "foundry-apim-2026-01-01",
            "pricing_small_input_rate_per_million_tokens": Decimal("0.15"),
            "pricing_small_output_rate_per_million_tokens": Decimal("0.60"),
            "pricing_strong_input_rate_per_million_tokens": Decimal("2.50"),
            "pricing_strong_output_rate_per_million_tokens": Decimal("10.00"),
            "pricing_embedding_input_rate_per_million_tokens": Decimal("0.02"),
        }
    )
    runtime = asyncio.run(
        build_production_runtime(
            settings,
            observability=RecordingObservability(events),
            builders=component_builders(events),
        )
    )
    calculated = runtime.dependencies.cost_calculator.calculate(
        ModelUsage(
            run_id="run-cost",
            provider=FOUNDRY_PROVIDER_NAME,
            deployment="small",
            model_role=ModelRole.SMALL,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            latency_ms=5,
        )
    )
    asyncio.run(runtime.aclose())

    assert calculated is not None
    assert calculated.amount == Decimal("0.75")
    assert calculated.provenance.catalog_version == "foundry-apim-2026-01-01"
    assert calculated.provenance.currency == "USD"


def test_production_runtime_prices_judge_from_its_configured_deployment() -> None:
    """Key evaluator economics to explicit judge identity and catalog provenance."""
    events: list[str] = []
    settings = llm_judge_settings(
        pricing_catalog_version="foundry-apim-2026-01-01",
        pricing_small_input_rate_per_million_tokens=Decimal("0.15"),
        pricing_small_output_rate_per_million_tokens=Decimal("0.60"),
        pricing_strong_input_rate_per_million_tokens=Decimal("2.50"),
        pricing_strong_output_rate_per_million_tokens=Decimal("10.00"),
        pricing_judge_input_rate_per_million_tokens=Decimal("0.25"),
        pricing_judge_output_rate_per_million_tokens=Decimal("1.25"),
        pricing_embedding_input_rate_per_million_tokens=Decimal("0.02"),
    )
    runtime = asyncio.run(
        build_production_runtime(
            settings,
            observability=RecordingObservability(events),
            builders=component_builders(events),
        )
    )

    calculated = runtime.dependencies.cost_calculator.calculate(
        ModelUsage(
            run_id="run-cost",
            provider=FOUNDRY_PROVIDER_NAME,
            deployment="judge",
            model_role=ModelRole.JUDGE,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            latency_ms=5,
        )
    )
    asyncio.run(runtime.aclose())

    assert calculated is not None
    assert calculated.amount == Decimal("1.50")
    assert calculated.provenance.catalog_version == "foundry-apim-2026-01-01"
    assert calculated.provenance.currency == "USD"


def test_cache_disabled_runtime_prices_only_active_model_roles() -> None:
    """Keep complete generator and judge cost without an embedding catalog entry."""
    events: list[str] = []
    settings = llm_judge_settings(
        semantic_cache_enabled=False,
        production_cost_measurement_required=True,
        pricing_catalog_version="foundry-apim-2026-01-01",
        pricing_small_input_rate_per_million_tokens=Decimal("0.15"),
        pricing_small_output_rate_per_million_tokens=Decimal("0.60"),
        pricing_strong_input_rate_per_million_tokens=Decimal("2.50"),
        pricing_strong_output_rate_per_million_tokens=Decimal("10.00"),
        pricing_judge_input_rate_per_million_tokens=Decimal("0.25"),
        pricing_judge_output_rate_per_million_tokens=Decimal("1.25"),
    )
    runtime = asyncio.run(
        build_production_runtime(
            settings,
            observability=RecordingObservability(events),
            builders=component_builders(events),
        )
    )

    strong = runtime.dependencies.cost_calculator.calculate(
        ModelUsage(
            run_id="run-cost",
            provider=FOUNDRY_PROVIDER_NAME,
            deployment="strong",
            model_role=ModelRole.STRONG,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            latency_ms=5,
        )
    )
    judge = runtime.dependencies.cost_calculator.calculate(
        ModelUsage(
            run_id="run-cost",
            provider=FOUNDRY_PROVIDER_NAME,
            deployment="judge",
            model_role=ModelRole.JUDGE,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            latency_ms=5,
        )
    )
    asyncio.run(runtime.aclose())

    assert strong is not None
    assert strong.amount == Decimal("12.50")
    assert judge is not None
    assert judge.amount == Decimal("1.50")
    assert "build:embedding" not in events
    assert "build:redis" not in events


def test_production_pricing_names_colliding_role_deployments() -> None:
    """Fail closed with the exact roles when a deployment is shared across keys."""
    events: list[str] = []
    settings = llm_judge_settings(
        judge_deployment="small",
        pricing_catalog_version="foundry-apim-2026-01-01",
        pricing_small_input_rate_per_million_tokens=Decimal("0.15"),
        pricing_small_output_rate_per_million_tokens=Decimal("0.60"),
        pricing_strong_input_rate_per_million_tokens=Decimal("2.50"),
        pricing_strong_output_rate_per_million_tokens=Decimal("10.00"),
        pricing_judge_input_rate_per_million_tokens=Decimal("0.25"),
        pricing_judge_output_rate_per_million_tokens=Decimal("1.25"),
        pricing_embedding_input_rate_per_million_tokens=Decimal("0.02"),
    )

    with pytest.raises(
        ValueError,
        match="SMALL and JUDGE roles both use deployment 'small'",
    ):
        asyncio.run(
            build_production_runtime(
                settings,
                observability=RecordingObservability(events),
                builders=component_builders(events),
            )
        )


def test_production_runtime_requires_pricing_when_cost_measurement_required() -> None:
    """Fail production startup before construction when required pricing is absent."""
    events: list[str] = []
    settings = production_settings().model_copy(
        update={"production_cost_measurement_required": True}
    )

    with pytest.raises(ValueError, match="Production cost measurement requires"):
        asyncio.run(
            build_production_runtime(
                settings,
                observability=RecordingObservability(events),
                builders=component_builders(events),
            )
        )

    assert events == []
