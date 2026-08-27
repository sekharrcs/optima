"""Provider-independent quality evaluator contracts."""

from enum import StrEnum
from typing import Annotated, Protocol, runtime_checkable

from pydantic import Field, model_validator

from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import ModelRole
from optima.domain.quality_contract import QualityContract, QualityScore
from optima.domain.run import ModelUsage
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


class EvaluationOutcome(ImmutableModel):
    """Evaluation result and actual model usage consumed to produce it."""

    result: EvaluationResult | None = None
    failure: "EvaluationFailure | None" = None
    model_usages: tuple[ModelUsage, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> "EvaluationOutcome":
        """Require exactly one result or failure and evaluator-only model roles."""
        if (self.result is None) is (self.failure is None):
            raise ValueError(
                "evaluation outcome requires exactly one result or failure"
            )
        if any(usage.model_role is not ModelRole.JUDGE for usage in self.model_usages):
            raise ValueError("evaluation model usage requires the JUDGE role")
        return self


class EvaluationFailureCode(StrEnum):
    """Stable failure categories for evaluation without fabricated scores."""

    GROUNDING_CONTEXT_REQUIRED = "GROUNDING_CONTEXT_REQUIRED"
    GROUNDING_NOT_SUPPORTED = "GROUNDING_NOT_SUPPORTED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"


class EvaluationFailure(ImmutableModel):
    """Score-free evidence that an evaluator could not be trusted."""

    evaluator_type: NonEmptyString
    code: EvaluationFailureCode
    timed_out: Annotated[bool, Field(strict=True)] = False


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
    ) -> EvaluationOutcome:
        """Return measured contract evidence or a score-free failure."""
