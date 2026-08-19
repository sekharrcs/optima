"""Tests for Quality Contract validation and translation."""

import math

import pytest
from pydantic import ValidationError

from optima.domain.quality_contract import (
    OptimizationMode,
    QualityContract,
    QualityProfile,
    QualityThresholds,
    RiskTier,
    build_quality_contract,
)


@pytest.mark.parametrize(
    ("profile", "expected_threshold"),
    [
        (QualityProfile.STANDARD, 0.80),
        (QualityProfile.HIGH, 0.90),
        (QualityProfile.CRITICAL, 0.95),
    ],
)
def test_builder_maps_each_profile_to_its_default_threshold(
    profile: QualityProfile,
    expected_threshold: float,
) -> None:
    """Translate each profile to its documented default threshold."""
    contract = build_quality_contract(
        quality_profile=profile,
        optimization_mode=OptimizationMode.BALANCED,
        risk_tier=RiskTier.MEDIUM,
    )

    assert contract.minimum_quality_score == expected_threshold


@pytest.mark.parametrize("profile", list(QualityProfile))
@pytest.mark.parametrize("mode", list(OptimizationMode))
def test_optimization_mode_does_not_modify_profile_threshold(
    profile: QualityProfile,
    mode: OptimizationMode,
) -> None:
    """Keep profile thresholds independent from optimization preferences."""
    expected = QualityThresholds().for_profile(profile)

    contract = build_quality_contract(
        quality_profile=profile,
        optimization_mode=mode,
        risk_tier=RiskTier.LOW,
    )

    assert contract.minimum_quality_score == expected
    assert contract.optimization_mode is mode


def test_builder_uses_injected_thresholds_and_preserves_contract_values() -> None:
    """Preserve caller values while using configurable profile thresholds."""
    contract = build_quality_contract(
        quality_profile=QualityProfile.HIGH,
        optimization_mode=OptimizationMode.COST,
        risk_tier=RiskTier.HIGH,
        max_latency_ms=2500,
        thresholds=QualityThresholds(standard=0.70, high=0.85, critical=0.99),
    )

    assert contract == QualityContract(
        quality_profile=QualityProfile.HIGH,
        minimum_quality_score=0.85,
        optimization_mode=OptimizationMode.COST,
        risk_tier=RiskTier.HIGH,
        max_latency_ms=2500,
    )


@pytest.mark.parametrize("score", [0, 0.42, 1])
def test_quality_contract_accepts_valid_score_boundaries(score: float) -> None:
    """Accept finite numeric scores throughout the closed unit interval."""
    contract = QualityContract(
        quality_profile=QualityProfile.STANDARD,
        minimum_quality_score=score,
        optimization_mode=OptimizationMode.QUALITY,
        risk_tier=RiskTier.LOW,
    )

    assert contract.minimum_quality_score == float(score)


@pytest.mark.parametrize(
    "score",
    [-0.01, 1.01, True, "0.8", math.nan, math.inf, -math.inf],
)
def test_quality_contract_rejects_invalid_scores(score: object) -> None:
    """Reject out-of-range, non-finite, boolean, and string scores."""
    with pytest.raises(ValidationError):
        QualityContract.model_validate(
            {
                "quality_profile": QualityProfile.STANDARD,
                "minimum_quality_score": score,
                "optimization_mode": OptimizationMode.COST,
                "risk_tier": RiskTier.LOW,
            }
        )


@pytest.mark.parametrize("latency", [None, 1, 2500])
def test_quality_contract_accepts_valid_optional_latency(
    latency: int | None,
) -> None:
    """Accept an omitted or positive millisecond latency ceiling."""
    contract = build_quality_contract(
        quality_profile=QualityProfile.STANDARD,
        optimization_mode=OptimizationMode.COST,
        risk_tier=RiskTier.LOW,
        max_latency_ms=latency,
    )

    assert contract.max_latency_ms == latency


@pytest.mark.parametrize("latency", [0, -1, True, 1.5, "100"])
def test_quality_contract_rejects_invalid_optional_latency(latency: object) -> None:
    """Reject nonpositive and non-integer latency ceilings."""
    with pytest.raises(ValidationError):
        QualityContract.model_validate(
            {
                "quality_profile": QualityProfile.STANDARD,
                "minimum_quality_score": 0.8,
                "optimization_mode": OptimizationMode.COST,
                "risk_tier": RiskTier.LOW,
                "max_latency_ms": latency,
            }
        )


def test_quality_thresholds_reject_nonmonotonic_profiles() -> None:
    """Reject threshold configuration that weakens stricter profiles."""
    with pytest.raises(ValidationError, match="STANDARD <= HIGH <= CRITICAL"):
        QualityThresholds(standard=0.9, high=0.8, critical=0.95)


def test_quality_contract_rejects_unknown_fields() -> None:
    """Reject accidental expansion of the contract schema."""
    with pytest.raises(ValidationError):
        QualityContract.model_validate(
            {
                "quality_profile": QualityProfile.STANDARD,
                "minimum_quality_score": 0.8,
                "optimization_mode": OptimizationMode.COST,
                "risk_tier": RiskTier.LOW,
                "unplanned_field": True,
            }
        )
