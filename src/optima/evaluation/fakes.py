"""Deterministic fake quality evaluator for tests and local development."""

from optima.domain.quality_contract import QualityContract
from optima.evaluation.contracts import (
    EvaluationEvidence,
    EvaluationOutcome,
    EvaluationRequest,
    EvaluatorCall,
    QualityEvaluator,
)
from optima.evaluation.thresholds import ThresholdEngine


class FakeEvaluator(QualityEvaluator):
    """Return configured evidence deterministically and record every invocation."""

    def __init__(
        self,
        *,
        responses: tuple[EvaluationEvidence, ...],
        threshold_engine: ThresholdEngine | None = None,
    ) -> None:
        if not responses:
            raise ValueError("fake evaluators require at least one configured response")
        self._responses = responses
        self._threshold_engine = threshold_engine or ThresholdEngine()
        self._call_index = 0
        self._calls: list[EvaluatorCall] = []

    @property
    def calls(self) -> tuple[EvaluatorCall, ...]:
        """Return calls in the exact order they were executed."""
        return tuple(self._calls)

    async def evaluate(
        self,
        request: EvaluationRequest,
        quality_contract: QualityContract,
    ) -> EvaluationOutcome:
        """Return the next configured outcome using production threshold semantics."""
        evidence = self._responses[self._call_index % len(self._responses)]
        self._call_index += 1
        result = self._threshold_engine.evaluate(
            evidence=evidence,
            quality_contract=quality_contract,
        )
        self._calls.append(
            EvaluatorCall(
                sequence=len(self._calls),
                request=request,
                quality_contract=quality_contract,
                result=result,
            )
        )
        return EvaluationOutcome(result=result)
