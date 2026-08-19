"""Tests for the Planner V1 Request Profile contract."""

import pytest
from pydantic import ValidationError

from optima.domain.quality_contract import RiskTier
from optima.domain.request_profile import Complexity, RequestProfile, TaskType


def test_task_types_match_planner_v1_exactly() -> None:
    """Expose only the task types authorized by Planner V1."""
    assert {task_type.value for task_type in TaskType} == {
        "SUMMARIZATION",
        "EXTRACTION",
        "CLASSIFICATION",
        "Q_AND_A",
        "CODE_GENERATION",
        "LOG_ANALYSIS",
        "GENERAL_REASONING",
        "UNKNOWN",
    }


def test_complexity_values_match_planner_v1_exactly() -> None:
    """Expose only the complexity values authorized by Planner V1."""
    assert {complexity.value for complexity in Complexity} == {
        "LOW",
        "MEDIUM",
        "HIGH",
    }


@pytest.mark.parametrize("task_type", list(TaskType))
@pytest.mark.parametrize("complexity", list(Complexity))
@pytest.mark.parametrize("risk_tier", list(RiskTier))
def test_request_profile_preserves_all_typed_profile_facts(
    task_type: TaskType,
    complexity: Complexity,
    risk_tier: RiskTier,
) -> None:
    """Preserve each supported profile classification without selecting a plan."""
    profile = RequestProfile(
        task_type=task_type,
        complexity=complexity,
        input_tokens=8000,
        risk_tier=risk_tier,
        cache_eligible=True,
        has_large_context=True,
    )

    assert profile.task_type is task_type
    assert profile.complexity is complexity
    assert profile.input_tokens == 8000
    assert profile.risk_tier is risk_tier
    assert profile.cache_eligible is True
    assert profile.has_large_context is True


@pytest.mark.parametrize("input_tokens", [0, 1, 100_000])
def test_request_profile_accepts_nonnegative_token_counts(input_tokens: int) -> None:
    """Accept measured or estimated input-token counts including zero."""
    profile = RequestProfile(
        task_type=TaskType.UNKNOWN,
        complexity=Complexity.LOW,
        input_tokens=input_tokens,
        risk_tier=RiskTier.LOW,
        cache_eligible=False,
        has_large_context=False,
    )

    assert profile.input_tokens == input_tokens


@pytest.mark.parametrize("input_tokens", [-1, True, 1.5, "100"])
def test_request_profile_rejects_invalid_token_counts(input_tokens: object) -> None:
    """Reject negative and non-integer token counts."""
    with pytest.raises(ValidationError):
        RequestProfile.model_validate(
            {
                "task_type": TaskType.UNKNOWN,
                "complexity": Complexity.LOW,
                "input_tokens": input_tokens,
                "risk_tier": RiskTier.LOW,
                "cache_eligible": False,
                "has_large_context": False,
            }
        )


def test_request_profile_rejects_unknown_fields() -> None:
    """Reject speculative additions to the profile contract."""
    with pytest.raises(ValidationError):
        RequestProfile.model_validate(
            {
                "task_type": TaskType.UNKNOWN,
                "complexity": Complexity.LOW,
                "input_tokens": 0,
                "risk_tier": RiskTier.LOW,
                "cache_eligible": False,
                "has_large_context": False,
                "selected_model": "SMALL",
            }
        )


@pytest.mark.parametrize("field", ["cache_eligible", "has_large_context"])
def test_request_profile_rejects_coerced_boolean_flags(field: str) -> None:
    """Reject string flags in the typed domain profile."""
    values: dict[str, object] = {
        "task_type": TaskType.UNKNOWN,
        "complexity": Complexity.LOW,
        "input_tokens": 0,
        "risk_tier": RiskTier.LOW,
        "cache_eligible": False,
        "has_large_context": False,
    }
    values[field] = "false"

    with pytest.raises(ValidationError):
        RequestProfile.model_validate(values)
