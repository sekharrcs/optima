"""Actual model-call and completed-run facts."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import Field, computed_field, model_validator

from optima.domain.embedding import EmbeddingAttempt, EmbeddingUsage
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
    validate_semantic_cache_binding,
)
from optima.domain.pricing import PricingProvenance as PricingProvenance
from optima.domain.quality_contract import QualityContract
from optima.domain.request_binding import RequestBinding
from optima.domain.request_profile import RequestProfile
from optima.immutable import ImmutableModel

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


class ModelUsage(ImmutableModel):
    """Measured facts for one provider model call."""

    request_id: NonEmptyString | None = None
    run_id: NonEmptyString
    provider: NonEmptyString
    deployment: NonEmptyString
    model_role: ModelRole
    input_tokens: NonNegativeCount | None = None
    output_tokens: NonNegativeCount | None = None
    provider_total_tokens: NonNegativeCount | None = None
    cached_tokens: NonNegativeCount | None = None
    latency_ms: NonNegativeCount
    calculated_cost: NonNegativeDecimal | None = None
    pricing_provenance: PricingProvenance | None = None

    @model_validator(mode="after")
    def validate_usage_measurements(self) -> "ModelUsage":
        """Validate cached input, token-total consistency, and cost/provenance pair."""
        if (
            self.cached_tokens is not None
            and self.input_tokens is not None
            and self.cached_tokens > self.input_tokens
        ):
            raise ValueError("cached_tokens must not exceed input_tokens")
        if (
            self.input_tokens is not None
            and self.output_tokens is not None
            and self.provider_total_tokens is not None
            and self.provider_total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError(
                "provider_total_tokens must equal input_tokens plus output_tokens "
                "when all three measurements are reported"
            )
        if (self.calculated_cost is None) is not (self.pricing_provenance is None):
            raise ValueError(
                "calculated_cost and pricing_provenance must be provided together"
            )
        return self


class RunResult(ImmutableModel):
    """Final result and actual decision trace for one OPTIMA run."""

    run_id: NonEmptyString
    correlation_id: NonEmptyString
    created_at: datetime
    status: RunStatus
    quality_contract: QualityContract
    request_profile: RequestProfile
    request_binding: RequestBinding
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

    def _embedding_attempt(self) -> EmbeddingAttempt | None:
        """Return the embedding attempt evidence recorded for the cache lookup."""
        if self.semantic_cache is None:
            return None
        return self.semantic_cache.embedding_attempt

    def _embedding_usage(self) -> EmbeddingUsage | None:
        """Return the embedding usage consumed while resolving the cache lookup."""
        attempt = self._embedding_attempt()
        if attempt is None:
            return None
        return attempt.usage

    def _embedding_consumption_indeterminate(self) -> bool:
        """Return whether a possibly paid embedding left consumption unmeasured."""
        attempt = self._embedding_attempt()
        return attempt is not None and attempt.consumption_indeterminate

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_input_tokens(self) -> int | None:
        """Return exact input tokens only when every consumed call reports them.

        An embedding attempt that reached the provider without returning measured
        usage makes the input-token total indeterminate rather than model-only.
        """
        if self._embedding_consumption_indeterminate():
            return None
        usages = self._complete_model_usages()
        if usages is None or any(usage.input_tokens is None for usage in usages):
            return None
        total = sum(
            usage.input_tokens for usage in usages if usage.input_tokens is not None
        )
        embedding = self._embedding_usage()
        if embedding is None:
            return total
        if embedding.input_tokens is None:
            return None
        return total + embedding.input_tokens

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_output_tokens(self) -> int | None:
        """Return exact output tokens; embedding requests emit no output tokens.

        Output totals stay exact even when an embedding attempt is indeterminate:
        embeddings never produce output tokens, so an unmeasured embedding cannot
        change this total.
        """
        usages = self._complete_model_usages()
        if usages is None or any(usage.output_tokens is None for usage in usages):
            return None
        return sum(
            usage.output_tokens for usage in usages if usage.output_tokens is not None
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tokens(self) -> int | None:
        """Return complete run tokens including any embedding-lookup consumption.

        An embedding attempt that reached the provider without returning measured
        usage makes the combined total indeterminate rather than model-only.
        """
        if self._embedding_consumption_indeterminate():
            return None
        usages = self._complete_model_usages()
        if usages is None:
            return None
        total = 0
        for usage in usages:
            if usage.provider_total_tokens is not None:
                total += usage.provider_total_tokens
            elif usage.input_tokens is not None and usage.output_tokens is not None:
                total += usage.input_tokens + usage.output_tokens
            else:
                return None
        embedding = self._embedding_usage()
        if embedding is None:
            return total
        if embedding.input_tokens is None:
            return None
        return total + embedding.input_tokens

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_calculated_cost(self) -> Decimal | None:
        """Sum exact Decimal costs across model and embedding consumption.

        An embedding attempt that reached the provider without returning measured
        usage makes the run cost indeterminate rather than model-only.
        """
        if self._embedding_consumption_indeterminate():
            return None
        usages = self._complete_model_usages()
        if usages is None:
            return None
        total = Decimal("0")
        priced_any = False
        for usage in usages:
            if usage.calculated_cost is None:
                return None
            total += usage.calculated_cost
            priced_any = True
        embedding = self._embedding_usage()
        if embedding is not None:
            if embedding.calculated_cost is None:
                return None
            total += embedding.calculated_cost
            priced_any = True
        if not priced_any:
            return None
        return total

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_cost_provenance(self) -> PricingProvenance | None:
        """Return provenance only for one complete, single-provenance run total."""
        if self.total_calculated_cost is None:
            return None
        usages = self._complete_model_usages()
        if usages is None:
            return None
        provenance: PricingProvenance | None = None
        for usage in usages:
            if provenance is None:
                provenance = usage.pricing_provenance
            elif usage.pricing_provenance != provenance:
                return None
        embedding = self._embedding_usage()
        if embedding is not None:
            if provenance is None:
                provenance = embedding.pricing_provenance
            elif embedding.pricing_provenance != provenance:
                return None
        return provenance

    @model_validator(mode="after")
    def validate_actual_run_facts(self) -> "RunResult":
        """Enforce trace, measurement, escalation, and terminal-state consistency."""
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")

        if self.request_binding is not None and (
            self.request_binding.task_type is not self.request_profile.task_type
            or self.request_binding.complexity is not self.request_profile.complexity
        ):
            raise ValueError("request binding must match request profile facts")
        if self.execution_plan.request_binding != self.request_binding:
            raise ValueError("execution plan request binding must match the run result")
        if (
            self.execution_plan.quality_profile
            is not self.quality_contract.quality_profile
        ):
            raise ValueError("execution plan must match the run Quality Contract")
        if (
            self.execution_plan.optimization_mode
            is not self.quality_contract.optimization_mode
        ):
            raise ValueError("execution plan must match the run Optimization Mode")
        if (
            self.execution_plan.decision_evidence.profile_risk_tier
            is not self.request_profile.risk_tier
            or self.execution_plan.decision_evidence.contract_risk_tier
            is not self.quality_contract.risk_tier
        ):
            raise ValueError("execution plan risk evidence must match current facts")

        sequences = [step.sequence for step in self.steps]
        if sequences != list(range(len(self.steps))):
            raise ValueError(
                "execution-step sequences must be zero-based and contiguous"
            )

        cache_event_codes = {
            ExecutionEventCode.CACHE_RESULT_REUSED,
            ExecutionEventCode.CACHE_MISS,
            ExecutionEventCode.CACHE_MATCH_REJECTED,
            ExecutionEventCode.CACHE_LOOKUP_FAILED,
            ExecutionEventCode.CACHE_LOOKUP_TIMED_OUT,
        }
        if any(
            step.step_type is not ExecutionStepType.SEMANTIC_CACHE
            and cache_event_codes.intersection(step.event_codes)
            for step in self.steps
        ):
            raise ValueError(
                "cache event codes are allowed only on semantic-cache steps"
            )

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
        embedding_usage = self._embedding_usage()
        if embedding_usage is not None and embedding_usage.run_id != self.run_id:
            raise ValueError("embedding usage must belong to this run")
        if (
            embedding_usage is not None
            and embedding_usage.pricing_provenance is not None
        ):
            cost_provenances.add(
                (
                    embedding_usage.pricing_provenance.catalog_version,
                    embedding_usage.pricing_provenance.currency,
                )
            )
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
        if (
            self.status is RunStatus.COMPLETED
            and self.execution_plan.model_policy
            is ModelPolicy.SMALL_FIRST_WITH_FALLBACK
        ):
            expected_model_attempts = 1 + int(has_escalation_step)
            if attempted_model_calls != expected_model_attempts:
                raise ValueError(
                    "small-first runs require one initial model-call attempt and "
                    "one additional attempt after escalation"
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

        self._validate_model_trace()
        self._validate_successful_step_evidence()

        if self.status is not RunStatus.COMPLETED:
            expected_step_status = {
                RunStatus.FAILED: ExecutionStatus.FAILED,
                RunStatus.TIMED_OUT: ExecutionStatus.TIMED_OUT,
            }[self.status]
            if not self.steps or self.steps[-1].status is not expected_step_status:
                raise ValueError(
                    "interrupted run status must match the final execution step"
                )

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

    def _validate_successful_step_evidence(self) -> None:
        """Bind successful model and evaluation steps to recorded evidence."""
        model_steps = tuple(
            step
            for step in self.steps
            if step.step_type is ExecutionStepType.MODEL_CALL
            and step.status is ExecutionStatus.SUCCEEDED
        )
        if len(self.model_usages) < len(model_steps):
            raise ValueError("successful model steps require model usage evidence")
        for step, usage in zip(model_steps, self.model_usages, strict=False):
            if step.facts != {
                "model_role": usage.model_role.value,
                "provider": usage.provider,
                "deployment": usage.deployment,
                "request_id": usage.request_id,
            }:
                raise ValueError(
                    "model-call step facts must match model usage evidence"
                )

        evaluation_steps = tuple(
            step
            for step in self.steps
            if step.step_type is ExecutionStepType.QUALITY_EVALUATION
            and step.status is ExecutionStatus.SUCCEEDED
        )
        if len(evaluation_steps) != len(self.evaluations):
            raise ValueError(
                "successful evaluation steps must match evaluation evidence"
            )
        for step, evaluation in zip(
            evaluation_steps,
            self.evaluations,
            strict=True,
        ):
            role = step.facts.get("model_role")
            expected_events: tuple[ExecutionEventCode, ...] = ()
            if evaluation.passed:
                expected_events = (ExecutionEventCode.QUALITY_CONTRACT_MET,)
            else:
                if evaluation.evaluator_valid and (
                    evaluation.score < evaluation.threshold
                ):
                    expected_events += (ExecutionEventCode.QUALITY_THRESHOLD_NOT_MET,)
                if role == ModelRole.STRONG.value and evaluation.evaluator_valid:
                    expected_events += (
                        ExecutionEventCode.FINAL_QUALITY_CONTRACT_NOT_MET,
                    )
            if step.event_codes != expected_events or step.facts != {
                "model_role": role,
                "evaluator_type": evaluation.evaluator_type,
                "evaluator_valid": evaluation.evaluator_valid,
                "score": evaluation.score,
                "threshold": evaluation.threshold,
                "passed": evaluation.passed,
            }:
                raise ValueError(
                    "evaluation step facts and events must match evaluation evidence"
                )

    def _validate_model_trace(self) -> None:
        """Require the exact causal model/evaluation grammar emitted by V1."""
        policy = self.execution_plan.model_policy
        if policy is None:
            return

        prefix_length = 0
        if self.steps[0].step_type is ExecutionStepType.SEMANTIC_CACHE:
            prefix_length += 1
        if (
            prefix_length < len(self.steps)
            and self.steps[prefix_length].step_type
            is ExecutionStepType.CONTEXT_REDUCTION
        ):
            prefix_length += 1
        trace = self.steps[prefix_length:]
        if not trace or trace[0].step_type is not ExecutionStepType.MODEL_CALL:
            if trace and trace[0].step_type is ExecutionStepType.ESCALATION:
                raise ValueError("escalation cannot precede the initial model call")
            raise ValueError("model execution must begin with a model call")

        if policy is ModelPolicy.STRONG_DIRECT:
            self._validate_candidate_attempt(trace, ModelRole.STRONG)
            if any(
                usage.model_role is not ModelRole.STRONG for usage in self.model_usages
            ):
                raise ValueError("strong-direct usage must identify STRONG")
            return

        escalation_indexes = [
            index
            for index, step in enumerate(trace)
            if step.step_type is ExecutionStepType.ESCALATION
        ]
        expected_usage_roles: tuple[ModelRole, ...]
        if self.escalated:
            if escalation_indexes != [2]:
                raise ValueError(
                    "small-first escalation must follow the SMALL evaluation exactly"
                )
            small_attempt = trace[:2]
            if len(small_attempt) != 2:
                raise ValueError("escalation requires one complete SMALL attempt")
            self._validate_candidate_attempt(
                small_attempt,
                ModelRole.SMALL,
                terminal_required=False,
            )
            if not self.evaluations or self.evaluations[0].passed:
                raise ValueError("escalation requires an unsuccessful SMALL evaluation")
            self._validate_escalation_step(trace[2])
            self._validate_candidate_attempt(trace[3:], ModelRole.STRONG)
            expected_usage_roles = (ModelRole.SMALL, ModelRole.STRONG)
        else:
            if escalation_indexes:
                raise ValueError("non-escalated small-first traces cannot escalate")
            self._validate_candidate_attempt(trace, ModelRole.SMALL)
            if (
                self.status is RunStatus.COMPLETED
                and self.evaluations
                and not self.evaluations[0].passed
            ):
                raise ValueError(
                    "unsuccessful SMALL evaluation requires STRONG escalation"
                )
            expected_usage_roles = (ModelRole.SMALL,)

        if any(
            usage.model_role is not expected_usage_roles[index]
            for index, usage in enumerate(self.model_usages)
            if index < len(expected_usage_roles)
        ) or len(self.model_usages) > len(expected_usage_roles):
            raise ValueError("small-first model usage must follow SMALL then STRONG")

    def _validate_candidate_attempt(
        self,
        trace: tuple[ExecutionStep, ...],
        role: ModelRole,
        *,
        terminal_required: bool = True,
    ) -> None:
        """Validate one causally ordered model, evaluation, and return attempt."""
        if not trace or trace[0].step_type is not ExecutionStepType.MODEL_CALL:
            raise ValueError(f"{role.value} attempt must begin with a model call")
        model_step = trace[0]
        if model_step.facts.get("model_role") != role.value:
            raise ValueError(f"model-call facts must identify {role.value}")
        if model_step.status is not ExecutionStatus.SUCCEEDED:
            if len(trace) != 1:
                raise ValueError("evaluation requires a successful model call")
            return
        if (
            len(trace) < 2
            or trace[1].step_type is not ExecutionStepType.QUALITY_EVALUATION
        ):
            raise ValueError("successful model calls require immediate evaluation")
        evaluation_step = trace[1]
        if evaluation_step.facts.get("model_role") != role.value:
            raise ValueError(f"evaluation facts must identify {role.value}")
        if not terminal_required:
            if (
                len(trace) != 2
                or evaluation_step.status is not ExecutionStatus.SUCCEEDED
            ):
                raise ValueError("escalation requires one successful SMALL evaluation")
            return
        if evaluation_step.status is not ExecutionStatus.SUCCEEDED:
            if len(trace) != 2:
                raise ValueError("return requires a successful evaluation")
            return
        if len(trace) != 3 or trace[2].step_type is not ExecutionStepType.RETURN:
            raise ValueError("successful evaluation requires one terminal return")
        terminal = trace[2]
        if terminal.status is ExecutionStatus.FAILED:
            if (
                self.status is not RunStatus.FAILED
                or self.final_evaluation is None
                or self.final_evaluation.evaluator_valid
                or terminal.event_codes
                or terminal.facts != {"model_role": role.value}
                or terminal.error != self.error
            ):
                raise ValueError(
                    "failed terminal return requires invalid final evaluation evidence"
                )
            return
        if terminal.status is not ExecutionStatus.SUCCEEDED or terminal.event_codes:
            raise ValueError("terminal return must have a valid terminal status")
        if self.status is not RunStatus.COMPLETED:
            raise ValueError("terminal return requires a completed run")
        if self.final_evaluation is None:
            raise ValueError("terminal return requires final evaluation evidence")
        expected_facts = {
            "model_role": role.value,
            "contract_met": self.final_evaluation.passed,
        }
        if terminal.facts != expected_facts:
            raise ValueError("return facts must match final evaluation evidence")

    @staticmethod
    def _validate_escalation_step(step: ExecutionStep) -> None:
        """Validate the exact transition emitted between SMALL and STRONG."""
        if (
            step.status is not ExecutionStatus.SUCCEEDED
            or step.event_codes
            != (
                ExecutionEventCode.ESCALATION_REQUIRED,
                ExecutionEventCode.ESCALATED_TO_STRONG,
            )
            or step.facts
            != {
                "from_model_role": ModelRole.SMALL.value,
                "to_model_role": ModelRole.STRONG.value,
            }
        ):
            raise ValueError(
                "escalation evidence must match SMALL-to-STRONG transition"
            )

    def _measured_contract_met(self, *, is_cache_hit: bool) -> bool | None:
        """Derive compliance from current evaluation or accepted source evidence."""
        cache_steps = tuple(
            step
            for step in self.steps
            if step.step_type is ExecutionStepType.SEMANTIC_CACHE
        )
        cache_contract = validate_semantic_cache_binding(
            plan=self.execution_plan,
            cache_eligible=self.request_profile.cache_eligible,
            evidence=self.semantic_cache,
            run_id=self.run_id,
            minimum_quality_score=self.quality_contract.minimum_quality_score,
            request_binding=self.request_binding,
        )
        if cache_contract is None:
            if cache_steps:
                raise ValueError("cache steps require top-level cache evidence")
        elif cache_contract.step_status is None:
            if cache_steps:
                raise ValueError("cache bypass cannot record a lookup step")
        elif len(cache_steps) != 1 or cache_steps[0] is not self.steps[0]:
            raise ValueError("attempted lookup requires one leading cache step")
        elif cache_steps[0].semantic_cache != self.semantic_cache:
            raise ValueError("cache step must match top-level cache evidence")
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
            if (
                len(self.steps) != 2
                or self.steps[1].step_type is not ExecutionStepType.RETURN
            ):
                raise ValueError("cache runs require exactly cache and return steps")
            if self.steps[1].status is not ExecutionStatus.SUCCEEDED:
                raise ValueError("cache runs require a successful return step")
            expected_return_facts = {
                "contract_met": True,
                "source_run_id": candidate.source_run_id,
            }
            if (
                self.steps[1].event_codes
                or self.steps[1].facts != expected_return_facts
            ):
                raise ValueError("cache return evidence must match the bound candidate")
            if (
                self.status is not RunStatus.COMPLETED
                or self.contract_met is not True
                or self.escalated
            ):
                raise ValueError("cache runs require completed terminal facts")
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
            if evidence.lookup_latency_ms > self.latency_ms:
                raise ValueError("cache lookup latency cannot exceed total latency")
            return True
        return (
            self.final_evaluation.passed
            if self.final_evaluation is not None
            and self.final_evaluation.evaluator_valid
            else None
        )

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
