"""Tests for structured quality-evaluation facts."""

import asyncio

import pytest
from pydantic import ValidationError

from optima.domain.evaluation import EvaluationResult
from optima.domain.quality_contract import (
    OptimizationMode,
    QualityContract,
    QualityProfile,
    RiskTier,
)
from optima.evaluation import (
    MANDATORY_CHECK_FAILED_PREFIX,
    DeterministicCheckResult,
    DeterministicEvaluator,
    EvaluationEvidence,
    EvaluationReasonCode,
    EvaluationRequest,
    FakeEvaluator,
    QualityEvaluator,
    ThresholdEngine,
)


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


def quality_contract(*, threshold: float = 0.8) -> QualityContract:
    """Build an explicit contract for evaluator behavior tests."""
    return QualityContract(
        quality_profile=QualityProfile.STANDARD,
        minimum_quality_score=threshold,
        optimization_mode=OptimizationMode.BALANCED,
        risk_tier=RiskTier.LOW,
    )


def evidence(**updates: object) -> EvaluationEvidence:
    """Build valid deterministic evidence with optional overrides."""
    values: dict[str, object] = {
        "evaluator_type": "deterministic",
        "evaluator_valid": True,
        "score": 0.9,
        "mandatory_checks": (),
        "metadata": {"method": "fixture"},
    }
    values.update(updates)
    return EvaluationEvidence.model_validate(values)


def evaluation_request(**updates: object) -> EvaluationRequest:
    """Build one provider-independent evaluator request."""
    values: dict[str, object] = {
        "run_id": "run-1",
        "output_text": "Candidate answer",
        "evidence": evidence(),
    }
    values.update(updates)
    return EvaluationRequest.model_validate(values)


@pytest.mark.parametrize(
    ("score", "expected_passed", "threshold_reason"),
    [
        (0.81, True, EvaluationReasonCode.QUALITY_THRESHOLD_MET),
        (0.80, True, EvaluationReasonCode.QUALITY_THRESHOLD_MET),
        (0.79, False, EvaluationReasonCode.QUALITY_THRESHOLD_NOT_MET),
    ],
)
def test_threshold_engine_applies_inclusive_contract_threshold(
    score: float,
    expected_passed: bool,
    threshold_reason: EvaluationReasonCode,
) -> None:
    """Pass scores at or above the threshold and fail lower scores."""
    result = ThresholdEngine().evaluate(
        evidence=evidence(score=score),
        quality_contract=quality_contract(threshold=0.8),
    )

    assert result.passed is expected_passed
    assert result.threshold == 0.8
    assert threshold_reason in result.reasons


def test_invalid_evaluator_fails_regardless_of_score() -> None:
    """Reject invalid evaluator evidence even with a perfect score."""
    result = ThresholdEngine().evaluate(
        evidence=evidence(score=1.0, evaluator_valid=False),
        quality_contract=quality_contract(),
    )

    assert result.passed is False
    assert EvaluationReasonCode.EVALUATOR_INVALID in result.reasons
    assert EvaluationReasonCode.QUALITY_CONTRACT_NOT_MET in result.reasons


def test_failed_mandatory_check_fails_regardless_of_score() -> None:
    """Reject a perfect score when any mandatory deterministic check fails."""
    result = ThresholdEngine().evaluate(
        evidence=evidence(
            score=1.0,
            mandatory_checks=(
                DeterministicCheckResult(check_id="schema", passed=False),
            ),
        ),
        quality_contract=quality_contract(),
    )

    assert result.mandatory_checks_passed is False
    assert result.passed is False
    assert f"{MANDATORY_CHECK_FAILED_PREFIX}:schema" in result.reasons


def test_multiple_mandatory_checks_report_each_failure_in_input_order() -> None:
    """Evaluate all mandatory checks and identify failures deterministically."""
    checks = (
        DeterministicCheckResult(check_id="json-schema", passed=True),
        DeterministicCheckResult(check_id="required-fields", passed=False),
        DeterministicCheckResult(check_id="unit-tests", passed=False),
    )
    engine = ThresholdEngine()

    first = engine.evaluate(
        evidence=evidence(mandatory_checks=checks),
        quality_contract=quality_contract(),
    )
    second = engine.evaluate(
        evidence=evidence(mandatory_checks=checks),
        quality_contract=quality_contract(),
    )

    assert first.reasons == second.reasons
    assert first.reasons == (
        EvaluationReasonCode.EVALUATOR_VALID,
        EvaluationReasonCode.QUALITY_THRESHOLD_MET,
        f"{MANDATORY_CHECK_FAILED_PREFIX}:required-fields",
        f"{MANDATORY_CHECK_FAILED_PREFIX}:unit-tests",
        EvaluationReasonCode.QUALITY_CONTRACT_NOT_MET,
    )


