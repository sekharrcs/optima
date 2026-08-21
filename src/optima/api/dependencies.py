"""Application-scoped runtime dependencies for execution routes."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

import httpx
from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential, ManagedIdentityCredential

from optima.cache import SemanticCache
from optima.config import AppSettings, FoundryAuthMode
from optima.context import ContextReducer, TokenCounter
from optima.context.safety import ContextReducerSafetyPolicy
from optima.cost import CostCalculator
from optima.domain.execution import ModelRole
from optima.evaluation import QualityEvaluator
from optima.execution.executor import system_utc_now
from optima.providers import (
    ApiKeyAuthentication,
    EntraTokenAuthentication,
    FoundryAuthentication,
    FoundryModelProvider,
    ModelProvider,
    MonotonicClock,
)


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
    semantic_cache: SemanticCache | None = None
    context_reducer: ContextReducer | None = None
    token_counter: TokenCounter | None = None
    context_reducer_safety_policy: ContextReducerSafetyPolicy | None = None
    monotonic_clock: MonotonicClock | None = None
    utc_now: Callable[[], datetime] = system_utc_now
    run_id_factory: Callable[[], str] = new_run_id
    correlation_id_factory: Callable[[], str] = new_correlation_id


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
