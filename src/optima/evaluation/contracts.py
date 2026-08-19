"""Provider-independent quality evaluator contracts."""

from typing import Annotated, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from optima.domain.evaluation import EvaluationResult
from optima.domain.quality_contract import QualityContract, QualityScore

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]


class DeterministicCheckResult(BaseModel):
    """Measured outcome of one mandatory deterministic check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check_id: NonEmptyString
    passed: Annotated[bool, Field(strict=True)]


class EvaluationEvidence(BaseModel):
    """Explicit measured facts consumed by deterministic evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluator_type: NonEmptyString
    evaluator_valid: Annotated[bool, Field(strict=True)]
    score: QualityScore
    mandatory_checks: tuple[DeterministicCheckResult, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unique_check_ids(self) -> "EvaluationEvidence":
        """Keep mandatory-check evidence unambiguous and deterministic."""
        check_ids = [check.check_id for check in self.mandatory_checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("mandatory check_id values must be unique")
        return self


class EvaluationRequest(BaseModel):
    """Input for evaluating one candidate output against a Quality Contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: NonEmptyString
    output_text: NonEmptyString
    evidence: EvaluationEvidence


class EvaluatorCall(BaseModel):
    """Recorded evaluator invocation for deterministic test assertions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: NonNegativeCount
    request: EvaluationRequest
    quality_contract: QualityContract
    result: EvaluationResult


@runtime_checkable
class QualityEvaluator(Protocol):
    """Asynchronous boundary for provider-independent quality evaluation."""

    async def evaluate(
        self,
        request: EvaluationRequest,
        quality_contract: QualityContract,
    ) -> EvaluationResult:
        """Measure candidate quality and return structured contract evidence."""
