"""Typed application settings for OPTIMA capabilities and thresholds."""

from enum import StrEnum
from typing import Annotated
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from optima.domain.quality_contract import QualityThresholds
from optima.immutable import ImmutableModel
from optima.planner.models import ModuleConfiguration, PlannerThresholds

ConfiguredQualityScore = Annotated[
    float,
    Field(ge=0.0, le=1.0, allow_inf_nan=False),
]
PositiveInteger = Annotated[int, Field(gt=0)]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
BoundedHistoryLimit = Annotated[int, Field(gt=0, le=100)]
BoundedRetryCount = Annotated[int, Field(gt=0, le=10)]
BoundedTimeoutSeconds = Annotated[
    float,
    Field(gt=0, le=120, allow_inf_nan=False),
]
NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]


class FoundryAuthMode(StrEnum):
    """Explicit authentication source for the Foundry/APIM provider."""

    API_KEY = "API_KEY"
    AZURE_CLI = "AZURE_CLI"
    MANAGED_IDENTITY = "MANAGED_IDENTITY"


class CosmosAuthMode(StrEnum):
    """Explicit authentication source for Cosmos run history."""

    ACCOUNT_KEY = "ACCOUNT_KEY"
    AZURE_CLI = "AZURE_CLI"
    MANAGED_IDENTITY = "MANAGED_IDENTITY"


class CosmosRunHistoryConfiguration(ImmutableModel):
    """Complete validated settings for one application-lifetime Cosmos client."""

    endpoint: NonEmptyString
    database_name: NonEmptyString
    container_name: NonEmptyString
    auth_mode: CosmosAuthMode
    account_key: SecretStr | None = None
    managed_identity_client_id: NonEmptyString | None = None
    history_list_limit: BoundedHistoryLimit = 50
    timeout_seconds: BoundedTimeoutSeconds = 10.0
    retry_total: BoundedRetryCount = 3

    @model_validator(mode="after")
    def validate_authentication(self) -> "CosmosRunHistoryConfiguration":
        """Require an absolute endpoint and exactly one explicit auth mode."""
        parsed = urlparse(self.endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.params
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Cosmos endpoint must be an absolute HTTPS account root")

        if self.auth_mode is CosmosAuthMode.ACCOUNT_KEY:
            if self.account_key is None or not self.account_key.get_secret_value():
                raise ValueError("ACCOUNT_KEY mode requires cosmos_account_key")
            if self.managed_identity_client_id is not None:
                raise ValueError(
                    "ACCOUNT_KEY mode cannot configure a managed identity client ID"
                )
            return self

        if self.account_key is not None:
            raise ValueError(
                "Microsoft Entra modes cannot configure cosmos_account_key"
            )
        if (
            self.auth_mode is CosmosAuthMode.AZURE_CLI
            and self.managed_identity_client_id is not None
        ):
            raise ValueError(
                "AZURE_CLI mode cannot configure a managed identity client ID"
            )
        return self


class FoundryProviderConfiguration(ImmutableModel):
    """Complete validated settings for one shared Foundry provider client."""

    base_url: NonEmptyString
    small_deployment: NonEmptyString
    strong_deployment: NonEmptyString
    auth_mode: FoundryAuthMode
    api_key: SecretStr | None = None
    token_scope: NonEmptyString | None = None
    managed_identity_client_id: NonEmptyString | None = None
    timeout_seconds: PositiveSeconds = 30.0

    @model_validator(mode="after")
    def validate_authentication(self) -> "FoundryProviderConfiguration":
        """Require one complete authentication mode without silent fallback."""
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.params
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/").endswith("/openai/v1") is False
        ):
            raise ValueError(
                "Foundry base URL must be an absolute HTTPS /openai/v1 API root"
            )

        if self.auth_mode is FoundryAuthMode.API_KEY:
            if self.api_key is None or not self.api_key.get_secret_value():
                raise ValueError("API_KEY mode requires foundry_api_key")
            if self.token_scope is not None:
                raise ValueError("API_KEY mode cannot configure foundry_token_scope")
            if self.managed_identity_client_id is not None:
                raise ValueError(
                    "API_KEY mode cannot configure a managed identity client ID"
                )
            return self

        if self.api_key is not None:
            raise ValueError("Microsoft Entra modes cannot configure foundry_api_key")
        if self.token_scope is None:
            raise ValueError("Microsoft Entra modes require foundry_token_scope")
        if (
            self.auth_mode is FoundryAuthMode.AZURE_CLI
            and self.managed_identity_client_id is not None
        ):
            raise ValueError(
                "AZURE_CLI mode cannot configure a managed identity client ID"
            )
        return self


