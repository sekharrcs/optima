"""Tests for typed application module settings."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from optima.config import AppSettings, FoundryAuthMode


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
