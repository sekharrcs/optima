"""Deterministic quality evaluator implementation."""

from optima.domain.evaluation import EvaluationResult
from optima.domain.quality_contract import QualityContract
from optima.evaluation.contracts import EvaluationRequest, QualityEvaluator
from optima.evaluation.thresholds import ThresholdEngine


class DeterministicEvaluator(QualityEvaluator):
    """Evaluate explicit deterministic evidence without model or network calls."""

    def __init__(self, *, threshold_engine: ThresholdEngine | None = None) -> None:
        self._threshold_engine = threshold_engine or ThresholdEngine()

    async def evaluate(
        self,
        request: EvaluationRequest,
        quality_contract: QualityContract,
    ) -> EvaluationResult:
        """Apply the Quality Contract to the supplied deterministic evidence."""
        return self._threshold_engine.evaluate(
            evidence=request.evidence,
            quality_contract=quality_contract,
        )
