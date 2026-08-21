"""Validation tests for immutable Planner V1 inputs and evidence."""

import pytest
from pydantic import ValidationError

from optima.domain.execution import (
    HistoricalDecisionEvidence,
    HistoricalEvidenceDisposition,
    ModelPolicy,
    PlannerDecisionEvidence,
    PlannerModuleStates,
)
from optima.domain.quality_contract import RiskTier
from optima.planner.models import PlannerThresholds


def module_states(**updates: object) -> PlannerModuleStates:
    """Build module evidence with optional test overrides."""
    values: dict[str, object] = {
        "semantic_cache_enabled": True,
        "context_reduction_enabled": True,
        "historical_policy_enabled": True,
        "foundry_router_comparator_enabled": False,
    }
    values.update(updates)
    return PlannerModuleStates.model_validate(values)


def decision_evidence(**updates: object) -> PlannerDecisionEvidence:
    """Build valid model-executed decision evidence for validation tests."""
    values: dict[str, object] = {
        "profile_risk_tier": RiskTier.LOW,
        "contract_risk_tier": RiskTier.MEDIUM,
        "effective_risk_tier": RiskTier.MEDIUM,
        "module_states": module_states(),
        "cache_candidate_assessed": False,
        "base_model_policy": ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
        "final_model_policy": ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
    }
    values.update(updates)
    return PlannerDecisionEvidence.model_validate(values)


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
        {"history_minimum_samples": 0},
    ],
)
def test_planner_thresholds_reject_incoherent_values(
    updates: dict[str, object],
) -> None:
    """Reject invalid ordering and nonpositive planner thresholds."""
    with pytest.raises(ValidationError):
        PlannerThresholds.model_validate(updates)


def test_decision_evidence_rejects_lower_effective_risk() -> None:
    """Prevent either risk source from being weakened in derived evidence."""
    with pytest.raises(ValidationError, match="most severe"):
        decision_evidence(effective_risk_tier=RiskTier.LOW)


def test_disabled_cache_rejects_candidate_assessment() -> None:
    """Do not claim cache assessment when the module gate prevented it."""
    with pytest.raises(ValidationError, match="disabled semantic cache"):
        decision_evidence(
            module_states=module_states(semantic_cache_enabled=False),
            cache_candidate_assessed=True,
        )


def test_disabled_history_rejects_statistics() -> None:
    """Do not attach historical facts when policy was disabled."""
    history = HistoricalDecisionEvidence(
        comparable_sample_count=20,
        small_pass_without_escalation_rate=0.96,
        average_final_quality=0.91,
        disposition=HistoricalEvidenceDisposition.POSITIVE_CONFIDENCE,
    )

    with pytest.raises(ValidationError, match="disabled historical policy"):
        decision_evidence(
            module_states=module_states(historical_policy_enabled=False),
            historical_statistics=history,
        )


def test_core_evidence_is_frozen_and_forbids_extra_fields() -> None:
    """Keep planner evidence immutable and reject arbitrary core facts."""
    evidence = decision_evidence()

    with pytest.raises(ValidationError):
        evidence.effective_risk_tier = RiskTier.HIGH
    with pytest.raises(ValidationError):
        PlannerDecisionEvidence.model_validate(
            {**evidence.model_dump(), "provider": "not-allowed"}
        )


def test_nested_planner_evidence_model_copy_revalidates_updates() -> None:
    """Reject invalid nested module evidence created through model_copy."""
    with pytest.raises(ValidationError):
        module_states().model_copy(update={"context_reduction_enabled": "not-a-bool"})
