"""Production FastAPI composition and application-lifetime resource ownership."""

import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from fastapi import FastAPI

from optima.api.app import create_app
from optima.api.dependencies import (
    ExecutionDependencies,
    build_foundry_embedding_provider,
    build_foundry_provider_pair,
)
from optima.cache import (
    SemanticCache,
    build_redis_semantic_cache_resources,
    ensure_redis_semantic_cache_index,
)
from optima.cache.contracts import SemanticCacheEmbeddingProvider
from optima.cache.redis import RedisSearchClient
from optima.config import AppSettings, ProductionEvaluatorMode
from optima.context import DeterministicExtractiveReducer, RegexTokenCounter
from optima.context.safety import DeterministicExtractiveSafetyPolicy
from optima.cost import CostCalculator, PriceCatalog, PriceCatalogEntry
from optima.evaluation import DeterministicEvaluator, ExactReferenceMeasurement
from optima.observability import Observability
from optima.observability.azure_monitor import build_observability
from optima.providers import FOUNDRY_PROVIDER_NAME, ModelProvider
from optima.storage import (
    RunHistoryStore,
    build_cosmos_run_history_resources,
)

_logger = logging.getLogger(__name__)

AsyncCloser = Callable[[], Awaitable[None]]
IndexBootstrap = Callable[..., Awaitable[None]]


class FoundryResources(Protocol):
    """Role providers and close boundary required by production composition."""

    @property
    def small_provider(self) -> ModelProvider:
        """Return the configured SMALL role provider."""
        ...

    @property
    def strong_provider(self) -> ModelProvider:
        """Return the configured STRONG role provider."""
        ...

    async def aclose(self) -> None:
        """Close Foundry transports and credentials."""
        ...


class EmbeddingResources(Protocol):
    """Embedding provider and close boundary required by production composition."""

    @property
    def provider(self) -> SemanticCacheEmbeddingProvider:
        """Return the configured embedding provider."""
        ...

    async def aclose(self) -> None:
        """Close embedding transport and credential."""
        ...


class RedisResources(Protocol):
    """Cache, client, and close boundary required by production composition."""

    @property
    def cache(self) -> SemanticCache:
        """Return the configured semantic cache."""
        ...

    @property
    def client(self) -> RedisSearchClient:
        """Return the client used by startup index bootstrap."""
        ...

    async def aclose(self) -> None:
        """Close Redis and its credential provider."""
        ...


class CosmosResources(Protocol):
    """Run-history store and close boundary required by production composition."""

    @property
    def store(self) -> RunHistoryStore:
        """Return the configured run-history store."""
        ...

    async def aclose(self) -> None:
        """Close Cosmos and its credential."""
        ...


@dataclass(frozen=True)
class ProductionComponentBuilders:
    """Injectable constructors for independently testing lifecycle ownership."""

    foundry: Callable[[AppSettings], FoundryResources]
    embedding: Callable[[AppSettings], EmbeddingResources]
    redis: Callable[
        [AppSettings, SemanticCacheEmbeddingProvider],
        RedisResources,
    ]
    cosmos: Callable[[AppSettings], CosmosResources]
    bootstrap_index: IndexBootstrap


DEFAULT_PRODUCTION_BUILDERS = ProductionComponentBuilders(
    foundry=build_foundry_provider_pair,
    embedding=build_foundry_embedding_provider,
    redis=build_redis_semantic_cache_resources,
    cosmos=build_cosmos_run_history_resources,
    bootstrap_index=ensure_redis_semantic_cache_index,
)


@dataclass
class ProductionRuntime:
    """Own one immutable dependency graph and its close-once resources."""

    dependencies: ExecutionDependencies
    _closers: tuple[AsyncCloser, ...] = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def aclose(self) -> None:
        """Close all resources once in reverse construction order."""
        if self._closed:
            return
        self._closed = True
        await _close_owned(self._closers)


class _DependencyHolder:
    """Keep lifespan state local to one FastAPI application instance."""

    def __init__(self) -> None:
        self.dependencies: ExecutionDependencies | None = None

    def resolve(self) -> ExecutionDependencies | None:
        return self.dependencies


