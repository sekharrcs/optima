"""Immutable contracts for measured baseline-versus-OPTIMA comparisons."""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from optima.domain.evaluation import EvaluationResult
from optima.domain.quality_contract import QualityScore
from optima.domain.run import PricingProvenance, RunResult

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
NonNegativeDecimal = Annotated[
    Decimal,
    Field(ge=Decimal("0"), allow_inf_nan=False),
]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
StrictBoolean = Annotated[bool, Field(strict=True)]
LLM_JUDGE_IDENTITY_KEYS = (
    "prompt_version",
    "schema_version",
    "judge_model",
    "judge_deployment",
)


class ComparisonArm(StrEnum):
    """Explicit role of one run in a measured comparison."""

    BASELINE = "BASELINE"
    OPTIMA = "OPTIMA"


class BenchmarkCaseIdentity(BaseModel):
    """Stable identity proving that two runs used the same benchmark input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_case_id: NonEmptyString
    input_fingerprint: NonEmptyString


class ComparableRun(BaseModel):
    """One explicitly labeled measured run and its benchmark identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: ComparisonArm
    identity: BenchmarkCaseIdentity
    run_result: RunResult


class BaselineComparisonRequest(BaseModel):
    """Validated baseline and OPTIMA runs eligible for comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline: ComparableRun
    optima: ComparableRun

    @model_validator(mode="after")
    def validate_compatibility(self) -> "BaselineComparisonRequest":
        """Reject mislabeled or incompatible measurements."""
        if self.baseline.arm is not ComparisonArm.BASELINE:
            raise ValueError("baseline run must use the BASELINE arm")
        if self.optima.arm is not ComparisonArm.OPTIMA:
            raise ValueError("OPTIMA run must use the OPTIMA arm")
        if self.baseline.identity != self.optima.identity:
            raise ValueError("comparison runs must have the same benchmark identity")

        baseline_result = self.baseline.run_result
        optima_result = self.optima.run_result
        if baseline_result.run_id == optima_result.run_id:
            raise ValueError("comparison runs must have different run IDs")
        if baseline_result.request_profile != optima_result.request_profile:
            raise ValueError("comparison runs must have the same RequestProfile")
        if baseline_result.quality_contract != optima_result.quality_contract:
            raise ValueError("comparison runs must have the same Quality Contract")

        baseline_cost = baseline_result.total_calculated_cost
        optima_cost = optima_result.total_calculated_cost
        if baseline_cost is not None and optima_cost is not None:
            baseline_provenance = baseline_result.total_cost_provenance
            optima_provenance = optima_result.total_cost_provenance
            if baseline_provenance is None or optima_provenance is None:
                raise ValueError(
                    "calculated cost comparisons require pricing provenance"
                )
            if baseline_provenance != optima_provenance:
                raise ValueError(
                    "calculated cost comparisons require the same pricing provenance"
                )

        baseline_evaluation = baseline_result.final_evaluation
        optima_evaluation = optima_result.final_evaluation
        if (
            baseline_evaluation is not None
            and baseline_evaluation.evaluator_valid
            and optima_evaluation is not None
            and optima_evaluation.evaluator_valid
            and _evaluator_identity(baseline_evaluation)
            != _evaluator_identity(optima_evaluation)
        ):
            raise ValueError(
                "valid final evaluations must use the same evaluator identity"
            )
        return self


def _evaluator_identity(evaluation: EvaluationResult) -> tuple[str, ...]:
    """Return complete measurement identity for comparison compatibility."""
    evaluator_type = evaluation.evaluator_type
    if evaluator_type != "llm_judge":
        return (evaluator_type,)
    identity_values: list[str] = []
    for key in LLM_JUDGE_IDENTITY_KEYS:
        value = evaluation.metadata.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(
                "valid llm_judge evaluations require complete evaluator identity"
            )
        identity_values.append(value)
    return (evaluator_type, *identity_values)


class ExecutionMetrics(BaseModel):
    """Measured execution and valid final-quality facts for one arm."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: ComparisonArm
    run_id: NonEmptyString
    model_calls: NonNegativeCount
    input_tokens: NonNegativeCount | None
    output_tokens: NonNegativeCount | None
    total_tokens: NonNegativeCount | None
    cost: NonNegativeDecimal | None
    cost_provenance: PricingProvenance | None
    latency_ms: NonNegativeCount
    evaluator_type: NonEmptyString | None
    quality_score: QualityScore | None
    contract_met: StrictBoolean | None

    @model_validator(mode="after")
    def validate_cost_provenance_pair(self) -> "ExecutionMetrics":
        """Prevent comparison metrics from separating cost and provenance."""
        if (self.cost is None) is not (self.cost_provenance is None):
            raise ValueError("cost and cost_provenance must be provided together")
        return self


class BaselineComparison(BaseModel):
    """Side-by-side measurements and truthful OPTIMA-relative differences."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: BenchmarkCaseIdentity
    baseline: ExecutionMetrics
    optima: ExecutionMetrics
    model_calls_delta: int
    input_tokens_delta: int | None
    output_tokens_delta: int | None
    total_tokens_delta: int | None
    cost_delta: FiniteDecimal | None
    latency_ms_delta: int
    quality_score_delta: float | None
    token_reduction_percentage: FiniteDecimal | None
    cost_reduction_percentage: FiniteDecimal | None
    latency_percentage_change: FiniteDecimal | None
