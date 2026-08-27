"""Deterministic quality evaluator implementation."""

from optima.domain.quality_contract import QualityContract
from optima.evaluation.contracts import (
    DeterministicMeasurement,
    EvaluationEvidence,
    EvaluationFailure,
    EvaluationFailureCode,
    EvaluationOutcome,
    EvaluationRequest,
    QualityEvaluator,
)
from optima.evaluation.thresholds import ThresholdEngine


class ExactReferenceMeasurement(DeterministicMeasurement):
    """Measure candidate equality against an optional reference output."""

    def measure(self, request: EvaluationRequest) -> EvaluationEvidence:
        """Return exact-reference evidence without model or network calls."""
        reference_supplied = request.reference_output is not None
        return EvaluationEvidence(
            evaluator_type="exact_reference",
            evaluator_valid=reference_supplied,
            score=float(
                reference_supplied and request.output_text == request.reference_output
            ),
            metadata={
                "method": "exact_reference",
                "reference_supplied": reference_supplied,
            },
        )


class DeterministicEvaluator(QualityEvaluator):
    """Measure deterministic evidence and apply Quality Contract semantics."""

    def __init__(
        self,
        *,
        measurement: DeterministicMeasurement,
        threshold_engine: ThresholdEngine | None = None,
    ) -> None:
        self._measurement = measurement
        self._threshold_engine = threshold_engine or ThresholdEngine()

    async def evaluate(
        self,
        request: EvaluationRequest,
        quality_contract: QualityContract,
    ) -> EvaluationOutcome:
        """Measure the candidate, then apply the explicit contract threshold."""
        if quality_contract.grounding_required and isinstance(
            self._measurement, ExactReferenceMeasurement
        ):
            return EvaluationOutcome(
                failure=EvaluationFailure(
                    evaluator_type="exact_reference",
                    code=EvaluationFailureCode.GROUNDING_NOT_SUPPORTED,
                )
            )
        evidence = self._measurement.measure(request)
        return EvaluationOutcome(
            result=self._threshold_engine.evaluate(
                evidence=evidence,
                quality_contract=quality_contract,
            )
        )
