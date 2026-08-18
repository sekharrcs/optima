"""Typed application settings for OPTIMA capabilities."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Configure independently controlled MVP optimization modules."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="OPTIMA_",
        extra="ignore",
    )

    semantic_cache_enabled: bool = True
    context_reduction_enabled: bool = True
    historical_policy_enabled: bool = True
    foundry_router_comparator_enabled: bool = False
