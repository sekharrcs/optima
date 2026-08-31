"""Versioned API routes for OPTIMA execution and run history."""

import asyncio
from collections.abc import Callable
from typing import Annotated, Never

from fastapi import APIRouter, HTTPException, Query, Response, status

from optima.api.dependencies import ExecutionDependencies
from optima.api.models import ApiError, RunRequest
from optima.api.security import ExecutionCapacityExceededError
from optima.cache import SemanticCacheLookupRequest
from optima.config import ProductionEvaluatorMode
from optima.context.safety import ContextReducerSafetyRequest
from optima.cost import CostCalculator
from optima.domain.cache import CacheCandidate
from optima.domain.embedding import EmbeddingAttempt
from optima.domain.execution import (
    CachePolicy,
    ExecutionPlan,
    PlannerReasonCode,
    SemanticCacheEvidence,
    SemanticCacheOutcome,
)
from optima.domain.quality_contract import build_quality_contract
from optima.domain.request_binding import build_request_binding
from optima.domain.run import RunResult
from optima.execution import (
    ContextReductionDependencyError,
    ExecutionRequest,
    PlanExecutor,
    SystemMonotonicClock,
    UnsupportedExecutionPlanError,
)
from optima.observability import (
    CacheLookupResult,
    CacheStageOutcome,
    FailureCategory,
    ObservationStage,
    ObservationStatus,
    PersistenceResult,
    PersistenceStageOutcome,
    PlannerStageOutcome,
    RunObservation,
    StageOutcome,
    plan_family,
)
from optima.planner import (
    ContextReducerCapability,
    PlannerCapabilities,
    PlannerInput,
    PlanningFailure,
    select_plan,
)
from optima.providers import MonotonicClock
from optima.storage import (
    RunHistoryError,
    RunHistoryErrorCode,
    RunHistoryInvalidDocumentError,
    RunHistoryNotFoundError,
    RunHistoryStore,
)

HistoryLimit = Annotated[int | None, Query(ge=1, le=100)]
ExecutionDependencyResolver = Callable[[], ExecutionDependencies | None]

RUN_HISTORY_OUTCOME_HEADER = "X-OPTIMA-Run-History"
RUN_HISTORY_ERROR_HEADER = "X-OPTIMA-Run-History-Error"


def _extract_embedding_attempt(error: BaseException) -> EmbeddingAttempt | None:
    """Recover the embedding attempt a cache failure carried, if any."""
    attempt = getattr(error, "embedding_attempt", None)
    return attempt if isinstance(attempt, EmbeddingAttempt) else None


def _price_embedding_attempt(
    attempt: EmbeddingAttempt | None,
    cost_calculator: CostCalculator,
) -> EmbeddingAttempt | None:
    """Apply the authoritative catalog cost to any measured embedding usage."""
    if attempt is None or attempt.usage is None:
        return attempt
    calculation = cost_calculator.calculate_embedding(attempt.usage)
    if calculation is None:
        return attempt
    priced_usage = attempt.usage.model_copy(
        update={
            "calculated_cost": calculation.amount,
            "pricing_provenance": calculation.provenance,
        }
    )
    return attempt.model_copy(update={"usage": priced_usage})


