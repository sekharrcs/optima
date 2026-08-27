"""Quality evaluator contracts and deterministic implementations."""

from optima.evaluation.contracts import (
    DeterministicCheckResult,
    DeterministicMeasurement,
    EvaluationEvidence,
    EvaluationFailure,
    EvaluationFailureCode,
    EvaluationOutcome,
    EvaluationRequest,
    EvaluatorCall,
    QualityEvaluator,
)
from optima.evaluation.deterministic import (
    DeterministicEvaluator,
    ExactReferenceMeasurement,
)
from optima.evaluation.fakes import FakeEvaluator
from optima.evaluation.llm_judge import (
    LLM_JUDGE_EVALUATOR_TYPE,
    LLM_JUDGE_PROMPT_VERSION,
    LLM_JUDGE_REQUEST_SCHEMA_VERSION,
    LLM_JUDGE_RESPONSE_SCHEMA_VERSION,
    LLM_JUDGE_SYSTEM_INSTRUCTION,
    JudgeCriterionMeasurement,
    JudgeReasonCode,
    JudgeResponse,
    LLMJudgeEvaluator,
)
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
    "EvaluationFailure",
    "EvaluationFailureCode",
    "EvaluationOutcome",
    "EvaluationReasonCode",
    "EvaluationRequest",
    "EvaluatorCall",
    "ExactReferenceMeasurement",
    "FakeEvaluator",
    "JudgeCriterionMeasurement",
    "JudgeReasonCode",
    "JudgeResponse",
    "LLMJudgeEvaluator",
    "LLM_JUDGE_EVALUATOR_TYPE",
    "LLM_JUDGE_PROMPT_VERSION",
    "LLM_JUDGE_REQUEST_SCHEMA_VERSION",
    "LLM_JUDGE_RESPONSE_SCHEMA_VERSION",
    "LLM_JUDGE_SYSTEM_INSTRUCTION",
    "QualityEvaluator",
    "ThresholdEngine",
]
