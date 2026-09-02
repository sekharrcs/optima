"""Typed application settings for OPTIMA capabilities and thresholds."""

import re
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from optima.domain.embedding import EmbeddingProfile, EmbeddingProfileToken
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
BoundedEmbeddingDimension = Annotated[int, Field(gt=0, le=32_768)]
BoundedRedisConnections = Annotated[int, Field(gt=0, le=100)]
BoundedExecutionConcurrency = Annotated[int, Field(gt=0, le=32)]
BoundedExecutionTimeoutSeconds = Annotated[
    float,
    Field(gt=0, le=300, allow_inf_nan=False),
]
BoundedTimeoutSeconds = Annotated[
    float,
    Field(gt=0, le=120, allow_inf_nan=False),
]
BoundedSafetyMarginSeconds = Annotated[
    float,
    Field(ge=0, le=1800, allow_inf_nan=False),
]
BoundedSamplingRatio = Annotated[
    float,
    Field(ge=0.0, le=1.0, allow_inf_nan=False),
]
NonNegativeRate = Annotated[
    Decimal,
    Field(ge=Decimal("0"), allow_inf_nan=False),
]
NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
TelemetryResourceName = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]

_PRODUCTION_CACHE_ONLY_FIELDS = frozenset(
    {
        "cache_similarity_threshold",
        "pricing_embedding_input_rate_per_million_tokens",
        "redis_access_key",
        "redis_auth_mode",
        "redis_embedding_deployment",
        "redis_embedding_dimension",
        "redis_embedding_model",
        "redis_host",
        "redis_index_name",
        "redis_managed_identity_client_id",
        "redis_max_connections",
        "redis_object_id",
        "redis_timeout_seconds",
        "redis_token_acquisition_timeout_seconds",
        "redis_token_expiry_safety_margin_seconds",
        "redis_token_reauth_timeout_seconds",
        "redis_token_renewal_attempts",
        "redis_token_retry_backoff_cap_seconds",
        "redis_token_retry_backoff_seconds",
    }
)
_PRODUCTION_JUDGE_ONLY_FIELDS = frozenset(
    {
        "judge_deployment",
        "judge_model",
        "judge_timeout_seconds",
        "pricing_judge_cached_input_rate_per_million_tokens",
        "pricing_judge_input_rate_per_million_tokens",
        "pricing_judge_output_rate_per_million_tokens",
    }
)


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


class RedisAuthMode(StrEnum):
    """Explicit authentication source for Azure Managed Redis."""

    ACCESS_KEY = "ACCESS_KEY"
    AZURE_CLI = "AZURE_CLI"
    MANAGED_IDENTITY = "MANAGED_IDENTITY"


class ProductionEvaluatorMode(StrEnum):
    """Reviewed evaluator implementations available to production composition.

    ``EXACT_REFERENCE`` is deterministic benchmark measurement and requires caller-
    supplied ``reference_output``. ``LLM_JUDGE`` is reference-free model-generated
    measurement for normal user-facing requests. Production never switches between
    these modes implicitly.
    """

    EXACT_REFERENCE = "EXACT_REFERENCE"
    LLM_JUDGE = "LLM_JUDGE"


class ProductionModelRoleRate(ImmutableModel):
    """Reviewed per-million-token rates for one production model deployment."""

    input_rate_per_million_tokens: NonNegativeRate
    output_rate_per_million_tokens: NonNegativeRate
    cached_input_rate_per_million_tokens: NonNegativeRate | None = None


class ProductionPricingInputs(ImmutableModel):
    """Complete reviewed pricing inputs for production monetary cost measurement."""

    catalog_version: NonEmptyString
    currency: NonEmptyString
    small: ProductionModelRoleRate
    strong: ProductionModelRoleRate
    judge: ProductionModelRoleRate | None = None
    embedding_input_rate_per_million_tokens: NonNegativeRate | None = None


