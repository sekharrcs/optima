"""Tests for structured quality-evaluation facts."""

import pytest
from pydantic import ValidationError

from optima.domain.evaluation import EvaluationResult


def evaluation_result(**updates: object) -> EvaluationResult:
    """Build a valid passing evaluation with optional test overrides."""
    values: dict[str, object] = {
        "evaluator_type": "deterministic",
        "evaluator_valid": True,
        "score": 0.9,
        "threshold": 0.8,
        "mandatory_checks_passed": True,
        "passed": True,
        "reasons": ("All required checks passed",),
        "metadata": {"suite": "quality"},
    }
    values.update(updates)
    return EvaluationResult.model_validate(values)


@pytest.mark.parametrize(
    ("updates", "expected_passed"),
    [
        ({"score": 0.8, "threshold": 0.8}, True),
        ({"score": 0.79, "threshold": 0.8, "passed": False}, False),
        ({"mandatory_checks_passed": False, "passed": False}, False),
        ({"evaluator_valid": False, "passed": False}, False),
    ],
)
def test_evaluation_result_represents_pass_condition(
    updates: dict[str, object],
    expected_passed: bool,
) -> None:
    """Represent evaluator validity, score, and mandatory checks explicitly."""
    result = evaluation_result(**updates)

    assert result.passed is expected_passed


@pytest.mark.parametrize(
    "updates",
    [
        {"score": 0.79, "threshold": 0.8},
        {"mandatory_checks_passed": False},
        {"evaluator_valid": False},
        {"passed": False},
    ],
)
def test_evaluation_result_rejects_inconsistent_passed_fact(
    updates: dict[str, object],
) -> None:
    """Reject pass/fail values inconsistent with the recorded evidence."""
    with pytest.raises(ValidationError, match="passed must match"):
        evaluation_result(**updates)


def test_evaluation_result_requires_reasons() -> None:
    """Require an explanation for every evaluation outcome."""
    with pytest.raises(ValidationError):
        evaluation_result(reasons=())
