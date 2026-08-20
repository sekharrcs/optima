"""Plan-honoring runtime orchestration for Planner V1 model execution."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter

from optima.context import (
    ContextReducer,
    ContextReductionRequest,
    ContextReductionResult,
    TokenCounter,
)
from optima.cost import CostCalculator
from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    ContextReductionEvidence,
    ContextReductionOutcome,
    ContextSource,
    ExecutionEventCode,
    ExecutionStatus,
    ExecutionStep,
    ExecutionStepType,
    ModelPolicy,
    ModelRole,
)
from optima.domain.run import ModelUsage, RunResult, RunStatus
from optima.evaluation import EvaluationRequest, QualityEvaluator
from optima.execution.contracts import (
    ContextReductionDependencyError,
    ExecutionRequest,
    UnsupportedExecutionPlanError,
)
from optima.providers import (
    ModelProvider,
    ModelProviderRequest,
    ModelProviderResult,
    MonotonicClock,
)


def system_utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class SystemMonotonicClock:
    """Default monotonic clock for executor elapsed-time measurements."""

    def now(self) -> float:
        """Return the current monotonic timestamp in seconds."""
        return perf_counter()


class PlanExecutor:
    """Execute an existing Planner V1 model plan without making routing policy."""

    def __init__(
        self,
        *,
        small_provider: ModelProvider,
        strong_provider: ModelProvider,
        evaluator: QualityEvaluator,
        cost_calculator: CostCalculator,
        context_reducer: ContextReducer | None = None,
        token_counter: TokenCounter | None = None,
        monotonic_clock: MonotonicClock | None = None,
        utc_now: Callable[[], datetime] = system_utc_now,
    ) -> None:
        if small_provider.model_role is not ModelRole.SMALL:
            raise ValueError("small_provider must implement the SMALL role")
        if strong_provider.model_role is not ModelRole.STRONG:
            raise ValueError("strong_provider must implement the STRONG role")
        self._small_provider = small_provider
        self._strong_provider = strong_provider
        self._evaluator = evaluator
        self._cost_calculator = cost_calculator
        self._context_reducer = context_reducer
        self._token_counter = token_counter
        self._clock = monotonic_clock or SystemMonotonicClock()
        self._utc_now = utc_now

    async def execute(self, request: ExecutionRequest) -> RunResult:
        """Execute the selected model policy and its mandatory verification."""
        self._validate_supported_plan(request)
        started_at = self._clock.now()
        created_at = self._utc_now()
        if created_at.utcoffset() is None:
            raise ValueError("utc_now must return a timezone-aware datetime")

        steps: list[ExecutionStep] = []
        usages: list[ModelUsage] = []
        evaluations: list[EvaluationResult] = []

        effective_context, context_source = await self._prepare_context(
            request=request,
            steps=steps,
        )

        if request.execution_plan.model_policy is ModelPolicy.STRONG_DIRECT:
            strong_attempt = await self._attempt_candidate(
                request=request,
                provider=self._strong_provider,
                role=ModelRole.STRONG,
                effective_context=effective_context,
                context_source=context_source,
                steps=steps,
                usages=usages,
                evaluations=evaluations,
            )
            if isinstance(strong_attempt, RunStatus):
                return self._interrupted_result(
                    request=request,
                    created_at=created_at,
                    started_at=started_at,
                    status=strong_attempt,
                    steps=steps,
                    usages=usages,
                    evaluations=evaluations,
                    error=steps[-1].error
                    or "Strong candidate attempt did not complete",
                )
            strong_result, strong_evaluation = strong_attempt
            return self._finalize_candidate(
                request=request,
                created_at=created_at,
                started_at=started_at,
                steps=steps,
                usages=usages,
                evaluations=evaluations,
                result=strong_result,
                evaluation=strong_evaluation,
                role=ModelRole.STRONG,
                escalated=False,
            )

        small_attempt = await self._attempt_candidate(
            request=request,
            provider=self._small_provider,
            role=ModelRole.SMALL,
            effective_context=effective_context,
            context_source=context_source,
            steps=steps,
            usages=usages,
            evaluations=evaluations,
        )
        if isinstance(small_attempt, RunStatus):
            return self._interrupted_result(
                request=request,
                created_at=created_at,
                started_at=started_at,
                status=small_attempt,
                steps=steps,
                usages=usages,
                evaluations=evaluations,
                error=steps[-1].error or "Small candidate attempt did not complete",
            )
        small_result, small_evaluation = small_attempt
        if small_evaluation.passed:
            steps.append(
                self._return_step(
                    sequence=len(steps),
                    role=ModelRole.SMALL,
                    contract_met=True,
                )
            )
            return self._completed_result(
                request=request,
                created_at=created_at,
                started_at=started_at,
                steps=steps,
                usages=usages,
                evaluations=evaluations,
                final_output=small_result.output_text,
                final_evaluation=small_evaluation,
                escalated=False,
            )

        steps.append(
            ExecutionStep(
                sequence=len(steps),
                step_type=ExecutionStepType.ESCALATION,
                status=ExecutionStatus.SUCCEEDED,
                latency_ms=0,
                event_codes=(
                    ExecutionEventCode.ESCALATION_REQUIRED,
                    ExecutionEventCode.ESCALATED_TO_STRONG,
                ),
                facts={
                    "from_model_role": ModelRole.SMALL.value,
                    "to_model_role": ModelRole.STRONG.value,
                },
            )
        )
        strong_attempt = await self._attempt_candidate(
            request=request,
            provider=self._strong_provider,
            role=ModelRole.STRONG,
            effective_context=effective_context,
            context_source=context_source,
            steps=steps,
            usages=usages,
            evaluations=evaluations,
        )
        if isinstance(strong_attempt, RunStatus):
            return self._interrupted_result(
                request=request,
                created_at=created_at,
                started_at=started_at,
                status=strong_attempt,
                steps=steps,
                usages=usages,
                evaluations=evaluations,
                error=steps[-1].error or "Strong candidate attempt did not complete",
            )
        strong_result, strong_evaluation = strong_attempt
        return self._finalize_candidate(
            request=request,
            created_at=created_at,
            started_at=started_at,
            steps=steps,
            usages=usages,
            evaluations=evaluations,
            result=strong_result,
            evaluation=strong_evaluation,
            role=ModelRole.STRONG,
            escalated=True,
        )

    async def _attempt_candidate(
        self,
        *,
        request: ExecutionRequest,
        provider: ModelProvider,
        role: ModelRole,
        effective_context: str | None,
        context_source: ContextSource,
        steps: list[ExecutionStep],
        usages: list[ModelUsage],
        evaluations: list[EvaluationResult],
    ) -> tuple[ModelProviderResult, EvaluationResult] | RunStatus:
        """Call and evaluate one model role while preserving completed evidence."""
        result = await self._call_provider(
            request=request,
            provider=provider,
            role=role,
            effective_context=effective_context,
            context_source=context_source,
            steps=steps,
        )
        if isinstance(result, RunStatus):
            return result
        usages.append(result.usage)

        evaluation = await self._evaluate_candidate(
            request=request,
            output_text=result.output_text,
            role=role,
            steps=steps,
        )
        if isinstance(evaluation, RunStatus):
            return evaluation
        evaluations.append(evaluation)
        return result, evaluation

    def _finalize_candidate(
        self,
        *,
        request: ExecutionRequest,
        created_at: datetime,
        started_at: float,
        steps: list[ExecutionStep],
        usages: list[ModelUsage],
        evaluations: list[EvaluationResult],
        result: ModelProviderResult,
        evaluation: EvaluationResult,
        role: ModelRole,
        escalated: bool,
    ) -> RunResult:
        """Return a valid final candidate or fail closed on invalid evidence."""
        if not evaluation.evaluator_valid:
            steps.append(
                ExecutionStep(
                    sequence=len(steps),
                    step_type=ExecutionStepType.RETURN,
                    status=ExecutionStatus.FAILED,
                    latency_ms=0,
                    facts={"model_role": role.value},
                    error="Final evaluation evidence is invalid",
                )
            )
            return self._interrupted_result(
                request=request,
                created_at=created_at,
                started_at=started_at,
                status=RunStatus.FAILED,
                steps=steps,
                usages=usages,
                evaluations=evaluations,
                final_evaluation=evaluation,
                error="Final evaluation evidence is invalid",
            )

        steps.append(
            self._return_step(
                sequence=len(steps),
                role=role,
                contract_met=evaluation.passed,
            )
        )
        return self._completed_result(
            request=request,
            created_at=created_at,
            started_at=started_at,
            steps=steps,
            usages=usages,
            evaluations=evaluations,
            final_output=result.output_text,
            final_evaluation=evaluation,
            escalated=escalated,
        )

    async def _call_provider(
        self,
        *,
        request: ExecutionRequest,
        provider: ModelProvider,
        role: ModelRole,
        effective_context: str | None,
        context_source: ContextSource,
        steps: list[ExecutionStep],
    ) -> ModelProviderResult | RunStatus:
        started_at = self._clock.now()
        try:
            result = await provider.generate(
                ModelProviderRequest(
                    run_id=request.run_id,
                    model_role=role,
                    input_text=request.input_text,
                    context=effective_context,
                    metadata={
                        "task_type": request.request_profile.task_type.value,
                        "complexity": request.request_profile.complexity.value,
                    },
                )
            )
            if result.usage.run_id != request.run_id:
                raise ValueError("provider usage run_id does not match the request")
            if result.usage.model_role is not role:
                raise ValueError("provider usage role does not match the request")
            if result.usage.provider != provider.provider_name:
                raise ValueError("provider usage identity does not match the provider")
            if result.usage.deployment != provider.deployment_name:
                raise ValueError(
                    "provider usage deployment does not match the provider"
                )
            result = self._with_authoritative_cost(result)
        except TimeoutError as error:
            self._append_failed_step(
                steps=steps,
                step_type=ExecutionStepType.MODEL_CALL,
                status=ExecutionStatus.TIMED_OUT,
                started_at=started_at,
                role=role,
                error=error,
                context_source=context_source,
            )
            return RunStatus.TIMED_OUT
        except Exception as error:
            self._append_failed_step(
                steps=steps,
                step_type=ExecutionStepType.MODEL_CALL,
                status=ExecutionStatus.FAILED,
                started_at=started_at,
                role=role,
                error=error,
                context_source=context_source,
            )
            return RunStatus.FAILED

        steps.append(
            ExecutionStep(
                sequence=len(steps),
                step_type=ExecutionStepType.MODEL_CALL,
                status=ExecutionStatus.SUCCEEDED,
                latency_ms=self._elapsed_ms(started_at),
                facts={
                    "model_role": role.value,
                    "provider": result.usage.provider,
                    "deployment": result.usage.deployment,
                    "request_id": result.usage.request_id,
                },
                context_source=context_source,
            )
        )
        return result

    async def _prepare_context(
        self,
        *,
        request: ExecutionRequest,
        steps: list[ExecutionStep],
    ) -> tuple[str | None, ContextSource]:
        """Apply selected reduction once or recover explicitly with original context."""
        if request.execution_plan.context_policy is ContextPolicy.KEEP_ORIGINAL:
            return request.context, ContextSource.ORIGINAL
        if self._context_reducer is None or self._token_counter is None:
            raise ContextReductionDependencyError(
                "REDUCE plan requires a configured context reducer and token counter"
            )
        if request.context is None:
            raise ContextReductionDependencyError(
                "REDUCE plan requires original context"
            )

        original_count = self._token_counter.count(request.context)
        started_at = self._clock.now()
        try:
            raw_result = await self._context_reducer.reduce(
                ContextReductionRequest(
                    run_id=request.run_id,
                    input_text=request.input_text,
                    context=request.context,
                )
            )
            result = ContextReductionResult.model_validate(
                raw_result.model_dump(mode="python")
            )
            reduced_count = self._token_counter.count(result.reduced_context)
            if result.reducer_name != self._context_reducer.reducer_name:
                raise ValueError("reducer result name does not match dependency")
            if result.token_counter_name != self._token_counter.counter_name:
                raise ValueError(
                    "reducer result token counter does not match dependency"
                )
            if result.original_token_count != original_count:
                raise ValueError("reported original token count disagrees with counter")
            if result.reduced_token_count != reduced_count:
                raise ValueError("reported reduced token count disagrees with counter")
            if reduced_count >= original_count:
                raise ValueError("reducer output did not reduce measured tokens")
        except TimeoutError as error:
            self._append_reduction_fallback(
                steps=steps,
                status=ExecutionStatus.TIMED_OUT,
                started_at=started_at,
                original_token_count=original_count,
                error=error,
            )
            return request.context, ContextSource.ORIGINAL
        except Exception as error:
            self._append_reduction_fallback(
                steps=steps,
                status=ExecutionStatus.FAILED,
                started_at=started_at,
                original_token_count=original_count,
                error=error,
            )
            return request.context, ContextSource.ORIGINAL

        steps.append(
            ExecutionStep(
                sequence=len(steps),
                step_type=ExecutionStepType.CONTEXT_REDUCTION,
                status=ExecutionStatus.SUCCEEDED,
                latency_ms=self._elapsed_ms(started_at),
                context_reduction=ContextReductionEvidence(
                    outcome=ContextReductionOutcome.APPLIED,
                    original_token_count=original_count,
                    effective_token_count=reduced_count,
                    reducer_name=result.reducer_name,
                    method=result.method,
                    token_counter_name=result.token_counter_name,
                    context_source=ContextSource.REDUCED,
                    preservation=result.preservation,
                ),
            )
        )
        return result.reduced_context, ContextSource.REDUCED

    def _append_reduction_fallback(
        self,
        *,
        steps: list[ExecutionStep],
        status: ExecutionStatus,
        started_at: float,
        original_token_count: int,
        error: Exception,
    ) -> None:
        """Record a failed optional reduction and explicit original-context recovery."""
        if self._context_reducer is None or self._token_counter is None:
            raise AssertionError("reduction fallback requires configured dependencies")
        steps.append(
            ExecutionStep(
                sequence=len(steps),
                step_type=ExecutionStepType.CONTEXT_REDUCTION,
                status=status,
                latency_ms=self._elapsed_ms(started_at),
                context_reduction=ContextReductionEvidence(
                    outcome=ContextReductionOutcome.FAILED_USING_ORIGINAL,
                    original_token_count=original_token_count,
                    effective_token_count=original_token_count,
                    reducer_name=self._context_reducer.reducer_name,
                    token_counter_name=self._token_counter.counter_name,
                    context_source=ContextSource.ORIGINAL,
                ),
                error=f"Context reduction {type(error).__name__}",
            )
        )

    def _with_authoritative_cost(
        self,
        result: ModelProviderResult,
    ) -> ModelProviderResult:
        usage = result.usage
        calculation = self._cost_calculator.calculate(usage)
        priced_usage = ModelUsage(
            request_id=usage.request_id,
            run_id=usage.run_id,
            provider=usage.provider,
            deployment=usage.deployment,
            model_role=usage.model_role,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            latency_ms=usage.latency_ms,
            calculated_cost=(calculation.amount if calculation is not None else None),
            pricing_provenance=(
                calculation.provenance if calculation is not None else None
            ),
        )
        return ModelProviderResult(
            output_text=result.output_text,
            usage=priced_usage,
        )

    async def _evaluate_candidate(
        self,
        *,
        request: ExecutionRequest,
        output_text: str,
        role: ModelRole,
        steps: list[ExecutionStep],
    ) -> EvaluationResult | RunStatus:
        started_at = self._clock.now()
        try:
            result = await self._evaluator.evaluate(
                EvaluationRequest(
                    run_id=request.run_id,
                    input_text=request.input_text,
                    output_text=output_text,
                    context=request.context,
                    reference_output=request.reference_output,
                    criteria=request.criteria,
                    metadata={
                        **request.metadata,
                        "model_role": role.value,
                        "task_type": request.request_profile.task_type.value,
                        "complexity": request.request_profile.complexity.value,
                    },
                ),
                request.quality_contract,
            )
            if result.threshold != request.quality_contract.minimum_quality_score:
                raise ValueError(
                    "evaluation threshold does not match the Quality Contract"
                )
        except TimeoutError as error:
            self._append_failed_step(
                steps=steps,
                step_type=ExecutionStepType.QUALITY_EVALUATION,
                status=ExecutionStatus.TIMED_OUT,
                started_at=started_at,
                role=role,
                error=error,
            )
            return RunStatus.TIMED_OUT
        except Exception as error:
            self._append_failed_step(
                steps=steps,
                step_type=ExecutionStepType.QUALITY_EVALUATION,
                status=ExecutionStatus.FAILED,
                started_at=started_at,
                role=role,
                error=error,
            )
            return RunStatus.FAILED

        event_codes: tuple[ExecutionEventCode, ...] = ()
        if result.passed:
            event_codes = (ExecutionEventCode.QUALITY_CONTRACT_MET,)
        else:
            if result.evaluator_valid and result.score < result.threshold:
                event_codes += (ExecutionEventCode.QUALITY_THRESHOLD_NOT_MET,)
            if role is ModelRole.STRONG and result.evaluator_valid:
                event_codes += (ExecutionEventCode.FINAL_QUALITY_CONTRACT_NOT_MET,)
        steps.append(
            ExecutionStep(
                sequence=len(steps),
                step_type=ExecutionStepType.QUALITY_EVALUATION,
                status=ExecutionStatus.SUCCEEDED,
                latency_ms=self._elapsed_ms(started_at),
                event_codes=event_codes,
                facts={
                    "model_role": role.value,
                    "evaluator_type": result.evaluator_type,
                    "evaluator_valid": result.evaluator_valid,
                    "score": result.score,
                    "threshold": result.threshold,
                    "passed": result.passed,
                },
            )
        )
        return result

    def _validate_supported_plan(self, request: ExecutionRequest) -> None:
        plan = request.execution_plan
        common_model_plan = (
            plan.cache_policy is CachePolicy.SKIP
            and plan.context_policy
            in {ContextPolicy.KEEP_ORIGINAL, ContextPolicy.REDUCE}
            and plan.verification_required
        )
        small_first = (
            plan.model_policy is ModelPolicy.SMALL_FIRST_WITH_FALLBACK
            and plan.initial_model_role is ModelRole.SMALL
            and plan.escalation_model_role is ModelRole.STRONG
        )
        strong_direct = (
            plan.model_policy is ModelPolicy.STRONG_DIRECT
            and plan.initial_model_role is ModelRole.STRONG
            and plan.escalation_model_role is None
        )
        if not (common_model_plan and (small_first or strong_direct)):
            raise UnsupportedExecutionPlanError(
                "Plan executor supports Planner V1 model execution plans only"
            )

    def _append_failed_step(
        self,
        *,
        steps: list[ExecutionStep],
        step_type: ExecutionStepType,
        status: ExecutionStatus,
        started_at: float,
        role: ModelRole,
        error: Exception,
        context_source: ContextSource | None = None,
    ) -> None:
        operation = step_type.value.lower().replace("_", " ")
        steps.append(
            ExecutionStep(
                sequence=len(steps),
                step_type=step_type,
                status=status,
                latency_ms=self._elapsed_ms(started_at),
                facts={"model_role": role.value},
                context_source=context_source,
                error=f"{role.value} {operation} {type(error).__name__}",
            )
        )

    def _return_step(
        self,
        *,
        sequence: int,
        role: ModelRole,
        contract_met: bool,
    ) -> ExecutionStep:
        return ExecutionStep(
            sequence=sequence,
            step_type=ExecutionStepType.RETURN,
            status=ExecutionStatus.SUCCEEDED,
            latency_ms=0,
            facts={"model_role": role.value, "contract_met": contract_met},
        )

    def _completed_result(
        self,
        *,
        request: ExecutionRequest,
        created_at: datetime,
        started_at: float,
        steps: list[ExecutionStep],
        usages: list[ModelUsage],
        evaluations: list[EvaluationResult],
        final_output: str,
        final_evaluation: EvaluationResult,
        escalated: bool,
    ) -> RunResult:
        return RunResult(
            run_id=request.run_id,
            correlation_id=request.correlation_id,
            created_at=created_at,
            status=RunStatus.COMPLETED,
            quality_contract=request.quality_contract,
            request_profile=request.request_profile,
            execution_plan=request.execution_plan,
            steps=tuple(steps),
            model_usages=tuple(usages),
            evaluations=tuple(evaluations),
            final_evaluation=final_evaluation,
            final_output=final_output,
            contract_met=final_evaluation.passed,
            escalated=escalated,
            latency_ms=self._elapsed_ms(started_at),
        )

    def _interrupted_result(
        self,
        *,
        request: ExecutionRequest,
        created_at: datetime,
        started_at: float,
        status: RunStatus,
        steps: list[ExecutionStep],
        usages: list[ModelUsage],
        evaluations: list[EvaluationResult],
        error: str,
        final_evaluation: EvaluationResult | None = None,
    ) -> RunResult:
        return RunResult(
            run_id=request.run_id,
            correlation_id=request.correlation_id,
            created_at=created_at,
            status=status,
            quality_contract=request.quality_contract,
            request_profile=request.request_profile,
            execution_plan=request.execution_plan,
            steps=tuple(steps),
            model_usages=tuple(usages),
            evaluations=tuple(evaluations),
            final_evaluation=final_evaluation,
            final_output=None,
            contract_met=None,
            escalated=any(
                step.step_type is ExecutionStepType.ESCALATION for step in steps
            ),
            latency_ms=self._elapsed_ms(started_at),
            error=error,
        )

    def _elapsed_ms(self, started_at: float) -> int:
        return max(0, int(round((self._clock.now() - started_at) * 1000)))