class JudgeProviderConfiguration(ImmutableModel):
    """Explicit Foundry deployment identity and timeout for the evaluator role."""

    deployment: NonEmptyString
    model: NonEmptyString
    timeout_seconds: BoundedTimeoutSeconds = 30.0


class RedisSemanticCacheConfiguration(ImmutableModel):
    """Complete settings for one application-lifetime Redis cache client."""

    host: NonEmptyString
    index_name: NonEmptyString
    embedding_dimension: BoundedEmbeddingDimension
    embedding_model: EmbeddingProfileToken
    embedding_deployment: EmbeddingProfileToken
    auth_mode: RedisAuthMode
    access_key: SecretStr | None = None
    object_id: NonEmptyString | None = None
    managed_identity_client_id: NonEmptyString | None = None
    timeout_seconds: BoundedTimeoutSeconds = 1.0
    max_connections: BoundedRedisConnections = 10
    token_renewal_attempts: BoundedRetryCount = 3
    token_retry_backoff_seconds: BoundedTimeoutSeconds = 0.5
    token_retry_backoff_cap_seconds: BoundedTimeoutSeconds = 5.0
    token_acquisition_timeout_seconds: BoundedTimeoutSeconds = 10.0
    token_reauth_timeout_seconds: BoundedTimeoutSeconds = 10.0
    token_expiry_safety_margin_seconds: BoundedSafetyMarginSeconds = 180.0

    def embedding_profile(self) -> EmbeddingProfile:
        """Build the versioned embedding profile bound to this cache index."""
        return EmbeddingProfile(
            model=self.embedding_model,
            deployment=self.embedding_deployment,
            dimension=self.embedding_dimension,
        )

    @model_validator(mode="after")
    def validate_configuration(self) -> "RedisSemanticCacheConfiguration":
        """Require one Azure endpoint and exactly one explicit auth mode."""
        if (
            re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
                self.host,
            )
            is None
            or ".." in self.host
            or not self.host.endswith(".redis.azure.net")
        ):
            raise ValueError(
                "Redis host must be a lowercase Azure Managed Redis hostname"
            )
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", self.index_name) is None:
            raise ValueError("Redis index name contains unsupported characters")

        if self.auth_mode is RedisAuthMode.ACCESS_KEY:
            if self.access_key is None or not self.access_key.get_secret_value():
                raise ValueError("ACCESS_KEY mode requires redis_access_key")
            if self.object_id is not None:
                raise ValueError("ACCESS_KEY mode cannot configure redis_object_id")
            if self.managed_identity_client_id is not None:
                raise ValueError(
                    "ACCESS_KEY mode cannot configure a managed identity client ID"
                )
            return self

        if self.access_key is not None:
            raise ValueError("Microsoft Entra modes cannot configure redis_access_key")
        if self.object_id is None:
            raise ValueError("Microsoft Entra modes require redis_object_id")
        if (
            self.auth_mode is RedisAuthMode.AZURE_CLI
            and self.managed_identity_client_id is not None
        ):
            raise ValueError(
                "AZURE_CLI mode cannot configure a managed identity client ID"
            )
        return self


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
    small_model: NonEmptyString
    strong_deployment: NonEmptyString
    strong_model: NonEmptyString
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


