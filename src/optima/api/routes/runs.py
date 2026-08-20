"""Versioned API route for planned OPTIMA execution."""

from typing import Never

from fastapi import APIRouter, HTTPException, status

from optima.api.dependencies import ExecutionDependencies
from optima.api.models import ApiError, RunRequest
from optima.cache import SemanticCacheLookupRequest
from optima.context.safety import ContextReducerSafetyRequest
from optima.domain.cache import CacheCandidate
from optima.domain.execution import (
    CachePolicy,
    ExecutionPlan,
    PlannerReasonCode,
    SemanticCacheEvidence,
    SemanticCacheOutcome,
)
from optima.domain.quality_contract import build_quality_contract
from optima.domain.run import RunResult
from optima.execution import (
    ContextReductionDependencyError,
    ExecutionRequest,
    PlanExecutor,
    SystemMonotonicClock,
    UnsupportedExecutionPlanError,
)
from optima.planner import (
    ContextReducerCapability,
    PlannerCapabilities,
    PlannerInput,
    PlanningFailure,
    select_plan,
)


def build_runs_router(
    dependencies: ExecutionDependencies | None,
) -> APIRouter:
    """Build a run router bound to one immutable dependency composition."""
    router = APIRouter()

    @router.post("/runs", response_model=RunResult)
    async def execute_run(run_request: RunRequest) -> RunResult:
        """Plan and execute one supported Planner V1 OPTIMA run."""
        if dependencies is None:
            _raise_api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                code="EXECUTION_NOT_CONFIGURED",
                message="Model providers and quality evaluator are not configured",
            )

        quality_contract = build_quality_contract(
            quality_profile=run_request.quality_profile,
            optimization_mode=run_request.optimization_mode,
            risk_tier=run_request.risk_tier,
            max_latency_ms=run_request.max_latency_ms,
            thresholds=dependencies.settings.quality_thresholds(),
        )
        run_id = dependencies.run_id_factory()
        correlation_id = dependencies.correlation_id_factory()
        clock = dependencies.monotonic_clock or SystemMonotonicClock()
        request_started_at = clock.now()
        cache_candidate: CacheCandidate | None = None
        cache_outcome: SemanticCacheOutcome
        cache_error: str | None = None
        cache_lookup_latency_ms = 0
        if not dependencies.settings.semantic_cache_enabled:
            cache_outcome = SemanticCacheOutcome.DISABLED_BYPASSED
        elif dependencies.semantic_cache is None:
            _raise_api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                code="SEMANTIC_CACHE_NOT_CONFIGURED",
                message=(
                    "Semantic cache is enabled but no runtime dependency is configured"
                ),
            )
        elif not run_request.request_profile.cache_eligible:
            cache_outcome = SemanticCacheOutcome.INELIGIBLE_BYPASSED
        else:
            lookup_started_at = clock.now()
            try:
                cache_candidate = await dependencies.semantic_cache.lookup(
                    SemanticCacheLookupRequest(
                        run_id=run_id,
                        input_text=run_request.input_text,
                        context=run_request.context,
                        quality_contract=quality_contract,
                        request_profile=run_request.request_profile,
                        metadata=run_request.metadata,
                    )
                )
                cache_outcome = (
                    SemanticCacheOutcome.MISS
                    if cache_candidate is None
                    else SemanticCacheOutcome.MATCH_REJECTED
                )
            except TimeoutError as error:
                cache_outcome = SemanticCacheOutcome.LOOKUP_TIMED_OUT
                cache_error = f"Semantic cache {type(error).__name__}"
            except Exception as error:
                cache_outcome = SemanticCacheOutcome.LOOKUP_FAILED
                cache_error = f"Semantic cache {type(error).__name__}"
            cache_lookup_latency_ms = _elapsed_ms(clock.now(), lookup_started_at)
        reducer_configured = (
            dependencies.context_reducer is not None
            and dependencies.token_counter is not None
            and run_request.context is not None
        )
        reducer_task_safe = False
        if (
            reducer_configured
            and dependencies.context_reducer_safety_policy is not None
        ):
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
        planner_result = select_plan(
            PlannerInput(
                request_profile=run_request.request_profile,
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
            )
        )
        if isinstance(planner_result, PlanningFailure):
            _raise_api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                code=planner_result.code.value,
                message=planner_result.message,
            )
        execution_plan = _require_execution_plan(planner_result)
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
                cache_candidate.prior_evaluation
                if cache_candidate is not None
                else None
            ),
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
            return await executor.execute(
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

    return router


def _cache_reason(execution_plan: ExecutionPlan) -> PlannerReasonCode:
    """Return the controlling Planner V1 semantic-cache reason."""
    cache_reasons = {
        PlannerReasonCode.SEMANTIC_CACHE_DISABLED,
        PlannerReasonCode.CACHE_REQUEST_NOT_ELIGIBLE,
        PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED,
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
