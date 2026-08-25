"""Tests for typed application module settings."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from optima.config import AppSettings, FoundryAuthMode, RedisAuthMode


@pytest.fixture(autouse=True)
def isolate_settings_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Isolate tests from developer dotenv files and module environment values."""
    monkeypatch.chdir(tmp_path)
    for variable in (
        "OPTIMA_SEMANTIC_CACHE_ENABLED",
        "OPTIMA_CONTEXT_REDUCTION_ENABLED",
        "OPTIMA_HISTORICAL_POLICY_ENABLED",
        "OPTIMA_FOUNDRY_ROUTER_COMPARATOR_ENABLED",
        "OPTIMA_STANDARD_QUALITY_THRESHOLD",
        "OPTIMA_HIGH_QUALITY_THRESHOLD",
        "OPTIMA_CRITICAL_QUALITY_THRESHOLD",
        "OPTIMA_CACHE_SIMILARITY_THRESHOLD",
        "OPTIMA_CONTEXT_REDUCTION_CONSIDER_TOKENS",
        "OPTIMA_CONTEXT_REDUCTION_REQUIRED_TOKENS",
        "OPTIMA_HISTORY_MINIMUM_SAMPLES",
        "OPTIMA_HISTORY_SMALL_PREFER_PASS_RATE",
        "OPTIMA_HISTORY_SMALL_AVOID_PASS_RATE",
        "OPTIMA_FOUNDRY_BASE_URL",
        "OPTIMA_FOUNDRY_SMALL_DEPLOYMENT",
        "OPTIMA_FOUNDRY_STRONG_DEPLOYMENT",
        "OPTIMA_FOUNDRY_AUTH_MODE",
        "OPTIMA_FOUNDRY_API_KEY",
        "OPTIMA_FOUNDRY_TOKEN_SCOPE",
        "OPTIMA_FOUNDRY_MANAGED_IDENTITY_CLIENT_ID",
        "OPTIMA_FOUNDRY_TIMEOUT_SECONDS",
        "OPTIMA_COSMOS_ENDPOINT",
        "OPTIMA_COSMOS_DATABASE_NAME",
        "OPTIMA_COSMOS_CONTAINER_NAME",
        "OPTIMA_COSMOS_AUTH_MODE",
        "OPTIMA_COSMOS_ACCOUNT_KEY",
        "OPTIMA_COSMOS_MANAGED_IDENTITY_CLIENT_ID",
        "OPTIMA_COSMOS_HISTORY_LIST_LIMIT",
        "OPTIMA_COSMOS_TIMEOUT_SECONDS",
        "OPTIMA_COSMOS_RETRY_TOTAL",
        "OPTIMA_REDIS_HOST",
        "OPTIMA_REDIS_INDEX_NAME",
        "OPTIMA_REDIS_EMBEDDING_DIMENSION",
        "OPTIMA_REDIS_EMBEDDING_MODEL",
        "OPTIMA_REDIS_EMBEDDING_DEPLOYMENT",
        "OPTIMA_REDIS_AUTH_MODE",
        "OPTIMA_REDIS_ACCESS_KEY",
        "OPTIMA_REDIS_OBJECT_ID",
        "OPTIMA_REDIS_MANAGED_IDENTITY_CLIENT_ID",
        "OPTIMA_REDIS_TIMEOUT_SECONDS",
        "OPTIMA_REDIS_MAX_CONNECTIONS",
        "OPTIMA_APPLICATION_INSIGHTS_ENABLED",
        "OPTIMA_APPLICATION_INSIGHTS_CONNECTION_STRING",
        "OPTIMA_APPLICATION_INSIGHTS_SERVICE_NAME",
        "OPTIMA_APPLICATION_INSIGHTS_SERVICE_VERSION",
        "OPTIMA_APPLICATION_INSIGHTS_DEPLOYMENT_ENVIRONMENT",
        "OPTIMA_APPLICATION_INSIGHTS_SAMPLING_RATIO",
        "OPTIMA_APPLICATION_INSIGHTS_LIVE_METRICS_ENABLED",
        "OPTIMA_APPLICATION_INSIGHTS_PERFORMANCE_COUNTERS_ENABLED",
        "OPTIMA_APPLICATION_INSIGHTS_OFFLINE_STORAGE_ENABLED",
        "OPTIMA_APPLICATION_INSIGHTS_FASTAPI_INSTRUMENTATION_ENABLED",
        "OPTIMA_APPLICATION_INSIGHTS_EXCLUDE_HEALTH_ROUTES",
    ):
        monkeypatch.delenv(variable, raising=False)


