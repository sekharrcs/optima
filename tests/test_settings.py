"""Tests for typed application module settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from optima.config import AppSettings


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


def test_settings_ignore_unrelated_dotenv_values(tmp_path: Path) -> None:
    """Ignore unrelated keys while reading an explicit dotenv source."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "UNRELATED_VALUE=ignored\nOPTIMA_HISTORICAL_POLICY_ENABLED=false\n",
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings.historical_policy_enabled is False
