"""Actual model-call and completed-run facts."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    ExecutionPlan,
    ExecutionStatus,
    ExecutionStep,
    ExecutionStepType,
    ModelPolicy,
    ModelRole,
)
from optima.domain.quality_contract import QualityContract
from optima.domain.request_profile import RequestProfile

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
NonNegativeDecimal = Annotated[
    Decimal,
    Field(ge=Decimal("0"), allow_inf_nan=False),
]


class RunStatus(StrEnum):
    """Final operational status of a run."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class ModelUsage(BaseModel):
    """Measured facts for one provider model call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: NonEmptyString
    run_id: NonEmptyString
    provider: NonEmptyString
    deployment: NonEmptyString
    model_role: ModelRole
    input_tokens: NonNegativeCount
    output_tokens: NonNegativeCount
    cached_tokens: NonNegativeCount | None = None
    latency_ms: NonNegativeCount
    calculated_cost: NonNegativeDecimal | None = None


class RunResult(BaseModel):
    """Final result and actual decision trace for one OPTIMA run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: NonEmptyString
    correlation_id: NonEmptyString
    created_at: datetime
    status: RunStatus
    quality_contract: QualityContract
    request_profile: RequestProfile
    execution_plan: ExecutionPlan
    steps: Annotated[tuple[ExecutionStep, ...], Field(min_length=1)]
    model_usages: tuple[ModelUsage, ...] = ()
    evaluations: tuple[EvaluationResult, ...] = ()
    final_evaluation: EvaluationResult | None = None
    final_output: NonEmptyString | None = None
    contract_met: Annotated[bool, Field(strict=True)] | None
    escalated: Annotated[bool, Field(strict=True)]
    latency_ms: NonNegativeCount
    error: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_actual_run_facts(self) -> "RunResult":
        """Enforce trace, measurement, escalation, and terminal-state consistency."""
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")

        sequences = [step.sequence for step in self.steps]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("execution-step sequences must be unique and ascending")

        has_escalation_step = any(
            step.step_type is ExecutionStepType.ESCALATION for step in self.steps
        )
        if self.escalated is not has_escalation_step:
            raise ValueError("escalated and ESCALATION execution step must agree")
        if self.escalated and (
            self.execution_plan.model_policy
            is not ModelPolicy.SMALL_FIRST_WITH_FALLBACK
        ):
            raise ValueError("only a small-first plan can record escalation")

        if any(usage.run_id != self.run_id for usage in self.model_usages):
            raise ValueError("every model usage must belong to this run")

        model_call_steps = tuple(
            step
            for step in self.steps
            if step.step_type is ExecutionStepType.MODEL_CALL
        )
        successful_model_calls = sum(
            step.status is ExecutionStatus.SUCCEEDED for step in model_call_steps
        )
        attempted_model_calls = sum(
            step.status is not ExecutionStatus.SKIPPED for step in model_call_steps
        )
        if (
            not successful_model_calls
            <= len(self.model_usages)
            <= attempted_model_calls
        ):
            raise ValueError(
                "model usage count must cover successful calls without exceeding "
                "non-skipped attempts"
            )

        evaluation_steps = tuple(
            step
            for step in self.steps
            if step.step_type is ExecutionStepType.QUALITY_EVALUATION
        )
        if self.execution_plan.model_policy is not None:
            successful_evaluations = sum(
                step.status is ExecutionStatus.SUCCEEDED for step in evaluation_steps
            )
            attempted_evaluations = sum(
                step.status is not ExecutionStatus.SKIPPED for step in evaluation_steps
            )
            if (
                not successful_evaluations
                <= len(self.evaluations)
                <= attempted_evaluations
            ):
                raise ValueError(
                    "evaluation result count must cover successful evaluations without "
                    "exceeding non-skipped attempts"
                )

        if any(
            evaluation.threshold != self.quality_contract.minimum_quality_score
            for evaluation in self.evaluations
        ):
            raise ValueError("evaluation thresholds must match the Quality Contract")

        if self.final_evaluation is not None:
            if not self.evaluations or self.evaluations[-1] != self.final_evaluation:
                raise ValueError(
                    "final_evaluation must be the final recorded evaluation"
                )
            if (
                self.final_evaluation.threshold
                != self.quality_contract.minimum_quality_score
            ):
                raise ValueError("final evaluation threshold must match the contract")

        measured_contract_met = (
            self.final_evaluation.passed
            if self.final_evaluation is not None
            and self.final_evaluation.evaluator_valid
            else None
        )
        if self.contract_met is not measured_contract_met:
            raise ValueError(
                "contract_met must reflect valid final evaluation evidence"
            )

        if self.status is RunStatus.COMPLETED:
            if self.final_output is None or self.error is not None:
                raise ValueError(
                    "completed runs require output and cannot contain an error"
                )
            if measured_contract_met is None:
                raise ValueError("completed runs require a valid final evaluation")
        elif self.error is None or self.final_output is not None:
            raise ValueError(
                "failed or timed-out runs require an error and no final output"
            )
        return self