def test_settings_defaults_match_mvp_module_configuration() -> None:
    """Use the documented MVP defaults when no source overrides them."""
    settings = AppSettings()

    assert settings.semantic_cache_enabled is True
    assert settings.context_reduction_enabled is True
    assert settings.historical_policy_enabled is True
    assert settings.foundry_router_comparator_enabled is False
    assert settings.standard_quality_threshold == 0.80
    assert settings.high_quality_threshold == 0.90
    assert settings.critical_quality_threshold == 0.95
    assert settings.planner_thresholds().model_dump() == {
        "cache_similarity_threshold": 0.95,
        "context_reduction_consider_tokens": 4_000,
        "context_reduction_required_tokens": 8_000,
        "history_minimum_samples": 20,
        "history_small_prefer_pass_rate": 0.95,
        "history_small_avoid_pass_rate": 0.70,
    }


def test_module_configuration_maps_default_setting_flags() -> None:
    """Map all default application flags into immutable planner input."""
    assert AppSettings().module_configuration().model_dump() == {
        "semantic_cache_enabled": True,
        "context_reduction_enabled": True,
        "historical_policy_enabled": True,
        "foundry_router_comparator_enabled": False,
    }


def test_module_configuration_maps_explicit_setting_overrides() -> None:
    """Map mixed explicit application flags without changing their values."""
    settings = AppSettings(
        semantic_cache_enabled=False,
        context_reduction_enabled=True,
        historical_policy_enabled=False,
        foundry_router_comparator_enabled=True,
    )

    assert settings.module_configuration().model_dump() == {
        "semantic_cache_enabled": False,
        "context_reduction_enabled": True,
        "historical_policy_enabled": False,
        "foundry_router_comparator_enabled": True,
    }


def test_settings_accept_explicit_injection() -> None:
    """Allow tests and application composition to inject module settings."""
    settings = AppSettings(
        semantic_cache_enabled=False,
        context_reduction_enabled=False,
        historical_policy_enabled=False,
        foundry_router_comparator_enabled=True,
    )

    assert settings.model_dump() == {
        "semantic_cache_enabled": False,
        "context_reduction_enabled": False,
        "historical_policy_enabled": False,
        "foundry_router_comparator_enabled": True,
        "standard_quality_threshold": 0.80,
        "high_quality_threshold": 0.90,
        "critical_quality_threshold": 0.95,
        "cache_similarity_threshold": 0.95,
        "context_reduction_consider_tokens": 4_000,
        "context_reduction_required_tokens": 8_000,
        "history_minimum_samples": 20,
        "history_small_prefer_pass_rate": 0.95,
        "history_small_avoid_pass_rate": 0.70,
        "foundry_base_url": None,
        "foundry_small_deployment": None,
        "foundry_strong_deployment": None,
        "foundry_auth_mode": None,
        "foundry_api_key": None,
        "foundry_token_scope": None,
        "foundry_managed_identity_client_id": None,
        "foundry_timeout_seconds": 30.0,
        "cosmos_endpoint": None,
        "cosmos_database_name": None,
        "cosmos_container_name": None,
        "cosmos_auth_mode": None,
        "cosmos_account_key": None,
        "cosmos_managed_identity_client_id": None,
        "cosmos_history_list_limit": 50,
        "cosmos_timeout_seconds": 10.0,
        "cosmos_retry_total": 3,
        "redis_host": None,
        "redis_index_name": None,
        "redis_embedding_dimension": None,
        "redis_embedding_model": None,
        "redis_embedding_deployment": None,
        "redis_auth_mode": None,
        "redis_access_key": None,
        "redis_object_id": None,
        "redis_managed_identity_client_id": None,
        "redis_timeout_seconds": 1.0,
        "redis_max_connections": 10,
        "redis_token_renewal_attempts": 3,
        "redis_token_retry_backoff_seconds": 0.5,
        "redis_token_retry_backoff_cap_seconds": 5.0,
        "redis_token_acquisition_timeout_seconds": 10.0,
        "redis_token_reauth_timeout_seconds": 10.0,
        "redis_token_expiry_safety_margin_seconds": 180.0,
        "application_insights_enabled": False,
        "application_insights_connection_string": None,
        "application_insights_service_name": "optima-api",
        "application_insights_service_version": "0.1.0",
        "application_insights_deployment_environment": "local",
        "application_insights_sampling_ratio": 1.0,
        "application_insights_live_metrics_enabled": False,
        "application_insights_performance_counters_enabled": False,
        "application_insights_offline_storage_enabled": False,
        "application_insights_fastapi_instrumentation_enabled": True,
        "application_insights_exclude_health_routes": True,
    }


