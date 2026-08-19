"""Typed application settings for OPTIMA capabilities and thresholds."""

from typing import Annotated

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from optima.domain.quality_contract import QualityThresholds
from optima.planner.models import ModuleConfiguration, PlannerThresholds

ConfiguredQualityScore = Annotated[
    float,
    Field(ge=0.0, le=1.0, allow_inf_nan=False),
]
PositiveInteger = Annotated[int, Field(gt=0)]


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

    @model_validator(mode="after")
    def validate_quality_threshold_order(self) -> "AppSettings":
        """Require stricter profiles to have nondecreasing thresholds."""
        self.quality_thresholds()
        self.planner_thresholds()
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
