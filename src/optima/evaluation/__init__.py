"""Quality evaluator contracts and deterministic implementations."""

from optima.evaluation.contracts import (
    DeterministicCheckResult,
    DeterministicMeasurement,
    EvaluationEvidence,
    EvaluationRequest,
    EvaluatorCall,
    QualityEvaluator,
)
from optima.evaluation.deterministic import (
    DeterministicEvaluator,
    ExactReferenceMeasurement,
)
from optima.evaluation.fakes import FakeEvaluator
from optima.evaluation.thresholds import (
    MANDATORY_CHECK_FAILED_PREFIX,
    EvaluationReasonCode,
    ThresholdEngine,
)

__all__ = [
    "MANDATORY_CHECK_FAILED_PREFIX",
    "DeterministicCheckResult",
    "DeterministicEvaluator",
    "DeterministicMeasurement",
    "EvaluationEvidence",
    "EvaluationReasonCode",
    "EvaluationRequest",
    "EvaluatorCall",
    "ExactReferenceMeasurement",
    "FakeEvaluator",
    "QualityEvaluator",
    "ThresholdEngine",
]