def _build_price_catalog(settings: AppSettings) -> PriceCatalog:
    """Assemble the reviewed production price catalog from configured rates.

    Absent pricing yields an explicit unpriced catalog so monetary cost stays
    unavailable rather than fabricated. Reviewed rates are keyed to the exact
    Foundry provider and deployment identities the runtime actually reports, so
    embedding output has no billable tokens and carries a zero output rate.
    """
    inputs = settings.production_pricing_inputs()
    if inputs is None:
        return PriceCatalog(
            version="runtime-unpriced-v1",
            currency=settings.pricing_currency,
            entries=(),
        )
    foundry = settings.foundry_provider_configuration()
    redis = settings.redis_semantic_cache_configuration()
    if foundry is None or redis is None:
        raise AssertionError(
            "validated production settings require Foundry and Redis for pricing"
        )
    return PriceCatalog(
        version=inputs.catalog_version,
        currency=inputs.currency,
        entries=(
            PriceCatalogEntry(
                provider=FOUNDRY_PROVIDER_NAME,
                deployment=foundry.small_deployment,
                input_rate_per_million_tokens=(
                    inputs.small.input_rate_per_million_tokens
                ),
                output_rate_per_million_tokens=(
                    inputs.small.output_rate_per_million_tokens
                ),
                cached_input_rate_per_million_tokens=(
                    inputs.small.cached_input_rate_per_million_tokens
                ),
            ),
            PriceCatalogEntry(
                provider=FOUNDRY_PROVIDER_NAME,
                deployment=foundry.strong_deployment,
                input_rate_per_million_tokens=(
                    inputs.strong.input_rate_per_million_tokens
                ),
                output_rate_per_million_tokens=(
                    inputs.strong.output_rate_per_million_tokens
                ),
                cached_input_rate_per_million_tokens=(
                    inputs.strong.cached_input_rate_per_million_tokens
                ),
            ),
            PriceCatalogEntry(
                provider=FOUNDRY_PROVIDER_NAME,
                deployment=redis.embedding_deployment,
                input_rate_per_million_tokens=(
                    inputs.embedding_input_rate_per_million_tokens
                ),
                output_rate_per_million_tokens=Decimal("0"),
            ),
        ),
    )


async def build_production_runtime(
    settings: AppSettings,
    *,
    observability: Observability,
    builders: ProductionComponentBuilders = DEFAULT_PRODUCTION_BUILDERS,
) -> ProductionRuntime:
    """Construct the production graph or clean every partially owned resource."""
    settings.validate_production_runtime()
    closers: list[AsyncCloser] = [_async_close(observability.close)]
    try:
        foundry = builders.foundry(settings)
        closers.append(foundry.aclose)
        embedding = builders.embedding(settings)
        closers.append(embedding.aclose)
        redis = builders.redis(settings, embedding.provider)
        closers.append(redis.aclose)
        redis_configuration = settings.redis_semantic_cache_configuration()
        if redis_configuration is None:
            raise AssertionError("validated production settings require Redis")
        await builders.bootstrap_index(
            redis.client,
            index_name=redis_configuration.index_name,
            embedding_profile=redis_configuration.embedding_profile(),
        )
        cosmos = builders.cosmos(settings)
        closers.append(cosmos.aclose)

        if (
            settings.production_evaluator_mode
            is not ProductionEvaluatorMode.EXACT_REFERENCE
        ):
            raise AssertionError("validated production settings require an evaluator")
        token_counter = RegexTokenCounter()
        dependencies = ExecutionDependencies(
            settings=settings,
            small_provider=foundry.small_provider,
            strong_provider=foundry.strong_provider,
            evaluator=DeterministicEvaluator(
                measurement=ExactReferenceMeasurement(),
            ),
            cost_calculator=CostCalculator(_build_price_catalog(settings)),
            run_history_store=cosmos.store,
            semantic_cache=redis.cache,
            context_reducer=DeterministicExtractiveReducer(token_counter),
            token_counter=token_counter,
            context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
            observability=observability,
        )
        return ProductionRuntime(dependencies=dependencies, _closers=tuple(closers))
    except BaseException:
        await _close_owned(tuple(closers))
        raise


def create_production_app(
    *,
    settings: AppSettings | None = None,
    builders: ProductionComponentBuilders = DEFAULT_PRODUCTION_BUILDERS,
    observability_builder: Callable[[AppSettings], Observability] = build_observability,
) -> FastAPI:
    """Create the Azure-backed API without any fake-provider fallback."""
    resolved_settings = settings or AppSettings()
    resolved_settings.validate_production_runtime()
    observability = observability_builder(resolved_settings)
    holder = _DependencyHolder()
    started = False

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal started
        if started:
            raise RuntimeError("Production application lifespan may only start once")
        started = True
        runtime = await build_production_runtime(
            resolved_settings,
            observability=observability,
            builders=builders,
        )
        holder.dependencies = runtime.dependencies
        try:
            yield
        finally:
            holder.dependencies = None
            await runtime.aclose()

    try:
        application = create_app(
            execution_dependency_resolver=holder.resolve,
            lifespan=lifespan,
        )
        observability.instrument_fastapi(application)
        return application
    except BaseException:
        observability.close()
        raise


def _async_close(close: Callable[[], object]) -> AsyncCloser:
    async def invoke() -> None:
        result = close()
        if inspect.isawaitable(result):
            await result

    return invoke


async def _close_owned(closers: tuple[AsyncCloser, ...]) -> None:
    for close in reversed(closers):
        try:
            await close()
        except BaseException as error:
            _logger.warning(
                "Production runtime resource cleanup failed: %s",
                type(error).__name__,
            )
