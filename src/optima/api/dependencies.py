"""Application-scoped runtime dependencies for execution routes."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

import httpx
from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential, ManagedIdentityCredential

from optima.api.security import ExecutionConcurrencyLimiter
from optima.cache import SemanticCache
from optima.config import AppSettings, FoundryAuthMode, FoundryProviderConfiguration
from optima.context import ContextReducer, TokenCounter
from optima.context.safety import ContextReducerSafetyPolicy
from optima.cost import CostCalculator
from optima.domain.execution import ModelRole
from optima.evaluation import QualityEvaluator
from optima.execution.executor import system_utc_now
from optima.observability import Observability
from optima.providers import (
    ApiKeyAuthentication,
    EntraTokenAuthentication,
    FoundryAuthentication,
    FoundryEmbeddingProvider,
    FoundryModelProvider,
    ModelProvider,
    MonotonicClock,
)
from optima.storage import RunHistoryStore


def new_run_id() -> str:
    """Create one opaque run identifier."""
    return f"run-{uuid4()}"


def new_correlation_id() -> str:
    """Create one opaque correlation identifier."""
    return f"correlation-{uuid4()}"


@dataclass(frozen=True)
class ExecutionDependencies:
    """Immutable application composition for one API instance."""

    settings: AppSettings
    small_provider: ModelProvider
    strong_provider: ModelProvider
    evaluator: QualityEvaluator
    cost_calculator: CostCalculator
    run_history_store: RunHistoryStore | None = None
    semantic_cache: SemanticCache | None = None
    context_reducer: ContextReducer | None = None
    token_counter: TokenCounter | None = None
    context_reducer_safety_policy: ContextReducerSafetyPolicy | None = None
    monotonic_clock: MonotonicClock | None = None
    utc_now: Callable[[], datetime] = system_utc_now
    run_id_factory: Callable[[], str] = new_run_id
    correlation_id_factory: Callable[[], str] = new_correlation_id
    observability: Observability | None = None
    execution_limiter: ExecutionConcurrencyLimiter | None = None


@dataclass(frozen=True)
class FoundryProviderPair:
    """Role-specific providers sharing one explicitly owned HTTP client."""

    small_provider: FoundryModelProvider
    strong_provider: FoundryModelProvider
    http_client: httpx.AsyncClient = field(repr=False)
    credential: TokenCredential | None = field(default=None, repr=False)

    async def aclose(self) -> None:
        """Close shared transport and any selected Azure Identity credential."""
        try:
            await self.http_client.aclose()
        finally:
            if self.credential is not None:
                close = getattr(self.credential, "close", None)
                if callable(close):
                    close()


@dataclass(frozen=True)
class FoundryJudgeResources:
    """One explicit JUDGE-role provider with owned transport and credential."""

    provider: FoundryModelProvider
    http_client: httpx.AsyncClient = field(repr=False)
    credential: TokenCredential | None = field(default=None, repr=False)

    async def aclose(self) -> None:
        """Close judge transport and any selected Azure Identity credential."""
        try:
            await self.http_client.aclose()
        finally:
            if self.credential is not None:
                close = getattr(self.credential, "close", None)
                if callable(close):
                    close()


def build_foundry_provider_pair(
    settings: AppSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    monotonic_clock: MonotonicClock | None = None,
) -> FoundryProviderPair:
    """Create both role adapters from one complete explicit Azure configuration."""
    configuration = settings.foundry_provider_configuration()
    if configuration is None:
        raise ValueError("Foundry provider settings are not configured")

    credential: TokenCredential | None = None
    authentication: FoundryAuthentication
    if configuration.auth_mode is FoundryAuthMode.API_KEY:
        if configuration.api_key is None:
            raise AssertionError("validated API-key configuration requires a key")
        authentication = ApiKeyAuthentication(configuration.api_key.get_secret_value())
    else:
        if configuration.token_scope is None:
            raise AssertionError("validated Entra configuration requires a scope")
        if configuration.auth_mode is FoundryAuthMode.AZURE_CLI:
            credential = AzureCliCredential()
        else:
            credential = ManagedIdentityCredential(
                client_id=configuration.managed_identity_client_id
            )
        authentication = EntraTokenAuthentication(
            credential,
            configuration.token_scope,
        )

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(configuration.timeout_seconds),
        transport=transport,
    )
    return FoundryProviderPair(
        small_provider=FoundryModelProvider(
            base_url=configuration.base_url,
            deployment_name=configuration.small_deployment,
            model_role=ModelRole.SMALL,
            authentication=authentication,
            client=client,
            clock=monotonic_clock,
        ),
        strong_provider=FoundryModelProvider(
            base_url=configuration.base_url,
            deployment_name=configuration.strong_deployment,
            model_role=ModelRole.STRONG,
            authentication=authentication,
            client=client,
            clock=monotonic_clock,
        ),
        http_client=client,
        credential=credential,
    )


def build_foundry_judge_provider(
    settings: AppSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    monotonic_clock: MonotonicClock | None = None,
) -> FoundryJudgeResources:
    """Create one separately configured JUDGE-role Foundry adapter."""
    configuration = settings.foundry_provider_configuration()
    if configuration is None:
        raise ValueError("Foundry provider settings are not configured")
    judge = settings.judge_provider_configuration()
    if judge is None:
        raise ValueError("LLM judge provider settings are not configured")

    credential: TokenCredential | None = None
    authentication: FoundryAuthentication
    if configuration.auth_mode is FoundryAuthMode.API_KEY:
        if configuration.api_key is None:
            raise AssertionError("validated API-key configuration requires a key")
        authentication = ApiKeyAuthentication(configuration.api_key.get_secret_value())
    else:
        if configuration.token_scope is None:
            raise AssertionError("validated Entra configuration requires a scope")
        if configuration.auth_mode is FoundryAuthMode.AZURE_CLI:
            credential = AzureCliCredential()
        else:
            credential = ManagedIdentityCredential(
                client_id=configuration.managed_identity_client_id
            )
        authentication = EntraTokenAuthentication(
            credential,
            configuration.token_scope,
        )

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(judge.timeout_seconds),
        transport=transport,
    )
    return FoundryJudgeResources(
        provider=FoundryModelProvider(
            base_url=configuration.base_url,
            deployment_name=judge.deployment,
            model_role=ModelRole.JUDGE,
            authentication=authentication,
            client=client,
            expected_response_model=judge.model,
            clock=monotonic_clock,
        ),
        http_client=client,
        credential=credential,
    )


@dataclass(frozen=True)
class FoundryEmbeddingResources:
    """One embedding provider with its explicitly owned transport and credential."""

    provider: FoundryEmbeddingProvider
    http_client: httpx.AsyncClient = field(repr=False)
    credential: TokenCredential | None = field(default=None, repr=False)

    async def aclose(self) -> None:
        """Close the owned transport and any selected Azure Identity credential."""
        try:
            await self.http_client.aclose()
        finally:
            if self.credential is not None:
                close = getattr(self.credential, "close", None)
                if callable(close):
                    close()


def build_foundry_embedding_provider(
    settings: AppSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FoundryEmbeddingResources:
    """Compose one embedding provider from Foundry auth and the Redis cache profile."""
    configuration: FoundryProviderConfiguration | None = (
        settings.foundry_provider_configuration()
    )
    if configuration is None:
        raise ValueError("Foundry provider settings are not configured")
    redis_configuration = settings.redis_semantic_cache_configuration()
    if redis_configuration is None:
        raise ValueError("Redis semantic-cache settings are not configured")
    profile = redis_configuration.embedding_profile()

    credential: TokenCredential | None = None
    authentication: FoundryAuthentication
    if configuration.auth_mode is FoundryAuthMode.API_KEY:
        if configuration.api_key is None:
            raise AssertionError("validated API-key configuration requires a key")
        authentication = ApiKeyAuthentication(configuration.api_key.get_secret_value())
    else:
        if configuration.token_scope is None:
            raise AssertionError("validated Entra configuration requires a scope")
        if configuration.auth_mode is FoundryAuthMode.AZURE_CLI:
            credential = AzureCliCredential()
        else:
            credential = ManagedIdentityCredential(
                client_id=configuration.managed_identity_client_id
            )
        authentication = EntraTokenAuthentication(
            credential,
            configuration.token_scope,
        )

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(configuration.timeout_seconds),
        transport=transport,
    )
    return FoundryEmbeddingResources(
        provider=FoundryEmbeddingProvider(
            base_url=configuration.base_url,
            profile=profile,
            authentication=authentication,
            client=client,
        ),
        http_client=client,
        credential=credential,
    )