def test_settings_read_prefixed_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read module overrides from the centralized environment namespace."""
    monkeypatch.setenv("OPTIMA_SEMANTIC_CACHE_ENABLED", "false")
    monkeypatch.setenv("OPTIMA_CONTEXT_REDUCTION_ENABLED", "0")
    monkeypatch.setenv("OPTIMA_HISTORICAL_POLICY_ENABLED", "no")
    monkeypatch.setenv("OPTIMA_FOUNDRY_ROUTER_COMPARATOR_ENABLED", "true")

    settings = AppSettings()

    assert settings.semantic_cache_enabled is False
    assert settings.context_reduction_enabled is False
    assert settings.historical_policy_enabled is False
    assert settings.foundry_router_comparator_enabled is True


def test_explicit_settings_take_precedence_over_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefer explicit injection over process environment values."""
    monkeypatch.setenv("OPTIMA_SEMANTIC_CACHE_ENABLED", "true")

    settings = AppSettings(semantic_cache_enabled=False)

    assert settings.semantic_cache_enabled is False


def test_settings_read_quality_threshold_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read configurable Quality Profile thresholds from the environment."""
    monkeypatch.setenv("OPTIMA_STANDARD_QUALITY_THRESHOLD", "0.70")
    monkeypatch.setenv("OPTIMA_HIGH_QUALITY_THRESHOLD", "0.85")
    monkeypatch.setenv("OPTIMA_CRITICAL_QUALITY_THRESHOLD", "0.99")

    settings = AppSettings()

    assert settings.quality_thresholds().model_dump() == {
        "standard": 0.70,
        "high": 0.85,
        "critical": 0.99,
    }


def test_settings_reject_nonmonotonic_quality_thresholds() -> None:
    """Reject settings that weaken a stricter Quality Profile."""
    with pytest.raises(ValidationError, match="STANDARD <= HIGH <= CRITICAL"):
        AppSettings(
            standard_quality_threshold=0.90,
            high_quality_threshold=0.80,
        )


def test_settings_reject_malformed_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject invalid module values rather than silently choosing a state."""
    monkeypatch.setenv("OPTIMA_CONTEXT_REDUCTION_ENABLED", "sometimes")

    with pytest.raises(ValidationError):
        AppSettings()


