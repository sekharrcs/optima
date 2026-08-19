"""Deterministic quality evaluator implementation."""

from optima.domain.evaluation import EvaluationResult
from optima.domain.quality_contract import QualityContract
from optima.evaluation.contracts import (
    DeterministicMeasurement,
    EvaluationEvidence,
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
    ) -> EvaluationResult:
        """Measure the candidate, then apply the explicit contract threshold."""
        evidence = self._measurement.measure(request)
        return self._threshold_engine.evaluate(
            evidence=evidence,
            quality_contract=quality_contract,
        )