def build_runs_router(
    dependencies: ExecutionDependencies | None | ExecutionDependencyResolver,
) -> APIRouter:
    """Build a run router bound to one immutable dependency composition."""
    router = APIRouter()

    def resolve_dependencies() -> ExecutionDependencies | None:
        if callable(dependencies):
            return dependencies()
        return dependencies

    @router.post("/runs", response_model=RunResult)
    async def execute_run(run_request: RunRequest, response: Response) -> RunResult:
        """Plan and execute one supported Planner V1 OPTIMA run."""
        resolved = resolve_dependencies()
        if resolved is None:
            _raise_api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                code="EXECUTION_NOT_CONFIGURED",
                message="Model providers and quality evaluator are not configured",
            )
        if (
            resolved.settings.production_require_reference_output
            and run_request.reference_output is None
        ):
            _raise_api_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="REFERENCE_OUTPUT_REQUIRED",
                message="The configured production evaluator requires reference output",
            )
        if run_request.grounding_required and run_request.context is None:
            _raise_api_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="GROUNDING_CONTEXT_REQUIRED",
                message="The Quality Contract requires supplied grounding context",
            )
        if (
            resolved.settings.production_evaluator_mode
            is ProductionEvaluatorMode.EXACT_REFERENCE
            and run_request.grounding_required
        ):
            _raise_api_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="GROUNDING_NOT_SUPPORTED",
                message="EXACT_REFERENCE cannot establish grounding",
            )
        limiter = resolved.execution_limiter
        if limiter is None:
            raise AssertionError("application composition must resolve execution limit")
        try:
            with limiter.acquire():
                run_id = resolved.run_id_factory()
                correlation_id = resolved.correlation_id_factory()
                clock = resolved.monotonic_clock or SystemMonotonicClock()
                request_started_at = clock.now()
                observability = resolved.observability
                if observability is None:
                    raise AssertionError(
                        "application composition must resolve observability"
                    )
                with observability.start_run(
                    run_id=run_id,
                    correlation_id=correlation_id,
                ) as run_observation:
                    timeout_ms = min(
                        int(resolved.settings.execution_timeout_seconds * 1000),
                        run_request.max_latency_ms
                        or int(resolved.settings.execution_timeout_seconds * 1000),
                    )
                    try:
                        async with asyncio.timeout(timeout_ms / 1000):
                            result = await _execute_observed_run(
                                run_request=run_request,
                                response=response,
                                dependencies=resolved,
                                run_id=run_id,
                                correlation_id=correlation_id,
                                clock=clock,
                                request_started_at=request_started_at,
                                observation=run_observation,
                            )
                    except TimeoutError:
                        run_observation.record_pre_result_failure(
                            FailureCategory.TIMEOUT
                        )
                        _raise_api_error(
                            status.HTTP_504_GATEWAY_TIMEOUT,
                            code="EXECUTION_TIMEOUT",
                            message=(
                                "OPTIMA execution exceeded its server-side deadline"
                            ),
                            facts={"timeout_ms": timeout_ms},
                        )
                    except Exception as error:
                        run_observation.record_pre_result_failure(
                            _pre_result_failure_category(error)
                        )
                        raise
                    await _record_run_history_persistence(
                        response,
                        resolved.run_history_store,
                        result,
                        run_observation,
                        save_timeout_seconds=resolved.settings.cosmos_timeout_seconds,
                    )
                    run_observation.project_result(result)
                    return result
        except ExecutionCapacityExceededError:
            _raise_api_error(
                status.HTTP_429_TOO_MANY_REQUESTS,
                code="EXECUTION_CAPACITY_EXCEEDED",
                message="This OPTIMA instance is at its execution concurrency limit",
                facts={"maximum_concurrency": limiter.maximum_concurrency},
            )

    @router.get("/runs/{run_id}", response_model=RunResult)
    async def get_run(run_id: str) -> RunResult:
        """Return one validated persisted run by opaque identifier."""
        resolved = resolve_dependencies()
        store = resolved.run_history_store if resolved is not None else None
        if store is None:
            _raise_history_not_configured()
        try:
            return await store.get(run_id)
        except RunHistoryNotFoundError:
            _raise_api_error(
                status.HTTP_404_NOT_FOUND,
                code="RUN_NOT_FOUND",
                message="Run history entry was not found",
                facts={"run_id": run_id},
            )
        except RunHistoryError as error:
            _raise_history_read_error(error)
        except Exception:
            _raise_api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                code="RUN_HISTORY_SERVICE_UNAVAILABLE",
                message="Run history is unavailable",
            )

    @router.get("/runs", response_model=tuple[RunResult, ...])
    async def list_runs(limit: HistoryLimit = None) -> tuple[RunResult, ...]:
        """Return a strictly bounded newest-first run-history sequence."""
        resolved = resolve_dependencies()
        store = resolved.run_history_store if resolved is not None else None
        if store is None or resolved is None:
            _raise_history_not_configured()
        configured_limit = resolved.settings.cosmos_history_list_limit
        effective_limit = configured_limit if limit is None else limit
        if effective_limit > configured_limit:
            _raise_api_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="RUN_HISTORY_LIMIT_EXCEEDED",
                message="Requested run-history limit exceeds the configured maximum",
                facts={"maximum_limit": configured_limit},
            )
        try:
            return await store.list_recent(effective_limit)
        except RunHistoryError as error:
            _raise_history_read_error(error)
        except Exception:
            _raise_api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                code="RUN_HISTORY_SERVICE_UNAVAILABLE",
                message="Run history is unavailable",
            )

    return router