def test_settings_read_planner_threshold_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read all Planner V1 thresholds from the centralized environment."""
    monkeypatch.setenv("OPTIMA_CACHE_SIMILARITY_THRESHOLD", "0.91")
    monkeypatch.setenv("OPTIMA_CONTEXT_REDUCTION_CONSIDER_TOKENS", "3000")
    monkeypatch.setenv("OPTIMA_CONTEXT_REDUCTION_REQUIRED_TOKENS", "7000")
    monkeypatch.setenv("OPTIMA_HISTORY_MINIMUM_SAMPLES", "12")
    monkeypatch.setenv("OPTIMA_HISTORY_SMALL_PREFER_PASS_RATE", "0.92")
    monkeypatch.setenv("OPTIMA_HISTORY_SMALL_AVOID_PASS_RATE", "0.60")

    settings = AppSettings()

    assert settings.planner_thresholds().model_dump() == {
        "cache_similarity_threshold": 0.91,
        "context_reduction_consider_tokens": 3_000,
        "context_reduction_required_tokens": 7_000,
        "history_minimum_samples": 12,
        "history_small_prefer_pass_rate": 0.92,
        "history_small_avoid_pass_rate": 0.60,
    }


@pytest.mark.parametrize(
    "updates",
    [
        {
            "context_reduction_consider_tokens": 8_001,
            "context_reduction_required_tokens": 8_000,
        },
        {
            "history_small_avoid_pass_rate": 0.95,
            "history_small_prefer_pass_rate": 0.95,
        },
    ],
)
def test_settings_reject_incoherent_planner_thresholds(
    updates: dict[str, object],
) -> None:
    """Reject threshold combinations that cannot define deterministic policy."""
    with pytest.raises(ValidationError):
        AppSettings.model_validate(updates)


def test_settings_ignore_unrelated_dotenv_values(tmp_path: Path) -> None:
    """Ignore unrelated keys while reading an explicit dotenv source."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "UNRELATED_VALUE=ignored\nOPTIMA_HISTORICAL_POLICY_ENABLED=false\n",
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings.historical_policy_enabled is False


def test_default_settings_do_not_request_foundry_composition() -> None:
    """Keep cloud composition optional for the default API and local demo."""
    assert AppSettings().foundry_provider_configuration() is None


def test_default_settings_do_not_request_redis_composition() -> None:
    """Keep local API and tests free from Redis credentials and connections."""
    assert AppSettings().redis_semantic_cache_configuration() is None


def test_default_settings_do_not_request_application_insights() -> None:
    """Keep default application construction free from telemetry exporters."""
    assert AppSettings().application_insights_configuration() is None


def test_enabled_application_insights_requires_connection_string() -> None:
    """Fail settings validation before an exporter can be constructed."""
    with pytest.raises(ValidationError, match="requires a configured connection"):
        AppSettings(application_insights_enabled=True)


def test_settings_build_explicit_application_insights_configuration() -> None:
    """Preserve explicit privacy, sampling, and resource configuration."""
    settings = AppSettings(
        application_insights_enabled=True,
        application_insights_connection_string=SecretStr(
            "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
            "IngestionEndpoint=https://example.applicationinsights.azure.com/;"
            "LiveEndpoint=https://example.livediagnostics.monitor.azure.com/;"
            "Authorization=ikey;"
            "ApplicationId=00000000-0000-0000-0000-000000000002"
        ),
        application_insights_service_name="optima-test",
        application_insights_service_version="1.2.3",
        application_insights_deployment_environment="test",
        application_insights_sampling_ratio=0.25,
        application_insights_offline_storage_enabled=True,
        application_insights_fastapi_instrumentation_enabled=False,
        application_insights_exclude_health_routes=False,
    )

    configuration = settings.application_insights_configuration()

    assert configuration is not None
    assert configuration.connection_string.get_secret_value().startswith(
        "InstrumentationKey="
    )
    assert "00000000-0000-0000-0000-000000000001" not in repr(configuration)
    assert configuration.service_name == "optima-test"
    assert configuration.service_version == "1.2.3"
    assert configuration.deployment_environment == "test"
    assert configuration.sampling_ratio == 0.25
    assert configuration.live_metrics_enabled is False
    assert configuration.performance_counters_enabled is False
    assert configuration.offline_storage_enabled is True
    assert configuration.fastapi_instrumentation_enabled is False
    assert configuration.exclude_health_routes is False


@pytest.mark.parametrize(
    "unsupported_setting",
    (
        "application_insights_live_metrics_enabled",
        "application_insights_performance_counters_enabled",
    ),
)
def test_isolated_application_insights_rejects_global_sdk_features(
    unsupported_setting: str,
) -> None:
    """Reject SDK-global telemetry features before exporter construction."""
    with pytest.raises(ValidationError, match="isolated Application Insights"):
        AppSettings.model_validate(
            {
                "application_insights_enabled": True,
                "application_insights_connection_string": SecretStr(
                    "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                    "IngestionEndpoint="
                    "https://example.applicationinsights.azure.com/"
                ),
                unsupported_setting: True,
            }
        ).application_insights_configuration()