class ApplicationInsightsConfiguration(ImmutableModel):
    """Complete privacy-conscious Azure Monitor configuration."""

    connection_string: SecretStr
    service_name: TelemetryResourceName = "optima-api"
    service_version: TelemetryResourceName = "0.1.0"
    deployment_environment: TelemetryResourceName = "local"
    sampling_ratio: BoundedSamplingRatio = 1.0
    live_metrics_enabled: bool = False
    performance_counters_enabled: bool = False
    offline_storage_enabled: bool = False
    fastapi_instrumentation_enabled: bool = True
    exclude_health_routes: bool = True

    @model_validator(mode="after")
    def validate_connection_string(self) -> "ApplicationInsightsConfiguration":
        """Reject malformed connection strings before exporter construction."""
        if self.live_metrics_enabled:
            raise ValueError(
                "isolated Application Insights does not support Live Metrics"
            )
        if self.performance_counters_enabled:
            raise ValueError(
                "isolated Application Insights does not support performance counters"
            )
        value = self.connection_string.get_secret_value()
        if (
            not value
            or len(value) > 4096
            or any(character.isspace() for character in value)
        ):
            raise ValueError("Application Insights connection string is malformed")
        segments = value.split(";")
        if any("=" not in segment for segment in segments):
            raise ValueError("Application Insights connection string is malformed")
        parsed: dict[str, str] = {}
        for segment in segments:
            key, segment_value = segment.split("=", 1)
            normalized_key = key.lower()
            if (
                re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key) is None
                or not segment_value
                or normalized_key in parsed
            ):
                raise ValueError("Application Insights connection string is malformed")
            parsed[normalized_key] = segment_value
        supported_keys = {
            "applicationid",
            "authorization",
            "ingestionendpoint",
            "instrumentationkey",
            "liveendpoint",
        }
        if not set(parsed).issubset(supported_keys):
            raise ValueError(
                "Application Insights connection string contains unsupported keys"
            )
        instrumentation_key = parsed.get("instrumentationkey")
        if instrumentation_key is None:
            raise ValueError(
                "Application Insights connection string requires InstrumentationKey"
            )
        try:
            UUID(instrumentation_key)
        except ValueError as error:
            raise ValueError(
                "Application Insights InstrumentationKey must be a UUID"
            ) from error
        ingestion_endpoint = parsed.get("ingestionendpoint")
        if ingestion_endpoint is None:
            raise ValueError(
                "Application Insights connection string requires IngestionEndpoint"
            )
        _validate_application_insights_endpoint(
            ingestion_endpoint,
            allowed_host_suffixes=(
                ".applicationinsights.azure.com",
                ".applicationinsights.azure.us",
                ".applicationinsights.azure.cn",
            ),
        )
        live_endpoint = parsed.get("liveendpoint")
        if live_endpoint is not None:
            _validate_application_insights_endpoint(
                live_endpoint,
                allowed_host_suffixes=(
                    ".livediagnostics.monitor.azure.com",
                    ".livediagnostics.monitor.azure.us",
                    ".livediagnostics.monitor.azure.cn",
                ),
            )
        authorization = parsed.get("authorization")
        if authorization is not None and authorization.lower() != "ikey":
            raise ValueError("Application Insights Authorization must use ikey")
        application_id = parsed.get("applicationid")
        if application_id is not None:
            try:
                UUID(application_id)
            except ValueError as error:
                raise ValueError(
                    "Application Insights ApplicationId must be a UUID"
                ) from error
        return self


def _validate_application_insights_endpoint(
    value: str,
    *,
    allowed_host_suffixes: tuple[str, ...],
) -> None:
    """Require one credential-free HTTPS Azure Application Insights endpoint."""
    parsed = urlparse(value)
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or hostname is None
        or re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
            hostname,
        )
        is None
        or not any(hostname.endswith(suffix) for suffix in allowed_host_suffixes)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "Application Insights endpoints must be credential-free Azure HTTPS roots"
        )


