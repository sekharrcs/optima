"""Provider-independent quality evaluator contracts."""

from typing import Annotated, Protocol, runtime_checkable

from pydantic import Field, model_validator

from optima.domain.evaluation import EvaluationResult
from optima.domain.quality_contract import QualityContract, QualityScore
from optima.immutable import ImmutableJsonObject, ImmutableModel

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]


class DeterministicCheckResult(ImmutableModel):
    """Measured outcome of one mandatory deterministic check."""

    check_id: NonEmptyString
    passed: Annotated[bool, Field(strict=True)]


class EvaluationEvidence(ImmutableModel):
    """Explicit measured facts consumed by deterministic evaluation."""

    evaluator_type: NonEmptyString
    evaluator_valid: Annotated[bool, Field(strict=True)]
    score: QualityScore
    mandatory_checks: tuple[DeterministicCheckResult, ...] = ()
    metadata: ImmutableJsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unique_check_ids(self) -> "EvaluationEvidence":
        """Keep mandatory-check evidence unambiguous and deterministic."""
        check_ids = [check.check_id for check in self.mandatory_checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("mandatory check_id values must be unique")
        return self


class EvaluationRequest(ImmutableModel):
    """Input for evaluating one candidate output against a Quality Contract."""

    run_id: NonEmptyString
    input_text: NonEmptyString
    output_text: NonEmptyString
    context: NonEmptyString | None = None
    reference_output: NonEmptyString | None = None
    criteria: tuple[NonEmptyString, ...] = ()
    metadata: ImmutableJsonObject = Field(default_factory=dict)


@runtime_checkable
class DeterministicMeasurement(Protocol):
    """Synchronous boundary for measuring explicit deterministic evidence."""

    def measure(self, request: EvaluationRequest) -> EvaluationEvidence:
        """Inspect a complete evaluation request and return measured facts."""


class EvaluatorCall(ImmutableModel):
    """Recorded evaluator invocation for deterministic test assertions."""

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