def test_passing_checks_and_sufficient_score_pass() -> None:
    """Pass when valid evidence meets the score and every mandatory check."""
    result = ThresholdEngine().evaluate(
        evidence=evidence(
            mandatory_checks=(
                DeterministicCheckResult(check_id="schema", passed=True),
                DeterministicCheckResult(check_id="reference", passed=True),
            )
        ),
        quality_contract=quality_contract(),
    )

    assert result.passed is True
    assert result.mandatory_checks_passed is True
    assert result.reasons[-1] == EvaluationReasonCode.QUALITY_CONTRACT_MET
    assert all(result.reasons)


def test_threshold_always_comes_from_quality_contract() -> None:
    """Copy the supplied contract threshold without profile-default lookup."""
    contract = quality_contract(threshold=0.87)

    result = ThresholdEngine().evaluate(
        evidence=evidence(score=0.87),
        quality_contract=contract,
    )

    assert result.threshold == contract.minimum_quality_score


def test_deterministic_evaluator_preserves_measured_facts_and_metadata() -> None:
    """Return explicit evidence without claiming unmeasured quality facts."""
    measured = evidence(
        score=0.92,
        metadata={"method": "exact-reference", "reference_count": 2},
    )
    evaluator = DeterministicEvaluator()

    result = asyncio.run(
        evaluator.evaluate(
            evaluation_request(evidence=measured),
            quality_contract(threshold=0.9),
        )
    )

    assert result.evaluator_type == "deterministic"
    assert result.score == 0.92
    assert result.threshold == 0.9
    assert result.metadata == measured.metadata
    assert result.metadata is not measured.metadata


def test_fake_evaluator_cycles_configured_results_and_records_calls() -> None:
    """Return configured outcomes repeatably while preserving invocation order."""
    fake = FakeEvaluator(
        responses=(
            evidence(score=0.8),
            evidence(score=0.79, evaluator_valid=False),
        )
    )
    contract = quality_contract(threshold=0.8)
    requests = (
        evaluation_request(run_id="run-1"),
        evaluation_request(run_id="run-2"),
        evaluation_request(run_id="run-3"),
    )

    results = tuple(
        asyncio.run(fake.evaluate(request, contract)) for request in requests
    )

    assert tuple(result.passed for result in results) == (True, False, True)
    assert tuple(call.sequence for call in fake.calls) == (0, 1, 2)
    assert tuple(call.request.run_id for call in fake.calls) == (
        "run-1",
        "run-2",
        "run-3",
    )
    assert fake.calls[0].quality_contract == contract
    assert fake.calls[0].result == results[0]


def test_fake_evaluator_supports_mandatory_check_failure() -> None:
    """Use the same mandatory-check semantics in the injectable fake."""
    fake = FakeEvaluator(
        responses=(
            evidence(
                score=1.0,
                mandatory_checks=(
                    DeterministicCheckResult(check_id="tests", passed=False),
                ),
            ),
        )
    )

    result = asyncio.run(
        fake.evaluate(evaluation_request(), quality_contract(threshold=0.8))
    )

    assert result.passed is False
    assert result.mandatory_checks_passed is False


@pytest.mark.parametrize(
    "factory",
    [
        lambda: EvaluationEvidence.model_validate(
            {
                "evaluator_type": "deterministic",
                "evaluator_valid": True,
                "score": 0.9,
                "mandatory_checks": (
                    {"check_id": "duplicate", "passed": True},
                    {"check_id": "duplicate", "passed": False},
                ),
            }
        ),
        lambda: EvaluationRequest.model_validate(
            {
                "run_id": "run-1",
                "output_text": "answer",
                "evidence": evidence(),
                "unexpected": True,
            }
        ),
        lambda: DeterministicCheckResult.model_validate(
            {"check_id": "schema", "passed": 1}
        ),
    ],
)
def test_evaluator_contracts_reject_malformed_or_ambiguous_inputs(
    factory: object,
) -> None:
    """Reject duplicate checks, unknown fields, and coerced check outcomes."""
    with pytest.raises(ValidationError):
        assert callable(factory)
        factory()


def test_fake_evaluator_requires_a_configured_response() -> None:
    """Reject a fake that cannot produce explicit evaluation evidence."""
    with pytest.raises(ValueError, match="at least one"):
        FakeEvaluator(responses=())


def test_evaluators_implement_async_protocol() -> None:
    """Keep deterministic and fake implementations injectable by one boundary."""
    assert isinstance(DeterministicEvaluator(), QualityEvaluator)
    assert isinstance(FakeEvaluator(responses=(evidence(),)), QualityEvaluator)


def test_evaluator_does_not_invoke_provider_or_planner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep evaluation isolated from model execution and plan selection."""

    async def unexpected_provider_call(*args: object, **kwargs: object) -> object:
        raise AssertionError("model provider must not be invoked by evaluator")

    def unexpected_planner_call(*args: object, **kwargs: object) -> object:
        raise AssertionError("planner must not be invoked by evaluator")

    monkeypatch.setattr(
        "optima.providers.fakes.FakeModelProvider.generate",
        unexpected_provider_call,
    )
    monkeypatch.setattr(
        "optima.planner.planner.select_plan",
        unexpected_planner_call,
    )

    result = asyncio.run(
        DeterministicEvaluator().evaluate(
            evaluation_request(),
            quality_contract(),
        )
    )

    assert result.passed is True