@pytest.mark.parametrize(
    "updates",
    [
        {"application_insights_connection_string": "not-a-connection-string"},
        {"application_insights_connection_string": ("InstrumentationKey=not-a-uuid")},
        {
            "application_insights_connection_string": (
                "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                "IngestionEndpoint=http://evil.applicationinsights.azure.com/"
            )
        },
        {
            "application_insights_connection_string": (
                "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                "IngestionEndpoint=https://evil.example/"
            )
        },
        {
            "application_insights_connection_string": (
                "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                "IngestionEndpoint=https://.applicationinsights.azure.com/"
            )
        },
        {
            "application_insights_connection_string": (
                "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                "IngestionEndpoint=https://foo..applicationinsights.azure.com/"
            )
        },
        {
            "application_insights_connection_string": (
                "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                "IngestionEndpoint=https://foo-.applicationinsights.azure.com/"
            )
        },
        {
            "application_insights_connection_string": (
                "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                "EndpointSuffix=applicationinsights.azure.com"
            )
        },
        {
            "application_insights_connection_string": (
                "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                "IngestionEndpoint=https://example.applicationinsights.azure.com/"
            ),
            "application_insights_sampling_ratio": -0.01,
        },
        {
            "application_insights_connection_string": (
                "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                "IngestionEndpoint=https://example.applicationinsights.azure.com/"
            ),
            "application_insights_sampling_ratio": 1.01,
        },
        {
            "application_insights_connection_string": (
                "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                "IngestionEndpoint=https://example.applicationinsights.azure.com/"
            ),
            "application_insights_service_name": "unsafe service name",
        },
    ],
)
def test_settings_reject_invalid_application_insights_configuration(
    updates: dict[str, object],
) -> None:
    """Reject malformed telemetry configuration before SDK initialization."""
    with pytest.raises(ValidationError):
        AppSettings.model_validate({"application_insights_enabled": True, **updates})


@pytest.mark.parametrize(
    ("auth_mode", "auth_values"),
    [
        (RedisAuthMode.ACCESS_KEY, {"redis_access_key": "fake-key"}),
        (RedisAuthMode.AZURE_CLI, {"redis_object_id": "cli-object-id"}),
        (
            RedisAuthMode.MANAGED_IDENTITY,
            {
                "redis_object_id": "identity-object-id",
                "redis_managed_identity_client_id": "identity-client-id",
            },
        ),
    ],
)
def test_settings_build_explicit_redis_authentication_modes(
    auth_mode: RedisAuthMode,
    auth_values: dict[str, str],
) -> None:
    """Build each Redis mode without selecting an implicit credential chain."""
    values: dict[str, object] = {
        "redis_host": "optima.eastus.redis.azure.net",
        "redis_index_name": "optima-cache-v1",
        "redis_embedding_dimension": 1536,
        "redis_embedding_model": "text-embed-3",
        "redis_embedding_deployment": "optima-embed",
        "redis_auth_mode": auth_mode,
    }
    values.update(auth_values)

    configuration = AppSettings.model_validate(
        values
    ).redis_semantic_cache_configuration()

    assert configuration is not None
    assert configuration.auth_mode is auth_mode
    assert configuration.embedding_dimension == 1536
    assert configuration.timeout_seconds == 1.0


