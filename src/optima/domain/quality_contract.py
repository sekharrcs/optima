"""Quality Contract domain values and profile translation."""

from enum import StrEnum
from typing import Annotated

from pydantic import Field, model_validator

from optima.immutable import ImmutableModel

QualityScore = Annotated[
    float,
    Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False),
]
PositiveMilliseconds = Annotated[int, Field(strict=True, gt=0)]


class QualityProfile(StrEnum):
    """User-facing minimum quality profiles."""

    STANDARD = "STANDARD"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class OptimizationMode(StrEnum):
    """Preferences for choosing among contract-compatible plans."""

    COST = "COST"
    BALANCED = "BALANCED"
    QUALITY = "QUALITY"


class RiskTier(StrEnum):
    """Request or contract risk classification."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class QualityThresholds(ImmutableModel):
    """Configurable minimum score for each Quality Profile."""

    standard: QualityScore = 0.80
    high: QualityScore = 0.90
    critical: QualityScore = 0.95

    @model_validator(mode="after")
    def validate_profile_order(self) -> "QualityThresholds":
        """Require stricter profiles to have nondecreasing thresholds."""
        if not self.standard <= self.high <= self.critical:
            raise ValueError(
                "quality thresholds must satisfy STANDARD <= HIGH <= CRITICAL"
            )
        return self

    def for_profile(self, profile: QualityProfile) -> float:
        """Return the configured threshold for a Quality Profile."""
        return {
            QualityProfile.STANDARD: self.standard,
            QualityProfile.HIGH: self.high,
            QualityProfile.CRITICAL: self.critical,
        }[profile]


class QualityContract(ImmutableModel):
    """Explicit quality constraints and optimization preference for a request."""

    quality_profile: QualityProfile
    minimum_quality_score: QualityScore
    optimization_mode: OptimizationMode
    risk_tier: RiskTier
    grounding_required: Annotated[bool, Field(strict=True)] = False
    max_latency_ms: PositiveMilliseconds | None = None


def build_quality_contract(
    *,
    quality_profile: QualityProfile,
    optimization_mode: OptimizationMode,
    risk_tier: RiskTier,
    grounding_required: bool = False,
    max_latency_ms: int | None = None,
    thresholds: QualityThresholds | None = None,
) -> QualityContract:
    """Translate user-facing controls into an explicit Quality Contract."""
    configured_thresholds = thresholds or QualityThresholds()
    return QualityContract(
        quality_profile=quality_profile,
        minimum_quality_score=configured_thresholds.for_profile(quality_profile),
        optimization_mode=optimization_mode,
        risk_tier=risk_tier,
        grounding_required=grounding_required,
        max_latency_ms=max_latency_ms,
    )
