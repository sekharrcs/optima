"""Actual model-call and completed-run facts."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    ContextReductionOutcome,
    ContextSource,
    ExecutionEventCode,
    ExecutionPlan,
    ExecutionStatus,
    ExecutionStep,
    ExecutionStepType,
    ModelPolicy,
    ModelRole,
    SemanticCacheEvidence,
    SemanticCacheOutcome,
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


class PricingProvenance(BaseModel):
    """Catalog identity governing one authoritative calculated cost."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: NonEmptyString
    currency: NonEmptyString


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
    pricing_provenance: PricingProvenance | None = None

    @model_validator(mode="after")
    def validate_usage_measurements(self) -> "ModelUsage":
        """Validate cached input and the authoritative cost/provenance pair."""
        if self.cached_tokens is not None and self.cached_tokens > self.input_tokens:
            raise ValueError("cached_tokens must not exceed input_tokens")
        if (self.calculated_cost is None) is not (self.pricing_provenance is None):
            raise ValueError(
                "calculated_cost and pricing_provenance must be provided together"
            )
        return self


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
    semantic_cache: SemanticCacheEvidence | None = None
    steps: Annotated[tuple[ExecutionStep, ...], Field(min_length=1)]
    model_usages: tuple[ModelUsage, ...] = ()
    evaluations: tuple[EvaluationResult, ...] = ()
    final_evaluation: EvaluationResult | None = None
    final_output: NonEmptyString | None = None
    contract_met: Annotated[bool, Field(strict=True)] | None
    escalated: Annotated[bool, Field(strict=True)]
    latency_ms: NonNegativeCount
    error: NonEmptyString | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_input_tokens(self) -> int | None:
        """Return exact input tokens only when every attempted call has usage."""
        usages = self._complete_model_usages()
        if usages is None:
            return None
        return sum(usage.input_tokens for usage in usages)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_output_tokens(self) -> int | None:
        """Return exact output tokens only when every attempted call has usage."""
        usages = self._complete_model_usages()
        if usages is None:
            return None
        return sum(usage.output_tokens for usage in usages)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tokens(self) -> int | None:
        """Return exact combined tokens without double-counting cached tokens."""
        input_tokens = self.total_input_tokens
        output_tokens = self.total_output_tokens
        if input_tokens is None or output_tokens is None:
            return None
        return input_tokens + output_tokens

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_calculated_cost(self) -> Decimal | None:
        """Sum exact Decimal costs only when every attempted cost is available."""
        usages = self._complete_model_usages()
        if usages is None or not usages:
            return None
        total = Decimal("0")
        for usage in usages:
            if usage.calculated_cost is None:
                return None
            total += usage.calculated_cost
        return total

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_cost_provenance(self) -> PricingProvenance | None:
        """Return provenance only for one complete compatible run total."""
        usages = self._complete_model_usages()
        if usages is None or not usages:
            return None
        provenance = usages[0].pricing_provenance
        if provenance is None:
            return None
        if any(
            usage.calculated_cost is None or usage.pricing_provenance != provenance
            for usage in usages
        ):
            return None
        return provenance

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
        if self.execution_plan.model_policy is ModelPolicy.STRONG_DIRECT:
            escalation_events = {
                ExecutionEventCode.ESCALATION_REQUIRED,
                ExecutionEventCode.ESCALATED_TO_STRONG,
            }
            if (
                has_escalation_step
                or self.escalated
                or any(
                    escalation_events.intersection(step.event_codes)
                    for step in self.steps
                )
            ):
                raise ValueError("strong-direct runs cannot record escalation evidence")
        if self.escalated is not has_escalation_step:
            raise ValueError("escalated and ESCALATION execution step must agree")
        if self.escalated and (
            self.execution_plan.model_policy
            is not ModelPolicy.SMALL_FIRST_WITH_FALLBACK
        ):
            raise ValueError("only a small-first plan can record escalation")

        if any(usage.run_id != self.run_id for usage in self.model_usages):
            raise ValueError("every model usage must belong to this run")

        cost_provenances = {
            (
                usage.pricing_provenance.catalog_version,
                usage.pricing_provenance.currency,
            )
            for usage in self.model_usages
            if usage.pricing_provenance is not None
        }
        if len(cost_provenances) > 1:
            raise ValueError(
                "all calculated costs in one run must use compatible provenance"
            )

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
        if self.execution_plan.model_policy is ModelPolicy.STRONG_DIRECT:
            if attempted_model_calls != 1:
                raise ValueError(
                    "strong-direct runs require exactly one model-call attempt"
                )
            if any(
                step.facts.get("model_role") != ModelRole.STRONG.value
                for step in model_call_steps
            ):
                raise ValueError(
                    "strong-direct model-call steps require STRONG model_role facts"
                )
            if any(
                usage.model_role is not ModelRole.STRONG for usage in self.model_usages
            ):
                raise ValueError("strong-direct runs require STRONG model usage")
            if any(
                step.step_type
                in {
                    ExecutionStepType.QUALITY_EVALUATION,
                    ExecutionStepType.RETURN,
                }
                and step.facts.get("model_role") != ModelRole.STRONG.value
                for step in self.steps
            ):
                raise ValueError(
                    "strong-direct evaluation and return steps require STRONG "
                    "model_role facts"
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

        reduction_steps = tuple(
            step
            for step in self.steps
            if step.step_type is ExecutionStepType.CONTEXT_REDUCTION
        )
        if self.execution_plan.context_policy is ContextPolicy.REDUCE:
            expected_reduction_index = int(
                bool(self.steps)
                and self.steps[0].step_type is ExecutionStepType.SEMANTIC_CACHE
            )
            if (
                len(reduction_steps) != 1
                or reduction_steps[0] is not self.steps[expected_reduction_index]
            ):
                raise ValueError(
                    "REDUCE plans require one leading context-reduction step after "
                    "any cache lookup step"
                )
            reduction = reduction_steps[0].context_reduction
            if reduction is None:
                raise ValueError("context-reduction step requires typed evidence")
            expected_source = (
                ContextSource.REDUCED
                if reduction.outcome is ContextReductionOutcome.APPLIED
                else ContextSource.ORIGINAL
            )
            if any(
                step.context_source is not expected_source for step in model_call_steps
            ):
                raise ValueError(
                    "model-call context source must match reduction outcome"
                )
        elif reduction_steps:
            raise ValueError("non-REDUCE plans cannot record reduction attempts")

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
            if self.execution_plan.model_policy is ModelPolicy.STRONG_DIRECT:
                expected_evaluation_attempts = int(successful_model_calls == 1)
                if attempted_evaluations != expected_evaluation_attempts:
                    raise ValueError(
                        "strong-direct evaluation attempts must match "
                        "model-call success"
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

        is_cache_hit = self.execution_plan.cache_policy is CachePolicy.USE_CACHED_RESULT
        if not is_cache_hit and any(
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

        measured_contract_met = self._measured_contract_met(is_cache_hit=is_cache_hit)
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

    def _measured_contract_met(self, *, is_cache_hit: bool) -> bool | None:
        """Derive compliance from current evaluation or accepted source evidence."""
        cache_steps = tuple(
            step
            for step in self.steps
            if step.step_type is ExecutionStepType.SEMANTIC_CACHE
        )
        if self.semantic_cache is not None:
            self._validate_cache_outcome_binding(self.semantic_cache)
        if is_cache_hit:
            candidate = self.execution_plan.cache_candidate
            evidence = self.semantic_cache
            forbidden_steps = {
                ExecutionStepType.CONTEXT_REDUCTION,
                ExecutionStepType.MODEL_CALL,
                ExecutionStepType.QUALITY_EVALUATION,
                ExecutionStepType.ESCALATION,
            }
            if candidate is None or evidence is None:
                raise ValueError("cache runs require bound candidate and evidence")
            if evidence.outcome is not SemanticCacheOutcome.REUSED:
                raise ValueError("cache runs require a reused outcome")
            if len(cache_steps) != 1 or cache_steps[0] is not self.steps[0]:
                raise ValueError("cache runs require one leading cache step")
            if cache_steps[0].semantic_cache != evidence:
                raise ValueError("cache step must match top-level cache evidence")
            if (
                len(self.steps) != 2
                or self.steps[1].step_type is not ExecutionStepType.RETURN
            ):
                raise ValueError("cache runs require exactly cache and return steps")
            if any(step.step_type in forbidden_steps for step in self.steps):
                raise ValueError("cache reuse cannot record model-path execution")
            if (
                self.model_usages
                or self.evaluations
                or self.final_evaluation is not None
            ):
                raise ValueError("cache reuse cannot claim current execution evidence")
            if self.final_output != candidate.output_text:
                raise ValueError("cache output must match the bound candidate")
            if self.run_id == candidate.source_run_id:
                raise ValueError("cache source run must differ from current run")
            source = candidate.prior_evaluation
            accepted = (
                evidence.source_run_id == candidate.source_run_id
                and evidence.similarity == candidate.similarity
                and evidence.prior_evaluation == source
                and candidate.similarity
                >= self.execution_plan.decision_evidence.cache_similarity_threshold
                and source.evaluator_valid
                and source.passed
                and source.mandatory_checks_passed
                and source.score >= self.quality_contract.minimum_quality_score
                and candidate.contract_compatible
                and candidate.safe_to_reuse
            )
            if not accepted:
                raise ValueError("cache result lacks valid accepted source evidence")
            return True
        if self.semantic_cache is not None:
            if self.semantic_cache.outcome is SemanticCacheOutcome.REUSED:
                raise ValueError("model runs cannot claim a reused cache result")
            attempted = self.semantic_cache.outcome not in {
                SemanticCacheOutcome.DISABLED_BYPASSED,
                SemanticCacheOutcome.INELIGIBLE_BYPASSED,
            }
            if attempted:
                if len(cache_steps) != 1 or cache_steps[0] is not self.steps[0]:
                    raise ValueError("attempted lookup requires one leading cache step")
                if cache_steps[0].semantic_cache != self.semantic_cache:
                    raise ValueError("cache step must match top-level cache evidence")
            elif cache_steps:
                raise ValueError("cache bypass cannot record a lookup step")
        elif cache_steps:
            raise ValueError("cache steps require top-level cache evidence")
        return (
            self.final_evaluation.passed
            if self.final_evaluation is not None
            and self.final_evaluation.evaluator_valid
            else None
        )

    def _validate_cache_outcome_binding(
        self,
        evidence: SemanticCacheEvidence,
    ) -> None:
        """Keep transported cache facts aligned with the selected plan."""
        plan = self.execution_plan
        if evidence.planner_reason_code not in plan.reason_codes:
            raise ValueError("cache evidence reason must appear in the selected plan")
        disabled = evidence.outcome is SemanticCacheOutcome.DISABLED_BYPASSED
        if disabled is plan.decision_evidence.module_states.semantic_cache_enabled:
            raise ValueError("cache outcome must match the planned module state")
        assessed = evidence.outcome in {
            SemanticCacheOutcome.MATCH_REJECTED,
            SemanticCacheOutcome.REUSED,
        }
        if assessed is not plan.decision_evidence.cache_candidate_assessed:
            raise ValueError("cache outcome must match candidate-assessed evidence")

    def _complete_model_usages(self) -> tuple[ModelUsage, ...] | None:
        """Return usage only when every attempted model call has measurements."""
        attempted_model_calls = sum(
            step.step_type is ExecutionStepType.MODEL_CALL
            and step.status is not ExecutionStatus.SKIPPED
            for step in self.steps
        )
        if len(self.model_usages) != attempted_model_calls:
            return None
        return self.model_usages