@pytest.mark.parametrize(
    "updates",
    [
        {"redis_host": "optima.eastus.redis.azure.net"},
        {
            "redis_host": "optima.eastus.redis.azure.net",
            "redis_index_name": "cache",
            "redis_embedding_dimension": 1536,
            "redis_embedding_model": "text-embed-3",
            "redis_auth_mode": "AZURE_CLI",
            "redis_object_id": "object-id",
        },
        {
            "redis_host": "https://optima.eastus.redis.azure.net",
            "redis_index_name": "cache",
            "redis_embedding_dimension": 1536,
            "redis_embedding_model": "text-embed-3",
            "redis_embedding_deployment": "optima-embed",
            "redis_auth_mode": "AZURE_CLI",
            "redis_object_id": "object-id",
        },
        {
            "redis_host": "optima.eastus.redis.azure.net",
            "redis_index_name": "unsafe index",
            "redis_embedding_dimension": 1536,
            "redis_embedding_model": "text-embed-3",
            "redis_embedding_deployment": "optima-embed",
            "redis_auth_mode": "AZURE_CLI",
            "redis_object_id": "object-id",
        },
        {
            "redis_host": "optima.eastus.redis.azure.net",
            "redis_index_name": "cache",
            "redis_embedding_dimension": 1536,
            "redis_embedding_model": "invalid model!",
            "redis_embedding_deployment": "optima-embed",
            "redis_auth_mode": "AZURE_CLI",
            "redis_object_id": "object-id",
        },
        {
            "redis_host": "optima.eastus.redis.azure.net",
            "redis_index_name": "cache",
            "redis_embedding_dimension": 1536,
            "redis_embedding_model": "text-embed-3",
            "redis_embedding_deployment": "optima-embed",
            "redis_auth_mode": "ACCESS_KEY",
        },
        {
            "redis_host": "optima.eastus.redis.azure.net",
            "redis_index_name": "cache",
            "redis_embedding_dimension": 1536,
            "redis_embedding_model": "text-embed-3",
            "redis_embedding_deployment": "optima-embed",
            "redis_auth_mode": "AZURE_CLI",
        },
        {
            "redis_host": "optima.eastus.redis.azure.net",
            "redis_index_name": "cache",
            "redis_embedding_dimension": 1536,
            "redis_embedding_model": "text-embed-3",
            "redis_embedding_deployment": "optima-embed",
            "redis_auth_mode": "AZURE_CLI",
            "redis_object_id": "object-id",
            "redis_access_key": "mixed-secret",
        },
        {
            "redis_host": "optima.eastus.redis.azure.net",
            "redis_index_name": "cache",
            "redis_embedding_dimension": 1536,
            "redis_embedding_model": "text-embed-3",
            "redis_embedding_deployment": "optima-embed",
            "redis_auth_mode": "AZURE_CLI",
            "redis_object_id": "object-id",
            "redis_managed_identity_client_id": "forbidden-client-id",
        },
    ],
)
def test_settings_reject_incomplete_unsafe_or_mixed_redis_configuration(
    updates: dict[str, object],
) -> None:
    """Fail closed for ambiguous Redis endpoints, indexes, and credentials."""
    with pytest.raises(ValidationError):
        AppSettings.model_validate(updates)


@pytest.mark.parametrize(
    ("auth_mode", "auth_values"),
    [
        (FoundryAuthMode.API_KEY, {"foundry_api_key": "fake-key"}),
        (
            FoundryAuthMode.AZURE_CLI,
            {"foundry_token_scope": "api://optima-apim/.default"},
        ),
        (
            FoundryAuthMode.MANAGED_IDENTITY,
            {
                "foundry_token_scope": "api://optima-apim/.default",
                "foundry_managed_identity_client_id": "managed-client-id",
            },
        ),
    ],
)
def test_settings_build_explicit_foundry_authentication_modes(
    auth_mode: FoundryAuthMode,
    auth_values: dict[str, str],
) -> None:
    """Build each supported mode without adding an implicit credential fallback."""
    values: dict[str, object] = {
        "foundry_base_url": "https://gateway.example/openai/v1",
        "foundry_small_deployment": "small-deployment",
        "foundry_strong_deployment": "strong-deployment",
        "foundry_auth_mode": auth_mode,
    }
    values.update(auth_values)
    settings = AppSettings.model_validate(values)

    configuration = settings.foundry_provider_configuration()

    assert configuration is not None
    assert configuration.auth_mode is auth_mode
    assert configuration.small_deployment == "small-deployment"
    assert configuration.strong_deployment == "strong-deployment"
    assert isinstance(configuration.api_key, SecretStr) or configuration.api_key is None