class AppSettings(BaseSettings):
    """Configure MVP optimization modules and quality thresholds."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="OPTIMA_",
        extra="ignore",
    )

    deployment_environment: TelemetryResourceName = "local"
    production_evaluator_mode: ProductionEvaluatorMode | None = None
    production_require_reference_output: bool = False
    production_cost_measurement_required: bool = False
    pricing_catalog_version: str | None = None
    pricing_currency: NonEmptyString = "USD"
    pricing_small_input_rate_per_million_tokens: Decimal | None = None
    pricing_small_output_rate_per_million_tokens: Decimal | None = None
    pricing_small_cached_input_rate_per_million_tokens: Decimal | None = None
    pricing_strong_input_rate_per_million_tokens: Decimal | None = None
    pricing_strong_output_rate_per_million_tokens: Decimal | None = None
    pricing_strong_cached_input_rate_per_million_tokens: Decimal | None = None
    pricing_judge_input_rate_per_million_tokens: Decimal | None = None
    pricing_judge_output_rate_per_million_tokens: Decimal | None = None
    pricing_judge_cached_input_rate_per_million_tokens: Decimal | None = None
    pricing_embedding_input_rate_per_million_tokens: Decimal | None = None
    semantic_cache_enabled: bool = True
    context_reduction_enabled: bool = True
    historical_policy_enabled: bool = True
    foundry_router_comparator_enabled: bool = False
    execution_concurrency_limit: BoundedExecutionConcurrency = 4
    execution_timeout_seconds: BoundedExecutionTimeoutSeconds = 300.0
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
    foundry_small_model: str | None = None
    foundry_strong_deployment: str | None = None
    foundry_strong_model: str | None = None
    foundry_auth_mode: FoundryAuthMode | None = None
    foundry_api_key: SecretStr | None = None
    foundry_token_scope: str | None = None
    foundry_managed_identity_client_id: str | None = None
    foundry_timeout_seconds: PositiveSeconds = 30.0
    judge_deployment: str | None = None
    judge_model: str | None = None
    judge_timeout_seconds: BoundedTimeoutSeconds = 30.0
    cosmos_endpoint: str | None = None
    cosmos_database_name: str | None = None
    cosmos_container_name: str | None = None
    cosmos_auth_mode: CosmosAuthMode | None = None
    cosmos_account_key: SecretStr | None = None
    cosmos_managed_identity_client_id: str | None = None
    cosmos_history_list_limit: BoundedHistoryLimit = 50
    cosmos_timeout_seconds: BoundedTimeoutSeconds = 10.0
    cosmos_retry_total: BoundedRetryCount = 3
    redis_host: str | None = None
    redis_index_name: str | None = None
    redis_embedding_dimension: int | None = None
    redis_embedding_model: str | None = None
    redis_embedding_deployment: str | None = None
    redis_auth_mode: RedisAuthMode | None = None
    redis_access_key: SecretStr | None = None
    redis_object_id: str | None = None
    redis_managed_identity_client_id: str | None = None
    redis_timeout_seconds: BoundedTimeoutSeconds = 1.0
    redis_max_connections: BoundedRedisConnections = 10
    redis_token_renewal_attempts: BoundedRetryCount = 3
    redis_token_retry_backoff_seconds: BoundedTimeoutSeconds = 0.5
    redis_token_retry_backoff_cap_seconds: BoundedTimeoutSeconds = 5.0
    redis_token_acquisition_timeout_seconds: BoundedTimeoutSeconds = 10.0
    redis_token_reauth_timeout_seconds: BoundedTimeoutSeconds = 10.0
    redis_token_expiry_safety_margin_seconds: BoundedSafetyMarginSeconds = 180.0
    application_insights_enabled: bool = False
    application_insights_connection_string: SecretStr | None = None
    application_insights_service_name: TelemetryResourceName = "optima-api"
    application_insights_service_version: TelemetryResourceName = "0.1.0"
    application_insights_deployment_environment: TelemetryResourceName = "local"
    application_insights_sampling_ratio: BoundedSamplingRatio = 1.0
    application_insights_live_metrics_enabled: bool = False
    application_insights_performance_counters_enabled: bool = False
    application_insights_offline_storage_enabled: bool = False
    application_insights_fastapi_instrumentation_enabled: bool = True
    application_insights_exclude_health_routes: bool = True

    @model_validator(mode="after")
    def validate_quality_threshold_order(self) -> "AppSettings":
        """Require stricter profiles to have nondecreasing thresholds."""
        self.quality_thresholds()
        self.planner_thresholds()
        self.foundry_provider_configuration()
        self.judge_provider_configuration()
        self.cosmos_run_history_configuration()
        if self.semantic_cache_enabled:
            self.redis_semantic_cache_configuration()
        self.application_insights_configuration()
        self.production_pricing_inputs()
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
            self.foundry_small_model,
            self.foundry_strong_deployment,
            self.foundry_strong_model,
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
            or self.foundry_small_model is None
            or self.foundry_strong_deployment is None
            or self.foundry_strong_model is None
            or self.foundry_auth_mode is None
        ):
            raise ValueError(
                "Foundry composition requires base URL, SMALL and STRONG deployment "
                "and model identities, and auth mode"
            )
        return FoundryProviderConfiguration(
            base_url=self.foundry_base_url,
            small_deployment=self.foundry_small_deployment,
            small_model=self.foundry_small_model,
            strong_deployment=self.foundry_strong_deployment,
            strong_model=self.foundry_strong_model,
            auth_mode=self.foundry_auth_mode,
            api_key=self.foundry_api_key,
            token_scope=self.foundry_token_scope,
            managed_identity_client_id=self.foundry_managed_identity_client_id,
            timeout_seconds=self.foundry_timeout_seconds,
        )

    def judge_provider_configuration(self) -> JudgeProviderConfiguration | None:
        """Build the explicit JUDGE role settings when they are configured."""
        if self.judge_deployment is None and self.judge_model is None:
            return None
        if self.judge_deployment is None or self.judge_model is None:
            raise ValueError("LLM judge composition requires both deployment and model")
        return JudgeProviderConfiguration(
            deployment=self.judge_deployment,
            model=self.judge_model,
            timeout_seconds=self.judge_timeout_seconds,
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

    def redis_semantic_cache_configuration(
        self,
    ) -> RedisSemanticCacheConfiguration | None:
        """Build cache settings only when Azure Managed Redis is requested."""
        optional_values = (
            self.redis_host,
            self.redis_index_name,
            self.redis_embedding_dimension,
            self.redis_embedding_model,
            self.redis_embedding_deployment,
            self.redis_auth_mode,
            self.redis_access_key,
            self.redis_object_id,
            self.redis_managed_identity_client_id,
        )
        if all(value is None for value in optional_values):
            return None
        if (
            self.redis_host is None
            or self.redis_index_name is None
            or self.redis_embedding_dimension is None
            or self.redis_embedding_model is None
            or self.redis_embedding_deployment is None
            or self.redis_auth_mode is None
        ):
            raise ValueError(
                "Redis semantic cache requires host, index, embedding dimension, "
                "embedding model, embedding deployment, and auth mode"
            )
        return RedisSemanticCacheConfiguration(
            host=self.redis_host,
            index_name=self.redis_index_name,
            embedding_dimension=self.redis_embedding_dimension,
            embedding_model=self.redis_embedding_model,
            embedding_deployment=self.redis_embedding_deployment,
            auth_mode=self.redis_auth_mode,
            access_key=self.redis_access_key,
            object_id=self.redis_object_id,
            managed_identity_client_id=self.redis_managed_identity_client_id,
            timeout_seconds=self.redis_timeout_seconds,
            max_connections=self.redis_max_connections,
            token_renewal_attempts=self.redis_token_renewal_attempts,
            token_retry_backoff_seconds=self.redis_token_retry_backoff_seconds,
            token_retry_backoff_cap_seconds=(
                self.redis_token_retry_backoff_cap_seconds
            ),
            token_acquisition_timeout_seconds=(
                self.redis_token_acquisition_timeout_seconds
            ),
            token_reauth_timeout_seconds=self.redis_token_reauth_timeout_seconds,
            token_expiry_safety_margin_seconds=(
                self.redis_token_expiry_safety_margin_seconds
            ),
        )

    def application_insights_configuration(
        self,
    ) -> ApplicationInsightsConfiguration | None:
        """Build Azure Monitor settings only when observability is enabled."""
        if not self.application_insights_enabled:
            return None
        if self.application_insights_connection_string is None:
            raise ValueError(
                "Application Insights requires a configured connection string"
            )
        return ApplicationInsightsConfiguration(
            connection_string=self.application_insights_connection_string,
            service_name=self.application_insights_service_name,
            service_version=self.application_insights_service_version,
            deployment_environment=(self.application_insights_deployment_environment),
            sampling_ratio=self.application_insights_sampling_ratio,
            live_metrics_enabled=self.application_insights_live_metrics_enabled,
            performance_counters_enabled=(
                self.application_insights_performance_counters_enabled
            ),
            offline_storage_enabled=(self.application_insights_offline_storage_enabled),
            fastapi_instrumentation_enabled=(
                self.application_insights_fastapi_instrumentation_enabled
            ),
            exclude_health_routes=(self.application_insights_exclude_health_routes),
        )

    def production_pricing_inputs(self) -> ProductionPricingInputs | None:
        """Build reviewed pricing inputs, or None when pricing is unconfigured.

        Partial pricing is rejected so incomplete configuration can never fabricate
        monetary evidence. Absent pricing keeps monetary cost unavailable unless the
        production runtime explicitly requires cost measurement.
        """
        version = self.pricing_catalog_version
        small_input = self.pricing_small_input_rate_per_million_tokens
        small_output = self.pricing_small_output_rate_per_million_tokens
        small_cached = self.pricing_small_cached_input_rate_per_million_tokens
        strong_input = self.pricing_strong_input_rate_per_million_tokens
        strong_output = self.pricing_strong_output_rate_per_million_tokens
        strong_cached = self.pricing_strong_cached_input_rate_per_million_tokens
        judge_input = self.pricing_judge_input_rate_per_million_tokens
        judge_output = self.pricing_judge_output_rate_per_million_tokens
        judge_cached = self.pricing_judge_cached_input_rate_per_million_tokens
        embedding_input = self.pricing_embedding_input_rate_per_million_tokens
        if all(
            value is None
            for value in (
                version,
                small_input,
                small_output,
                small_cached,
                strong_input,
                strong_output,
                strong_cached,
                judge_input,
                judge_output,
                judge_cached,
                embedding_input,
            )
        ):
            return None
        if (
            version is None
            or small_input is None
            or small_output is None
            or strong_input is None
            or strong_output is None
            or (self.semantic_cache_enabled and embedding_input is None)
        ):
            raise ValueError(
                "Production pricing requires a catalog version and SMALL, STRONG, "
                "and active-role token rates; embedding pricing is required when "
                "semantic cache is enabled; partial pricing cannot fabricate monetary "
                "evidence"
            )
        if (judge_input is None) is not (judge_output is None):
            raise ValueError(
                "Production JUDGE pricing requires both input and output rates; "
                "partial pricing cannot fabricate monetary evidence"
            )
        return ProductionPricingInputs(
            catalog_version=version,
            currency=self.pricing_currency,
            small=ProductionModelRoleRate(
                input_rate_per_million_tokens=small_input,
                output_rate_per_million_tokens=small_output,
                cached_input_rate_per_million_tokens=small_cached,
            ),
            strong=ProductionModelRoleRate(
                input_rate_per_million_tokens=strong_input,
                output_rate_per_million_tokens=strong_output,
                cached_input_rate_per_million_tokens=strong_cached,
            ),
            judge=(
                ProductionModelRoleRate(
                    input_rate_per_million_tokens=judge_input,
                    output_rate_per_million_tokens=judge_output,
                    cached_input_rate_per_million_tokens=judge_cached,
                )
                if judge_input is not None and judge_output is not None
                else None
            ),
            embedding_input_rate_per_million_tokens=embedding_input,
        )

    def validate_production_runtime(self) -> "AppSettings":
        """Require every dependency owned by the production API lifespan."""
        if "semantic_cache_enabled" not in self.model_fields_set:
            raise ValueError(
                "Production runtime requires an explicit semantic_cache_enabled "
                "decision"
            )
        if not self.semantic_cache_enabled:
            supplied_cache_fields = sorted(
                self.model_fields_set.intersection(_PRODUCTION_CACHE_ONLY_FIELDS)
            )
            if supplied_cache_fields:
                raise ValueError(
                    "Disabled production semantic cache cannot configure cache-only "
                    f"fields: {', '.join(supplied_cache_fields)}"
                )
        mode = self.production_evaluator_mode
        if mode is None:
            raise ValueError(
                "Production runtime requires an explicit supported evaluator mode; "
                "configure EXACT_REFERENCE or LLM_JUDGE"
            )
        if (
            mode is ProductionEvaluatorMode.EXACT_REFERENCE
            and not self.production_require_reference_output
        ):
            raise ValueError(
                "EXACT_REFERENCE production evaluation requires reference output"
            )
        if mode is ProductionEvaluatorMode.EXACT_REFERENCE:
            supplied_judge_fields = sorted(
                self.model_fields_set.intersection(_PRODUCTION_JUDGE_ONLY_FIELDS)
            )
            if supplied_judge_fields:
                raise ValueError(
                    "EXACT_REFERENCE production cannot configure inactive JUDGE "
                    f"fields: {', '.join(supplied_judge_fields)}"
                )
        judge = self.judge_provider_configuration()
        if mode is ProductionEvaluatorMode.LLM_JUDGE:
            if self.production_require_reference_output:
                raise ValueError(
                    "LLM_JUDGE production evaluation must not require reference output"
                )
            if judge is None:
                raise ValueError(
                    "LLM_JUDGE production evaluation requires judge deployment "
                    "and model"
                )
        foundry = self.foundry_provider_configuration()
        if foundry is None:
            raise ValueError("Production runtime requires Foundry configuration")
        cosmos = self.cosmos_run_history_configuration()
        if cosmos is None:
            raise ValueError("Production runtime requires Cosmos configuration")
        redis: RedisSemanticCacheConfiguration | None = None
        if self.semantic_cache_enabled:
            redis = self.redis_semantic_cache_configuration()
            if redis is None:
                raise ValueError("Production runtime requires Redis configuration")
        if self.application_insights_configuration() is None:
            raise ValueError(
                "Production runtime requires Application Insights configuration"
            )
        pricing = self.production_pricing_inputs()
        if mode is ProductionEvaluatorMode.LLM_JUDGE and (
            pricing is not None and pricing.judge is None
        ):
            raise ValueError(
                "LLM_JUDGE production pricing requires reviewed JUDGE token rates; "
                "partial pricing cannot fabricate monetary evidence"
            )
        if pricing is not None and pricing.judge is not None and judge is None:
            raise ValueError(
                "Production JUDGE pricing requires configured judge deployment"
            )
        if self.production_cost_measurement_required and pricing is None:
            raise ValueError(
                "Production cost measurement requires reviewed SMALL and STRONG "
                "pricing rates, plus JUDGE rates in LLM_JUDGE mode and embedding "
                "pricing when semantic cache is enabled; configure the price catalog "
                "or disable "
                "production_cost_measurement_required"
            )

        managed_identity_client_ids = [
            (
                foundry.auth_mode is FoundryAuthMode.MANAGED_IDENTITY,
                foundry.managed_identity_client_id,
                "Foundry",
            ),
            (
                cosmos.auth_mode is CosmosAuthMode.MANAGED_IDENTITY,
                cosmos.managed_identity_client_id,
                "Cosmos",
            ),
        ]
        if redis is not None:
            managed_identity_client_ids.append(
                (
                    redis.auth_mode is RedisAuthMode.MANAGED_IDENTITY,
                    redis.managed_identity_client_id,
                    "Redis",
                )
            )
        for (
            managed_identity_selected,
            client_id,
            service_name,
        ) in managed_identity_client_ids:
            if managed_identity_selected and client_id is None:
                raise ValueError(
                    f"Production {service_name} Managed Identity requires a client ID"
                )
        return self