async def _execute_observed_run(
    *,
    run_request: RunRequest,
    response: Response,
    dependencies: ExecutionDependencies,
    run_id: str,
    correlation_id: str,
    clock: MonotonicClock,
    request_started_at: float,
    observation: RunObservation,
) -> RunResult:
    """Execute one run while emitting only bounded observational evidence."""
    with observation.start_stage(ObservationStage.QUALITY_CONTRACT_BUILD) as observed:
        try:
            quality_contract = build_quality_contract(
                quality_profile=run_request.quality_profile,
                optimization_mode=run_request.optimization_mode,
                risk_tier=run_request.risk_tier,
                grounding_required=run_request.grounding_required,
                max_latency_ms=run_request.max_latency_ms,
                thresholds=dependencies.settings.quality_thresholds(),
            )
            request_binding = build_request_binding(
                input_text=run_request.input_text,
                context=run_request.context,
                reference_output=run_request.reference_output,
                criteria=run_request.criteria,
                metadata=run_request.metadata,
                task_type=run_request.request_profile.task_type,
                complexity=run_request.request_profile.complexity,
            )
        except Exception:
            observed.finish(
                StageOutcome(
                    status=ObservationStatus.FAILED,
                    failure_category=FailureCategory.VALIDATION,
                )
            )
            raise
        observed.finish(StageOutcome(status=ObservationStatus.SUCCEEDED))

    cache_candidate: CacheCandidate | None = None
    cache_outcome: SemanticCacheOutcome
    cache_error: str | None = None
    cache_lookup_latency_ms = 0
    embedding_attempt: EmbeddingAttempt | None = None
    if not dependencies.settings.semantic_cache_enabled:
        cache_outcome = SemanticCacheOutcome.DISABLED_BYPASSED
    elif dependencies.semantic_cache is None:
        _raise_api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="SEMANTIC_CACHE_NOT_CONFIGURED",
            message="Semantic cache is enabled but no runtime dependency is configured",
        )
    elif not run_request.request_profile.cache_eligible:
        cache_outcome = SemanticCacheOutcome.INELIGIBLE_BYPASSED
    else:
        with observation.start_stage(
            ObservationStage.SEMANTIC_CACHE_LOOKUP
        ) as observed:
            lookup_started_at = clock.now()
            try:
                lookup_result = await dependencies.semantic_cache.lookup(
                    SemanticCacheLookupRequest(
                        run_id=run_id,
                        input_text=run_request.input_text,
                        context=run_request.context,
                        reference_output=run_request.reference_output,
                        criteria=run_request.criteria,
                        quality_contract=quality_contract,
                        request_profile=run_request.request_profile,
                        metadata=run_request.metadata,
                        request_binding=request_binding,
                    )
                )
                cache_candidate = (
                    CacheCandidate.model_validate(lookup_result.candidate)
                    if lookup_result.candidate is not None
                    else None
                )
                embedding_attempt = lookup_result.embedding_attempt
                cache_outcome = (
                    SemanticCacheOutcome.MISS
                    if cache_candidate is None
                    else SemanticCacheOutcome.MATCH_REJECTED
                )
                observed.finish(
                    CacheStageOutcome(
                        status=ObservationStatus.SUCCEEDED,
                        lookup_result=(
                            CacheLookupResult.MISS
                            if cache_candidate is None
                            else CacheLookupResult.CANDIDATE_FOUND
                        ),
                    )
                )
            except TimeoutError as error:
                cache_outcome = SemanticCacheOutcome.LOOKUP_TIMED_OUT
                cache_error = f"Semantic cache {type(error).__name__}"
                embedding_attempt = _extract_embedding_attempt(error)
                observed.finish(
                    CacheStageOutcome(
                        status=ObservationStatus.TIMED_OUT,
                        failure_category=FailureCategory.TIMEOUT,
                    )
                )
            except Exception as error:
                cache_outcome = SemanticCacheOutcome.LOOKUP_FAILED
                cache_error = f"Semantic cache {type(error).__name__}"
                embedding_attempt = _extract_embedding_attempt(error)
                observed.finish(
                    CacheStageOutcome(
                        status=ObservationStatus.FAILED,
                        failure_category=FailureCategory.CACHE,
                    )
                )
            cache_lookup_latency_ms = _elapsed_ms(clock.now(), lookup_started_at)
            embedding_attempt = _price_embedding_attempt(
                embedding_attempt, dependencies.cost_calculator
            )

    reducer_configured = (
        dependencies.context_reducer is not None
        and dependencies.token_counter is not None
        and run_request.context is not None
    )
    reducer_task_safe = False
    if reducer_configured and dependencies.context_reducer_safety_policy is not None:
        if run_request.context is None:
            raise AssertionError("configured reducer request must include context")
        safety_decision = dependencies.context_reducer_safety_policy.evaluate(
            ContextReducerSafetyRequest(
                input_text=run_request.input_text,
                context=run_request.context,
                task_type=run_request.request_profile.task_type,
                complexity=run_request.request_profile.complexity,
            )
        )
        reducer_task_safe = safety_decision.task_safe

    with observation.start_stage(ObservationStage.PLANNER_SELECT) as observed:
        try:
            planner_result = select_plan(
                PlannerInput(
                    request_profile=run_request.request_profile,
                    request_binding=request_binding,
                    quality_contract=quality_contract,
                    modules=dependencies.settings.module_configuration(),
                    thresholds=dependencies.settings.planner_thresholds(),
                    reducer_capability=ContextReducerCapability(
                        available=reducer_configured,
                        task_safe=reducer_task_safe,
                        approved_for_critical_high_risk=False,
                    ),
                    capabilities=PlannerCapabilities(
                        small_model_configured=True,
                        strong_model_configured=True,
                        evaluator_configured=True,
                    ),
                    cache_candidate=cache_candidate,
                    current_evaluator_identity=dependencies.evaluator.evaluator_identity,
                )
            )
            if isinstance(planner_result, PlanningFailure):
                observed.finish(
                    StageOutcome(
                        status=ObservationStatus.FAILED,
                        failure_category=FailureCategory.PLANNING,
                    )
                )
                _raise_api_error(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    code=planner_result.code.value,
                    message=planner_result.message,
                )
            execution_plan = _require_execution_plan(planner_result)
            observed.finish(
                PlannerStageOutcome(
                    status=ObservationStatus.SUCCEEDED,
                    plan_family=plan_family(
                        cache_policy=execution_plan.cache_policy,
                        model_policy=execution_plan.model_policy,
                    ),
                    cache_policy=execution_plan.cache_policy,
                    context_policy=execution_plan.context_policy,
                    model_policy=execution_plan.model_policy,
                )
            )
        except HTTPException:
            raise
        except Exception:
            observed.finish(
                StageOutcome(
                    status=ObservationStatus.FAILED,
                    failure_category=FailureCategory.PLANNING,
                )
            )
            raise

    cache_reason = _cache_reason(execution_plan)
    if execution_plan.cache_policy is CachePolicy.USE_CACHED_RESULT:
        cache_outcome = SemanticCacheOutcome.REUSED
    cache_evidence = SemanticCacheEvidence(
        outcome=cache_outcome,
        lookup_latency_ms=cache_lookup_latency_ms,
        planner_reason_code=cache_reason,
        source_run_id=(
            cache_candidate.source_run_id if cache_candidate is not None else None
        ),
        similarity=(
            cache_candidate.similarity if cache_candidate is not None else None
        ),
        prior_evaluation=(
            cache_candidate.prior_evaluation if cache_candidate is not None else None
        ),
        candidate_assessment=execution_plan.cache_candidate_assessment,
        embedding_attempt=embedding_attempt,
        error=cache_error,
    )

    executor = PlanExecutor(
        small_provider=dependencies.small_provider,
        strong_provider=dependencies.strong_provider,
        evaluator=dependencies.evaluator,
        cost_calculator=dependencies.cost_calculator,
        context_reducer=dependencies.context_reducer,
        token_counter=dependencies.token_counter,
        monotonic_clock=clock,
        utc_now=dependencies.utc_now,
    )
    try:
        result = await executor.execute(
            ExecutionRequest(
                run_id=run_id,
                correlation_id=correlation_id,
                input_text=run_request.input_text,
                context=run_request.context,
                reference_output=run_request.reference_output,
                criteria=run_request.criteria,
                metadata=run_request.metadata,
                quality_contract=quality_contract,
                request_profile=run_request.request_profile,
                execution_plan=execution_plan,
                semantic_cache=cache_evidence,
            ),
            started_at=request_started_at,
            observation=observation,
        )
    except UnsupportedExecutionPlanError as error:
        _raise_api_error(
            status.HTTP_501_NOT_IMPLEMENTED,
            code="UNSUPPORTED_EXECUTION_PLAN",
            message=str(error),
            facts={
                "model_policy": (
                    execution_plan.model_policy.value
                    if execution_plan.model_policy is not None
                    else None
                ),
                "context_policy": execution_plan.context_policy.value,
            },
        )
    except ContextReductionDependencyError as error:
        _raise_api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="CONTEXT_REDUCTION_NOT_CONFIGURED",
            message=str(error),
            facts={"context_policy": execution_plan.context_policy.value},
        )

    return result