def test_settings_read_foundry_api_key_without_exposing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load API-key configuration through the OPTIMA namespace as a secret value."""
    monkeypatch.setenv(
        "OPTIMA_FOUNDRY_BASE_URL",
        "https://gateway.example/openai/v1",
    )
    monkeypatch.setenv("OPTIMA_FOUNDRY_SMALL_DEPLOYMENT", "small-deployment")
    monkeypatch.setenv("OPTIMA_FOUNDRY_STRONG_DEPLOYMENT", "strong-deployment")
    monkeypatch.setenv("OPTIMA_FOUNDRY_AUTH_MODE", "API_KEY")
    monkeypatch.setenv("OPTIMA_FOUNDRY_API_KEY", "configured-secret")

    settings = AppSettings()
    configuration = settings.foundry_provider_configuration()

    assert configuration is not None
    assert configuration.api_key is not None
    assert configuration.api_key.get_secret_value() == "configured-secret"
    assert "configured-secret" not in repr(settings)


def test_settings_read_cosmos_numeric_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coerce documented numeric environment strings within explicit bounds."""
    monkeypatch.setenv("OPTIMA_COSMOS_HISTORY_LIST_LIMIT", "25")
    monkeypatch.setenv("OPTIMA_COSMOS_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("OPTIMA_COSMOS_RETRY_TOTAL", "4")

    settings = AppSettings()

    assert settings.cosmos_history_list_limit == 25
    assert settings.cosmos_timeout_seconds == 12.5
    assert settings.cosmos_retry_total == 4


@pytest.mark.parametrize(
    "updates",
    [
        {"foundry_base_url": "https://gateway.example/openai/v1"},
        {
            "foundry_base_url": "not-a-url",
            "foundry_small_deployment": "small",
            "foundry_strong_deployment": "strong",
            "foundry_auth_mode": "API_KEY",
            "foundry_api_key": "key",
        },
        {
            "foundry_base_url": "http://gateway.example/openai/v1",
            "foundry_small_deployment": "small",
            "foundry_strong_deployment": "strong",
            "foundry_auth_mode": "API_KEY",
            "foundry_api_key": "key",
        },
        {
            "foundry_base_url": "https://user:password@gateway.example/openai/v1",
            "foundry_small_deployment": "small",
            "foundry_strong_deployment": "strong",
            "foundry_auth_mode": "API_KEY",
            "foundry_api_key": "key",
        },
        {
            "foundry_base_url": "https://gateway.example/not-the-v1-root",
            "foundry_small_deployment": "small",
            "foundry_strong_deployment": "strong",
            "foundry_auth_mode": "API_KEY",
            "foundry_api_key": "key",
        },
        {
            "foundry_base_url": "https://gateway.example/OPENAI/V1",
            "foundry_small_deployment": "small",
            "foundry_strong_deployment": "strong",
            "foundry_auth_mode": "API_KEY",
            "foundry_api_key": "key",
        },
        {
            "foundry_base_url": "https://gateway.example/openai/v1",
            "foundry_small_deployment": "small",
            "foundry_strong_deployment": "strong",
            "foundry_auth_mode": "API_KEY",
        },
        {
            "foundry_base_url": "https://gateway.example/openai/v1",
            "foundry_small_deployment": "small",
            "foundry_strong_deployment": "strong",
            "foundry_auth_mode": "AZURE_CLI",
        },
        {
            "foundry_base_url": "https://gateway.example/openai/v1",
            "foundry_small_deployment": "small",
            "foundry_strong_deployment": "strong",
            "foundry_auth_mode": "AZURE_CLI",
            "foundry_token_scope": "scope",
            "foundry_api_key": "ambiguous",
        },
        {
            "foundry_base_url": "https://gateway.example/openai/v1",
            "foundry_small_deployment": "small",
            "foundry_strong_deployment": "strong",
            "foundry_auth_mode": "API_KEY",
            "foundry_api_key": "key",
            "foundry_token_scope": "ambiguous",
        },
    ],
)
def test_settings_reject_incomplete_or_ambiguous_foundry_configuration(
    updates: dict[str, object],
) -> None:
    """Fail closed instead of selecting or combining credentials implicitly."""
    with pytest.raises(ValidationError):
        AppSettings.model_validate(updates)
