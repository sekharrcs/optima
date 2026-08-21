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
    DeterministicMeasurement,
    EvaluationEvidence,
    EvaluationReasonCode,
    EvaluationRequest,
    ExactReferenceMeasurement,
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


def test_evaluation_evidence_revalidates_constructed_mandatory_checks() -> None:
    """Reject unchecked mandatory-check values before threshold evaluation."""
    constructed = DeterministicCheckResult.model_construct(
        check_id="must-pass",
        passed="false",
    )

    with pytest.raises(ValidationError, match="passed"):
        evidence(mandatory_checks=(constructed,))


def evaluation_request(**updates: object) -> EvaluationRequest:
    """Build one provider-independent evaluator request."""
    values: dict[str, object] = {
        "run_id": "run-1",
        "input_text": "Summarize the supplied incident report",
        "output_text": "Candidate answer",
        "context": "Incident report contents",
        "reference_output": "Candidate answer",
        "criteria": ("Preserve the reported outcome",),
        "metadata": {"task": "summarization"},
    }
    values.update(updates)
    return EvaluationRequest.model_validate(values)


class RecordingMeasurement:
    """Deterministic test measurement that records complete requests."""

    def __init__(self, measured_evidence: EvaluationEvidence) -> None:
        self._measured_evidence = measured_evidence
        self.calls: list[EvaluationRequest] = []

    def measure(self, request: EvaluationRequest) -> EvaluationEvidence:
        """Record the request and return evaluator-owned measured facts."""
        self.calls.append(request)
        return self._measured_evidence


class InputAwareMeasurement:
    """Test measurement whose score depends on source and candidate text."""

    def measure(self, request: EvaluationRequest) -> EvaluationEvidence:
        """Use both sides of the evaluation boundary to derive evidence."""
        expected_output = f"Answer: {request.input_text}"
        return EvaluationEvidence(
            evaluator_type="input_aware",
            evaluator_valid=True,
            score=float(request.output_text == expected_output),
            metadata={"method": "input_aware"},
        )


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


def test_evaluation_request_contains_source_candidate_and_optional_inputs() -> None:
    """Carry provider-independent inputs without caller-declared measurements."""
    request = evaluation_request()

    assert request.input_text == "Summarize the supplied incident report"
    assert request.output_text == "Candidate answer"
    assert request.context == "Incident report contents"
    assert request.reference_output == "Candidate answer"
    assert request.criteria == ("Preserve the reported outcome",)
    assert request.metadata == {"task": "summarization"}
    assert "evidence" not in EvaluationRequest.model_fields


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence", evidence()),
        ("score", 0.9),
        ("evaluator_valid", True),
        ("mandatory_checks", ()),
    ],
)
def test_evaluation_request_rejects_caller_declared_measurements(
    field: str,
    value: object,
) -> None:
    """Prevent callers from asserting facts owned by evaluator implementations."""
    values = evaluation_request().model_dump()
    values[field] = value

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EvaluationRequest.model_validate(values)


def test_deterministic_evaluator_passes_complete_request_to_measurement() -> None:
    """Give the measurement component every source and candidate input unchanged."""
    measured = evidence(
        score=0.92,
        metadata={"method": "exact-reference", "reference_count": 2},
    )
    measurement = RecordingMeasurement(measured)
    evaluator = DeterministicEvaluator(measurement=measurement)
    request = evaluation_request(
        input_text="Original task",
        output_text="Measured candidate",
        context="Relevant context",
        reference_output="Expected candidate",
        criteria=("Be exact", "Be complete"),
        metadata={"request_kind": "fixture"},
    )

    result = asyncio.run(
        evaluator.evaluate(
            request,
            quality_contract(threshold=0.9),
        )
    )

    assert measurement.calls == [request]
    assert result.evaluator_type == "deterministic"
    assert result.score == 0.92
    assert result.threshold == 0.9
    assert result.metadata == measured.metadata
    assert result.metadata is not measured.metadata


def test_measurement_derives_evidence_from_original_input_and_candidate() -> None:
    """Make source and candidate text materially affect evaluator-owned evidence."""
    evaluator = DeterministicEvaluator(measurement=InputAwareMeasurement())
    contract = quality_contract(threshold=1.0)
    matching_request = evaluation_request(
        input_text="Classify this record",
        output_text="Answer: Classify this record",
    )
    nonmatching_request = matching_request.model_copy(
        update={"output_text": "Unrelated answer"}
    )

    matching = asyncio.run(evaluator.evaluate(matching_request, contract))
    nonmatching = asyncio.run(evaluator.evaluate(nonmatching_request, contract))

    assert matching.passed is True
    assert nonmatching.passed is False
    assert matching.evaluator_type == "input_aware"


@pytest.mark.parametrize(
    ("reference_output", "output_text", "expected_valid", "expected_score"),
    [
        ("Expected", "Expected", True, 1.0),
        ("Expected", "Different", True, 0.0),
        (None, "Candidate", False, 0.0),
    ],
)
def test_exact_reference_measurement_obtains_evidence_internally(
    reference_output: str | None,
    output_text: str,
    expected_valid: bool,
    expected_score: float,
) -> None:
    """Measure exact-reference evidence from request inputs without caller facts."""
    evaluator = DeterministicEvaluator(measurement=ExactReferenceMeasurement())

    result = asyncio.run(
        evaluator.evaluate(
            evaluation_request(
                output_text=output_text,
                reference_output=reference_output,
            ),
            quality_contract(threshold=1.0),
        )
    )

    assert result.evaluator_valid is expected_valid
    assert result.score == expected_score
    assert result.passed is (expected_valid and expected_score == 1.0)
    assert result.metadata == {
        "method": "exact_reference",
        "reference_supplied": reference_output is not None,
    }


def test_request_metadata_is_json_safe_and_does_not_affect_measurement() -> None:
    """Validate request metadata without letting it alter exact-match semantics."""
    evaluator = DeterministicEvaluator(measurement=ExactReferenceMeasurement())
    contract = quality_contract(threshold=1.0)
    first = evaluation_request(metadata={"attempt": 1, "tags": ["a"]})
    second = evaluation_request(metadata={"attempt": 2, "tags": ["b"]})

    first_result = asyncio.run(evaluator.evaluate(first, contract))
    second_result = asyncio.run(evaluator.evaluate(second, contract))

    assert first_result == second_result
    with pytest.raises(ValidationError):
        evaluation_request(metadata={"unsafe": object()})


def test_fake_evaluator_cycles_configured_results_and_records_calls() -> None:
    """Keep configured evidence internal while preserving complete call history."""
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
    assert fake.calls[0].request == requests[0]
    assert "evidence" not in EvaluationRequest.model_fields


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
                "input_text": "task",
                "output_text": "answer",
                "evidence": evidence(),
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
    measurement = ExactReferenceMeasurement()

    assert isinstance(measurement, DeterministicMeasurement)
    assert isinstance(
        DeterministicEvaluator(measurement=measurement),
        QualityEvaluator,
    )
    assert isinstance(FakeEvaluator(responses=(evidence(),)), QualityEvaluator)


def test_deterministic_evaluator_applies_threshold_to_measured_evidence() -> None:
    """Compose measurement and thresholding without routing or escalation behavior."""
    measurement = RecordingMeasurement(evidence(score=0.89))
    result = asyncio.run(
        DeterministicEvaluator(measurement=measurement).evaluate(
            evaluation_request(),
            quality_contract(threshold=0.9),
        )
    )

    assert result.passed is False
    assert result.threshold == 0.9
    assert result.reasons[-1] == EvaluationReasonCode.QUALITY_CONTRACT_NOT_MET