def _cache_reason(execution_plan: ExecutionPlan) -> PlannerReasonCode:
    """Return the controlling Planner V1 semantic-cache reason."""
    cache_reasons = {
        PlannerReasonCode.SEMANTIC_CACHE_DISABLED,
        PlannerReasonCode.CACHE_REQUEST_NOT_ELIGIBLE,
        PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED,
        PlannerReasonCode.CACHE_REQUEST_BINDING_MISMATCH,
        PlannerReasonCode.CACHE_SIMILARITY_BELOW_THRESHOLD,
        PlannerReasonCode.CACHE_PRIOR_EVALUATOR_INVALID,
        PlannerReasonCode.CACHE_PRIOR_EVALUATION_FAILED,
        PlannerReasonCode.CACHE_QUALITY_BELOW_CONTRACT_THRESHOLD,
        PlannerReasonCode.CACHE_CONTRACT_INCOMPATIBLE,
        PlannerReasonCode.CACHE_REUSE_UNSAFE,
        PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
    }
    reason = next(
        (code for code in execution_plan.reason_codes if code in cache_reasons),
        None,
    )
    if reason is None:
        raise AssertionError("Planner V1 plan must contain one cache reason")
    return reason


def _elapsed_ms(ended_at: float, started_at: float) -> int:
    """Return one non-negative rounded elapsed duration."""
    return max(0, int(round((ended_at - started_at) * 1000)))


