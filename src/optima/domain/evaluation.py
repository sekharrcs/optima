"""Structured quality-evaluation facts."""

from typing import Annotated

from pydantic import Field, model_validator

from optima.domain.quality_contract import QualityScore
from optima.immutable import ImmutableJsonObject, ImmutableModel

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]


class EvaluationResult(ImmutableModel):
    """Measured quality evidence produced by an evaluator."""

    evaluator_type: NonEmptyString
    evaluator_valid: Annotated[bool, Field(strict=True)]
    score: QualityScore
    threshold: QualityScore
    mandatory_checks_passed: Annotated[bool, Field(strict=True)]
    passed: Annotated[bool, Field(strict=True)]
    reasons: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]
    metadata: ImmutableJsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_pass_condition(self) -> "EvaluationResult":
        """Keep the stored pass/fail fact consistent with contract semantics."""
        measured_pass = (
            self.evaluator_valid
            and self.score >= self.threshold
            and self.mandatory_checks_passed
        )
        if self.passed is not measured_pass:
            raise ValueError(
                "passed must match evaluator, threshold, and mandatory checks"
            )
        return self
