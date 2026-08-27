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


LLM_JUDGE_EVALUATOR_TYPE = "llm_judge"
"""Canonical evaluator_type recorded by the reference-free LLM judge."""

_LLM_JUDGE_IDENTITY_METADATA_KEYS = (
    "prompt_version",
    "request_schema_version",
    "schema_version",
    "judge_model",
    "judge_deployment",
)


def evaluator_identity_of(evaluation: EvaluationResult) -> tuple[str, ...] | None:
    """Return the canonical evaluator identity, or None when it is incomplete.

    Non-judge evaluators are identified by evaluator_type alone. The reference-free
    judge additionally binds its prompt, request/response schema versions, and model
    deployment so semantically different judges are never treated as equivalent.
    """
    evaluator_type = evaluation.evaluator_type
    if evaluator_type != LLM_JUDGE_EVALUATOR_TYPE:
        return (evaluator_type,)
    identity: list[str] = [evaluator_type]
    for key in _LLM_JUDGE_IDENTITY_METADATA_KEYS:
        value = evaluation.metadata.get(key)
        if not isinstance(value, str) or not value:
            return None
        identity.append(value)
    return tuple(identity)


def evaluator_identities_compatible(
    current_identity: tuple[str, ...] | None,
    prior_evaluation: EvaluationResult,
) -> bool:
    """Return whether a prior evaluation may be reused under the current evaluator.

    A missing current identity disables the check. Otherwise the prior evaluation must
    resolve to a complete identity equal to the current one, so an LLM_JUDGE result can
    never satisfy an EXACT_REFERENCE request or a differently versioned judge.
    """
    if current_identity is None:
        return True
    prior_identity = evaluator_identity_of(prior_evaluation)
    return prior_identity is not None and prior_identity == current_identity