def _require_execution_plan(
    planner_result: ExecutionPlan | PlanningFailure,
) -> ExecutionPlan:
    """Narrow a planner result after the explicit failure branch."""
    if isinstance(planner_result, PlanningFailure):
        raise AssertionError("planning failures must be handled before execution")
    return planner_result


async def _record_run_history_persistence(
    response: Response,
    store: RunHistoryStore | None,
    result: RunResult,
    observation: RunObservation,
    *,
    save_timeout_seconds: float,
) -> None:
    """Report best-effort persistence via headers without altering the result."""
    if store is None:
        response.headers[RUN_HISTORY_OUTCOME_HEADER] = "NOT_CONFIGURED"
        return
    with observation.start_stage(ObservationStage.RUN_HISTORY_SAVE) as observed:
        error_code: RunHistoryErrorCode | None = None
        try:
            async with asyncio.timeout(save_timeout_seconds):
                await store.save(result)
        except TimeoutError:
            error_code = RunHistoryErrorCode.TIMED_OUT
        except RunHistoryError as error:
            error_code = error.code
        except Exception:
            error_code = RunHistoryErrorCode.SERVICE_UNAVAILABLE
        if error_code is None:
            response.headers[RUN_HISTORY_OUTCOME_HEADER] = "PERSISTED"
            observed.finish(
                PersistenceStageOutcome(
                    status=ObservationStatus.SUCCEEDED,
                    result=PersistenceResult.PERSISTED,
                )
            )
            return
        response.headers[RUN_HISTORY_OUTCOME_HEADER] = "FAILED"
        response.headers[RUN_HISTORY_ERROR_HEADER] = error_code.value
        observed.finish(
            PersistenceStageOutcome(
                status=(
                    ObservationStatus.TIMED_OUT
                    if error_code is RunHistoryErrorCode.TIMED_OUT
                    else ObservationStatus.FAILED
                ),
                result=PersistenceResult.FAILED,
                error_code=error_code,
                failure_category=(
                    FailureCategory.TIMEOUT
                    if error_code is RunHistoryErrorCode.TIMED_OUT
                    else FailureCategory.PERSISTENCE
                ),
            )
        )