class AppSettings(BaseSettings):
    """Configure MVP optimization modules and quality thresholds."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="OPTIMA_",
        extra="ignore",
    )

    semantic_cache_enabled: bool = True
    context_reduction_enabled: bool = True
    historical_policy_enabled: bool = True
    foundry_router_comparator_enabled: bool = False
    standard_quality_threshold: ConfiguredQualityScore = 0.80
    high_quality_threshold: ConfiguredQualityScore = 0.90
    critical_quality_threshold: ConfiguredQualityScore = 0.95
    cache_similarity_threshold: ConfiguredQualityScore = 0.95
    context_reduction_consider_tokens: PositiveInteger = 4_000
    context_reduction_required_tokens: PositiveInteger = 8_000
    history_minimum_samples: PositiveInteger = 20
    history_small_prefer_pass_rate: ConfiguredQualityScore = 0.95
    history_small_avoid_pass_rate: ConfiguredQualityScore = 0.70
    foundry_base_url: str | None = None
    foundry_small_deployment: str | None = None
    foundry_strong_deployment: str | None = None
    foundry_auth_mode: FoundryAuthMode | None = None
    foundry_api_key: SecretStr | None = None
    foundry_token_scope: str | None = None
    foundry_managed_identity_client_id: str | None = None
    foundry_timeout_seconds: PositiveSeconds = 30.0
    cosmos_endpoint: str | None = None
    cosmos_database_name: str | None = None
    cosmos_container_name: str | None = None
    cosmos_auth_mode: CosmosAuthMode | None = None
    cosmos_account_key: SecretStr | None = None
    cosmos_managed_identity_client_id: str | None = None
    cosmos_history_list_limit: BoundedHistoryLimit = 50
    cosmos_timeout_seconds: BoundedTimeoutSeconds = 10.0
    cosmos_retry_total: BoundedRetryCount = 3

    @model_validator(mode="after")
    def validate_quality_threshold_order(self) -> "AppSettings":
        """Require stricter profiles to have nondecreasing thresholds."""
        self.quality_thresholds()
        self.planner_thresholds()
        self.foundry_provider_configuration()
        self.cosmos_run_history_configuration()
        return self

    def quality_thresholds(self) -> QualityThresholds:
        """Build the domain threshold configuration."""
        return QualityThresholds(
            standard=self.standard_quality_threshold,
            high=self.high_quality_threshold,
            critical=self.critical_quality_threshold,
        )

    def module_configuration(self) -> ModuleConfiguration:
        """Build the immutable module configuration supplied to the planner."""
        return ModuleConfiguration(
            semantic_cache_enabled=self.semantic_cache_enabled,
            context_reduction_enabled=self.context_reduction_enabled,
            historical_policy_enabled=self.historical_policy_enabled,
            foundry_router_comparator_enabled=(self.foundry_router_comparator_enabled),
        )

    def planner_thresholds(self) -> PlannerThresholds:
        """Build validated deterministic Planner V1 thresholds."""
        return PlannerThresholds(
            cache_similarity_threshold=self.cache_similarity_threshold,
            context_reduction_consider_tokens=(self.context_reduction_consider_tokens),
            context_reduction_required_tokens=(self.context_reduction_required_tokens),
            history_minimum_samples=self.history_minimum_samples,
            history_small_prefer_pass_rate=(self.history_small_prefer_pass_rate),
            history_small_avoid_pass_rate=self.history_small_avoid_pass_rate,
        )

    def foundry_provider_configuration(
        self,
    ) -> FoundryProviderConfiguration | None:
        """Build complete provider settings only when Azure composition is requested."""
        optional_values = (
            self.foundry_base_url,
            self.foundry_small_deployment,
            self.foundry_strong_deployment,
            self.foundry_auth_mode,
            self.foundry_api_key,
            self.foundry_token_scope,
            self.foundry_managed_identity_client_id,
        )
        if all(value is None for value in optional_values):
            return None
        if (
            self.foundry_base_url is None
            or self.foundry_small_deployment is None
            or self.foundry_strong_deployment is None
            or self.foundry_auth_mode is None
        ):
            raise ValueError(
                "Foundry composition requires base URL, both deployments, and auth mode"
            )
        return FoundryProviderConfiguration(
            base_url=self.foundry_base_url,
            small_deployment=self.foundry_small_deployment,
            strong_deployment=self.foundry_strong_deployment,
            auth_mode=self.foundry_auth_mode,
            api_key=self.foundry_api_key,
            token_scope=self.foundry_token_scope,
            managed_identity_client_id=self.foundry_managed_identity_client_id,
            timeout_seconds=self.foundry_timeout_seconds,
        )

    def cosmos_run_history_configuration(
        self,
    ) -> CosmosRunHistoryConfiguration | None:
        """Build run-history settings only when Cosmos composition is requested."""
        optional_values = (
            self.cosmos_endpoint,
            self.cosmos_database_name,
            self.cosmos_container_name,
            self.cosmos_auth_mode,
            self.cosmos_account_key,
            self.cosmos_managed_identity_client_id,
        )
        if all(value is None for value in optional_values):
            return None
        if (
            self.cosmos_endpoint is None
            or self.cosmos_database_name is None
            or self.cosmos_container_name is None
            or self.cosmos_auth_mode is None
        ):
            raise ValueError(
                "Cosmos run history requires endpoint, database, container, "
                "and auth mode"
            )
        return CosmosRunHistoryConfiguration(
            endpoint=self.cosmos_endpoint,
            database_name=self.cosmos_database_name,
            container_name=self.cosmos_container_name,
            auth_mode=self.cosmos_auth_mode,
            account_key=self.cosmos_account_key,
            managed_identity_client_id=self.cosmos_managed_identity_client_id,
            history_list_limit=self.cosmos_history_list_limit,
            timeout_seconds=self.cosmos_timeout_seconds,
            retry_total=self.cosmos_retry_total,
        )