def _pre_result_failure_category(error: Exception) -> FailureCategory:
    """Map one pre-result failure to a bounded non-sensitive category."""
    if isinstance(error, TimeoutError):
        return FailureCategory.TIMEOUT
    if isinstance(error, UnsupportedExecutionPlanError):
        return FailureCategory.UNSUPPORTED_PLAN
    if isinstance(error, ContextReductionDependencyError):
        return FailureCategory.CONFIGURATION
    if isinstance(error, HTTPException) and isinstance(error.detail, dict):
        code = error.detail.get("code")
        if code in {
            "SEMANTIC_CACHE_NOT_CONFIGURED",
            "CONTEXT_REDUCTION_NOT_CONFIGURED",
        }:
            return FailureCategory.CONFIGURATION
        if code == "UNSUPPORTED_EXECUTION_PLAN":
            return FailureCategory.UNSUPPORTED_PLAN
        if isinstance(code, str) and code.endswith("_NOT_CONFIGURED"):
            return FailureCategory.PLANNING
    return FailureCategory.VALIDATION


def _raise_history_not_configured() -> Never:
    """Return the stable cloud-free behavior for history reads."""
    _raise_api_error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        code="RUN_HISTORY_NOT_CONFIGURED",
        message="Run history is not configured",
    )


def _raise_history_read_error(error: RunHistoryError) -> Never:
    """Map sanitized storage errors to one structured read failure."""
    status_code = (
        status.HTTP_500_INTERNAL_SERVER_ERROR
        if isinstance(error, RunHistoryInvalidDocumentError)
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    _raise_api_error(
        status_code,
        code=error.code.value,
        message=str(error),
    )


def _raise_api_error(
    status_code: int,
    *,
    code: str,
    message: str,
    facts: dict[str, object] | None = None,
) -> Never:
    """Raise one stable structured HTTP error detail."""
    error = ApiError.model_validate(
        {"code": code, "message": message, "facts": facts or {}}
    )
    raise HTTPException(
        status_code=status_code,
        detail=error.model_dump(mode="json"),
    )
