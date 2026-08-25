"""OpenTelemetry adapter for the provider-independent OPTIMA boundary."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from decimal import Decimal
from threading import RLock
from time import monotonic
from types import TracebackType
from typing import Protocol, Self

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.metrics import Counter, Histogram, Meter
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from optima.domain.execution import (
    ExecutionStep,
    ExecutionStepType,
    ModelRole,
    SemanticCacheOutcome,
)
from optima.domain.run import RunResult, RunStatus
from optima.observability.contracts import (
    TELEMETRY_SCHEMA_VERSION,
    CacheStageOutcome,
    ContextStageOutcome,
    EvaluationStageOutcome,
    FailureCategory,
    ModelStageOutcome,
    ObservationStage,
    ObservationStatus,
    PersistenceStageOutcome,
    PlannerStageOutcome,
    StageOutcomeEvidence,
    plan_family,
)

_HTTP_METHODS = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
)
_PROVIDER_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_ROUTE_TEMPLATE = re.compile(r"/[A-Za-z0-9_{}./:-]{0,255}")
_ATTEMPTED_CACHE_OUTCOMES = frozenset(
    {
        SemanticCacheOutcome.MISS,
        SemanticCacheOutcome.MATCH_REJECTED,
        SemanticCacheOutcome.REUSED,
        SemanticCacheOutcome.LOOKUP_FAILED,
        SemanticCacheOutcome.LOOKUP_TIMED_OUT,
    }
)
_MAX_COST_FIXED_POINT_CHARS = 96


class FlushableProvider(Protocol):
    """OpenTelemetry provider lifecycle used without a concrete SDK type leak."""

    def force_flush(self, timeout_millis: int = 30_000) -> bool | None:
        """Flush pending telemetry within the supplied timeout."""
        ...

    def shutdown(self) -> None:
        """Shut down the provider."""
        ...


class PrivacySafeOpenTelemetryMiddleware:
    """Create one server span without collecting user-controlled HTTP data."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        tracer: Tracer,
        excluded_paths: frozenset[str],
        route_templates: tuple[str, ...],
    ) -> None:
        self._app = app
        self._tracer = tracer
        self._excluded_paths = excluded_paths
        self._route_templates = tuple(
            (template, _compile_route_template(template))
            for template in route_templates
            if _ROUTE_TEMPLATE.fullmatch(template)
        )
        self._propagator = TraceContextTextMapPropagator()

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http" or scope.get("path") in self._excluded_paths:
            await self._app(scope, receive, send)
            return

        method = _safe_http_method(scope.get("method"))
        carrier = _trace_context_carrier(scope)
        parent_context = self._propagator.extract(carrier)
        span = self._tracer.start_span(
            f"{method} unmatched",
            context=parent_context,
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method},
            record_exception=False,
            set_status_on_exception=False,
        )
        status_code: int | None = None

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                raw_status = message.get("status")
                if isinstance(raw_status, int):
                    status_code = raw_status
            await send(message)

        with trace.use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                await self._app(scope, receive, send_with_status)
            except Exception as error:
                span.set_attribute(
                    "optima.error.category",
                    _safe_exception_category(error),
                )
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                route_template = _safe_route_template(
                    scope,
                    self._route_templates,
                )
                if route_template is not None:
                    span.update_name(f"{method} {route_template}")
                    span.set_attribute("http.route", route_template)
                if status_code is not None:
                    span.set_attribute("http.response.status_code", status_code)
                    if status_code >= 500:
                        span.set_status(Status(StatusCode.ERROR))
                span.end()


class OpenTelemetryStageObservation:
    """One close-once OpenTelemetry child span."""

    def __init__(
        self,
        owner: OpenTelemetryObservability,
        stage: ObservationStage,
    ) -> None:
        self._owner = owner
        self._stage = stage
        self._span = owner.tracer.start_span(
            stage.value,
            attributes={
                "optima.telemetry.schema_version": TELEMETRY_SCHEMA_VERSION,
            },
            record_exception=False,
            set_status_on_exception=False,
        )
        self._scope: AbstractContextManager[Span] | None = None
        self._finished = False
        self._closed = False

    def __enter__(self) -> Self:
        if self._scope is None and not self._closed:
            self._scope = trace.use_span(
                self._span,
                end_on_exit=False,
                record_exception=False,
                set_status_on_exception=False,
            )
            self._scope.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        if self._scope is not None:
            self._scope.__exit__(None, None, None)
        self._span.end()

    def finish(self, outcome: StageOutcomeEvidence) -> None:
        if self._finished or self._closed:
            return
        self._finished = True
        self._span.set_attribute("optima.operation.status", outcome.status.value)
        if outcome.failure_category is not None:
            self._span.set_attribute(
                "optima.error.category",
                outcome.failure_category.value,
            )
        _set_operation_status(self._span, outcome.status)
        self._set_typed_attributes(outcome)
        if isinstance(outcome, PersistenceStageOutcome):
            self._owner.record_persistence(outcome)

    def _set_typed_attributes(self, outcome: StageOutcomeEvidence) -> None:
        if isinstance(outcome, CacheStageOutcome):
            if outcome.lookup_result is not None:
                self._span.set_attribute(
                    "optima.cache.lookup_result",
                    outcome.lookup_result.value,
                )
            return
        if isinstance(outcome, PlannerStageOutcome):
            if outcome.plan_family is not None:
                self._span.set_attribute(
                    "optima.plan.family",
                    outcome.plan_family.value,
                )
            if outcome.cache_policy is not None:
                self._span.set_attribute(
                    "optima.plan.cache_policy",
                    outcome.cache_policy.value,
                )
            if outcome.context_policy is not None:
                self._span.set_attribute(
                    "optima.plan.context_policy",
                    outcome.context_policy.value,
                )
            if outcome.model_policy is not None:
                self._span.set_attribute(
                    "optima.plan.model_policy",
                    outcome.model_policy.value,
                )
            return
        if isinstance(outcome, ContextStageOutcome):
            self._span.set_attribute(
                "optima.context_reduction.outcome",
                outcome.outcome.value,
            )
            self._span.set_attribute(
                "optima.context_reduction.original_tokens",
                outcome.original_tokens,
            )
            self._span.set_attribute(
                "optima.context_reduction.effective_tokens",
                outcome.effective_tokens,
            )
            self._span.set_attribute("optima.operation.duration_ms", outcome.latency_ms)
            return
        if isinstance(outcome, ModelStageOutcome):
            self._set_model_attributes(outcome)
            return
        if isinstance(outcome, EvaluationStageOutcome):
            self._set_evaluation_attributes(outcome)
            return
        if isinstance(outcome, PersistenceStageOutcome):
            self._span.set_attribute(
                "optima.run_history.result",
                outcome.result.value,
            )
            if outcome.error_code is not None:
                self._span.set_attribute(
                    "optima.run_history.error_code",
                    outcome.error_code.value,
                )

    def _set_model_attributes(self, outcome: ModelStageOutcome) -> None:
        self._span.set_attribute("optima.model.role", outcome.model_role.value)
        self._span.set_attribute("optima.operation.duration_ms", outcome.latency_ms)
        request_id = outcome.provider_request_id
        if request_id is not None and _PROVIDER_REQUEST_ID.fullmatch(request_id):
            self._span.set_attribute("optima.provider.request_id", request_id)
        _set_optional_measurement(
            self._span,
            "optima.model.input_tokens",
            outcome.input_tokens,
        )
        _set_optional_measurement(
            self._span,
            "optima.model.output_tokens",
            outcome.output_tokens,
        )
        _set_optional_measurement(
            self._span,
            "optima.model.cached_tokens",
            outcome.cached_tokens,
        )

    def _set_evaluation_attributes(self, outcome: EvaluationStageOutcome) -> None:
        self._span.set_attribute("optima.model.role", outcome.model_role.value)
        self._span.set_attribute("optima.operation.duration_ms", outcome.latency_ms)
        self._span.set_attribute(
            "optima.measurement.evaluation.available",
            outcome.score is not None,
        )
        if outcome.evaluator_valid is not None:
            self._span.set_attribute(
                "optima.evaluation.valid",
                outcome.evaluator_valid,
            )
        if outcome.score is not None:
            self._span.set_attribute("optima.evaluation.score", outcome.score)
        if outcome.passed is not None:
            self._span.set_attribute("optima.evaluation.passed", outcome.passed)


class OpenTelemetryRunObservation:
    """One emit-once OpenTelemetry run root and terminal projection."""

    def __init__(
        self,
        owner: OpenTelemetryObservability,
        *,
        run_id: str,
        correlation_id: str,
    ) -> None:
        self._owner = owner
        self._run_id = run_id
        self._correlation_id = correlation_id
        self._span = owner.tracer.start_span(
            "optima.run",
            attributes={
                "optima.telemetry.schema_version": TELEMETRY_SCHEMA_VERSION,
                "optima.run.id": run_id,
                "optima.correlation.id": correlation_id,
            },
            record_exception=False,
            set_status_on_exception=False,
        )
        self._scope: AbstractContextManager[Span] | None = None
        self._lock = RLock()
        self._projected = False
        self._failed = False
        self._closed = False

    def __enter__(self) -> Self:
        with self._lock:
            if self._scope is None and not self._closed:
                self._scope = trace.use_span(
                    self._span,
                    end_on_exit=False,
                    record_exception=False,
                    set_status_on_exception=False,
                )
                self._scope.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if not self._projected and not self._failed:
                self._span.set_attribute("optima.run.observation_incomplete", True)
                self._span.set_status(Status(StatusCode.ERROR))
            if self._scope is not None:
                self._scope.__exit__(None, None, None)
            self._span.end()

    def start_stage(self, stage: ObservationStage) -> OpenTelemetryStageObservation:
        return OpenTelemetryStageObservation(self._owner, stage)

    def project_result(self, result: RunResult) -> None:
        with self._lock:
            if self._projected or self._failed or self._closed:
                return
            self._projected = True
            if (
                result.run_id != self._run_id
                or result.correlation_id != self._correlation_id
            ):
                self._span.set_attribute(
                    "optima.error.category",
                    FailureCategory.VALIDATION.value,
                )
                self._span.set_status(Status(StatusCode.ERROR))
                raise ValueError("terminal result identity does not match observation")

            outcome_span = self._owner.tracer.start_span(
                ObservationStage.OUTCOME_PROJECT.value,
                attributes={
                    "optima.telemetry.schema_version": TELEMETRY_SCHEMA_VERSION,
                },
                record_exception=False,
                set_status_on_exception=False,
            )
            with trace.use_span(
                outcome_span,
                end_on_exit=False,
                record_exception=False,
                set_status_on_exception=False,
            ):
                try:
                    self._project_terminal_attributes(result)
                    self._owner.project_metrics(result)
                    outcome_span.set_attribute(
                        "optima.run.status",
                        result.status.value,
                    )
                    outcome_span.set_status(Status(StatusCode.OK))
                except Exception:
                    outcome_span.set_attribute(
                        "optima.error.category",
                        FailureCategory.VALIDATION.value,
                    )
                    outcome_span.set_status(Status(StatusCode.ERROR))
                    raise
                finally:
                    outcome_span.end()

    def record_pre_result_failure(self, category: FailureCategory) -> None:
        with self._lock:
            if self._failed or self._projected or self._closed:
                return
            self._failed = True
            self._span.set_attribute("optima.error.category", category.value)
            self._span.set_status(Status(StatusCode.ERROR))
            self._owner.record_pre_result_failure(category)

    def _project_terminal_attributes(self, result: RunResult) -> None:
        quality_contract = result.quality_contract
        selected_plan_family = plan_family(
            cache_policy=result.execution_plan.cache_policy,
            model_policy=result.execution_plan.model_policy,
        )
        model_attempt_count = sum(
            step.step_type is ExecutionStepType.MODEL_CALL for step in result.steps
        )
        self._span.set_attributes(
            {
                "optima.plan.family": selected_plan_family.value,
                "optima.optimization.mode": quality_contract.optimization_mode.value,
                "optima.quality.profile": quality_contract.quality_profile.value,
                "optima.task.type": result.request_profile.task_type.value,
                "optima.run.status": result.status.value,
                "optima.contract.result": _contract_result(result),
                "optima.escalated": result.escalated,
                "optima.model.attempt_count": model_attempt_count,
                "optima.measurement.total_input_tokens.available": (
                    result.total_input_tokens is not None
                ),
                "optima.measurement.total_output_tokens.available": (
                    result.total_output_tokens is not None
                ),
                "optima.measurement.total_tokens.available": result.total_tokens
                is not None,
                "optima.measurement.total_cost.available": result.total_calculated_cost
                is not None,
                "optima.measurement.final_evaluation.available": result.final_evaluation
                is not None,
            }
        )
        if result.semantic_cache is not None:
            self._span.set_attribute(
                "optima.cache.outcome",
                result.semantic_cache.outcome.value,
            )
        self._span.set_attribute(
            "optima.context_reduction.outcome",
            _context_reduction_result(result),
        )
        _set_optional_measurement(
            self._span,
            "optima.run.total_input_tokens",
            result.total_input_tokens,
        )
        _set_optional_measurement(
            self._span,
            "optima.run.total_output_tokens",
            result.total_output_tokens,
        )
        _set_optional_measurement(
            self._span,
            "optima.run.total_tokens",
            result.total_tokens,
        )
        if result.total_calculated_cost is not None:
            cost_text = _canonical_decimal_text(result.total_calculated_cost)
            if cost_text is not None:
                self._span.set_attribute("optima.run.total_cost_exact", cost_text)
        if result.final_evaluation is not None:
            self._span.set_attribute(
                "optima.evaluation.final_score",
                result.final_evaluation.score,
            )
        if result.status is RunStatus.COMPLETED:
            self._span.set_status(Status(StatusCode.OK))
        else:
            self._span.set_status(Status(StatusCode.ERROR))


class OpenTelemetryObservability:
    """OpenTelemetry implementation with explicit provider lifecycle hooks."""

    def __init__(
        self,
        *,
        tracer: Tracer,
        meter: Meter,
        tracer_provider: FlushableProvider | None = None,
        meter_provider: FlushableProvider | None = None,
        fastapi_instrumentation_enabled: bool = True,
        exclude_health_routes: bool = True,
        close_callbacks: tuple[Callable[[], None], ...] = (),
    ) -> None:
        self.tracer = tracer
        self._tracer_provider = tracer_provider
        self._meter_provider = meter_provider
        self._fastapi_instrumentation_enabled = fastapi_instrumentation_enabled
        self._exclude_health_routes = exclude_health_routes
        self._close_callbacks = close_callbacks
        self._lock = RLock()
        self._closed = False
        self._runs: Counter = meter.create_counter(
            "optima.runs",
            unit="{run}",
            description="Validated terminal OPTIMA runs",
        )
        self._run_duration: Histogram = meter.create_histogram(
            "optima.run.duration",
            unit="ms",
            description="Validated terminal OPTIMA run latency",
        )
        self._model_attempts: Counter = meter.create_counter(
            "optima.model.attempts",
            unit="{attempt}",
            description="Actual model-generation attempts",
        )
        self._model_duration: Histogram = meter.create_histogram(
            "optima.model.duration",
            unit="ms",
            description="Actual model-generation attempt latency",
        )
        self._tokens: Counter = meter.create_counter(
            "optima.tokens",
            unit="{token}",
            description="Measured model and embedding tokens",
        )
        self._cache_lookups: Counter = meter.create_counter(
            "optima.cache.lookups",
            unit="{lookup}",
            description="Attempted semantic-cache lookups",
        )
        self._escalations: Counter = meter.create_counter(
            "optima.escalations",
            unit="{escalation}",
            description="Actual small-to-strong escalations",
        )
        self._contract_results: Counter = meter.create_counter(
            "optima.quality_contract.results",
            unit="{result}",
            description="Validated terminal Quality Contract results",
        )
        self._evaluation_scores: Histogram = meter.create_histogram(
            "optima.evaluation.score",
            unit="1",
            description="Actual evaluator scores",
        )
        self._embedding_attempts: Counter = meter.create_counter(
            "optima.embedding.attempts",
            unit="{attempt}",
            description="Actual embedding-provider attempts",
        )
        self._history_persistence: Counter = meter.create_counter(
            "optima.run_history.persistence",
            unit="{attempt}",
            description="Attempted run-history saves",
        )
        self._telemetry_projections: Counter = meter.create_counter(
            "optima.telemetry.projections",
            unit="{projection}",
            description="Terminal and pre-result telemetry projections",
        )

    def start_run(
        self,
        *,
        run_id: str,
        correlation_id: str,
    ) -> OpenTelemetryRunObservation:
        return OpenTelemetryRunObservation(
            self,
            run_id=run_id,
            correlation_id=correlation_id,
        )

    def instrument_fastapi(self, application: FastAPI) -> None:
        if not self._fastapi_instrumentation_enabled:
            return
        if getattr(application.state, "optima_otel_instrumented", False):
            return
        excluded_paths = (
            frozenset({"/api/v1/health"})
            if self._exclude_health_routes
            else frozenset()
        )
        application.add_middleware(
            PrivacySafeOpenTelemetryMiddleware,
            tracer=self.tracer,
            excluded_paths=excluded_paths,
            route_templates=tuple(application.openapi()["paths"]),
        )
        application.state.optima_otel_instrumented = True

    def project_metrics(self, result: RunResult) -> None:
        """Project bounded metrics once from one validated terminal result."""
        family = plan_family(
            cache_policy=result.execution_plan.cache_policy,
            model_policy=result.execution_plan.model_policy,
        )
        run_attributes = {
            "optima.run.status": result.status.value,
            "optima.plan.family": family.value,
        }
        self._runs.add(1, run_attributes)
        self._run_duration.record(result.latency_ms, run_attributes)
        contract_attributes = {"optima.contract.result": _contract_result(result)}
        self._contract_results.add(1, contract_attributes)
        if result.escalated:
            self._escalations.add(1, {"optima.plan.family": family.value})

        self._project_model_metrics(result.steps)
        for usage in result.model_usages:
            role_attributes = {"optima.model.role": usage.model_role.value}
            _add_optional_counter(
                self._tokens,
                usage.input_tokens,
                {**role_attributes, "optima.token.category": "INPUT"},
            )
            _add_optional_counter(
                self._tokens,
                usage.output_tokens,
                {**role_attributes, "optima.token.category": "OUTPUT"},
            )
            _add_optional_counter(
                self._tokens,
                usage.cached_tokens,
                {**role_attributes, "optima.token.category": "CACHED"},
            )

        cache = result.semantic_cache
        if cache is not None and cache.outcome in _ATTEMPTED_CACHE_OUTCOMES:
            self._cache_lookups.add(
                1,
                {"optima.cache.outcome": cache.outcome.value},
            )
        if cache is not None and cache.embedding_attempt is not None:
            attempt = cache.embedding_attempt
            if attempt.invoked:
                embedding_result = (
                    "MEASURED"
                    if attempt.usage is not None
                    else (
                        "UNMEASURED_OUTBOUND"
                        if attempt.outbound_attempted
                        else "FAILED_PRE_OUTBOUND"
                    )
                )
                self._embedding_attempts.add(
                    1,
                    {"optima.embedding.result": embedding_result},
                )
                if attempt.usage is not None and attempt.usage.input_tokens is not None:
                    self._tokens.add(
                        attempt.usage.input_tokens,
                        {"optima.token.category": "EMBEDDING"},
                    )

        for evaluation in result.evaluations:
            evaluation_result = (
                "INVALID"
                if not evaluation.evaluator_valid
                else ("PASSED" if evaluation.passed else "REJECTED")
            )
            self._evaluation_scores.record(
                evaluation.score,
                {"optima.evaluation.result": evaluation_result},
            )
        self._telemetry_projections.add(
            1,
            {"optima.telemetry.projection_result": "TERMINAL"},
        )

    def record_persistence(self, outcome: PersistenceStageOutcome) -> None:
        self._history_persistence.add(
            1,
            {"optima.run_history.result": outcome.result.value},
        )

    def record_pre_result_failure(self, category: FailureCategory) -> None:
        self._telemetry_projections.add(
            1,
            {
                "optima.telemetry.projection_result": "PRE_RESULT_FAILURE",
                "optima.error.category": category.value,
            },
        )

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        if timeout_millis <= 0:
            return False
        deadline = monotonic() + timeout_millis / 1000
        results: list[bool] = []
        for provider in (self._tracer_provider, self._meter_provider):
            if provider is None:
                continue
            remaining_millis = int((deadline - monotonic()) * 1000)
            if remaining_millis <= 0:
                return False
            try:
                result = provider.force_flush(remaining_millis)
                results.append(result is not False)
            except Exception:
                results.append(False)
        return all(results)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for provider in (self._tracer_provider, self._meter_provider):
            if provider is not None:
                try:
                    provider.shutdown()
                except Exception:
                    continue
        for callback in self._close_callbacks:
            try:
                callback()
            except Exception:
                continue

    def _project_model_metrics(self, steps: tuple[ExecutionStep, ...]) -> None:
        for step in steps:
            if step.step_type is not ExecutionStepType.MODEL_CALL:
                continue
            role = _step_model_role(step)
            if role is None:
                continue
            attributes = {
                "optima.model.role": role.value,
                "optima.model.result": step.status.value,
            }
            self._model_attempts.add(1, attributes)
            self._model_duration.record(step.latency_ms, attributes)


def _set_operation_status(span: Span, status: ObservationStatus) -> None:
    status_code = (
        StatusCode.OK if status is ObservationStatus.SUCCEEDED else StatusCode.ERROR
    )
    span.set_status(Status(status_code))


def _set_optional_measurement(span: Span, name: str, value: int | None) -> None:
    span.set_attribute(
        f"optima.measurement.{name.removeprefix('optima.')}.available",
        value is not None,
    )
    if value is not None:
        span.set_attribute(name, value)


def _add_optional_counter(
    counter: Counter,
    value: int | None,
    attributes: dict[str, str],
) -> None:
    if value is not None:
        counter.add(value, attributes)


def _canonical_decimal_text(value: Decimal) -> str | None:
    """Return one bounded exact fixed-point representation, or None when unsafe.

    Cost rates carry no exponent bound in the domain, so an extreme-exponent
    Decimal is omitted rather than expanded into a pathologically wide fixed-
    point attribute. Significant digits are already capped by the Decimal
    context; only the exponent-driven width is bounded here.
    """
    if value.is_zero():
        return "0"
    _sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        return None
    integer_places = len(digits) + exponent
    fractional_places = -exponent if exponent < 0 else 0
    if max(integer_places, 1) + fractional_places > _MAX_COST_FIXED_POINT_CHARS:
        return None
    fixed_point = format(value, "f")
    if "." not in fixed_point:
        return fixed_point
    return fixed_point.rstrip("0").rstrip(".")


def _step_model_role(step: ExecutionStep) -> ModelRole | None:
    raw_role = step.facts.get("model_role")
    try:
        return ModelRole(raw_role) if isinstance(raw_role, str) else None
    except ValueError:
        return None


def _contract_result(result: RunResult) -> str:
    if result.contract_met is True:
        return "MET"
    if result.contract_met is False:
        return "NOT_MET"
    return "UNAVAILABLE"


def _context_reduction_result(result: RunResult) -> str:
    step = next(
        (
            candidate
            for candidate in result.steps
            if candidate.step_type is ExecutionStepType.CONTEXT_REDUCTION
        ),
        None,
    )
    if step is None or step.context_reduction is None:
        return "NOT_ATTEMPTED"
    return step.context_reduction.outcome.value


def _safe_http_method(value: object) -> str:
    if not isinstance(value, str):
        return "OTHER"
    normalized = value.upper()
    return normalized if normalized in _HTTP_METHODS else "OTHER"


def _trace_context_carrier(scope: Scope) -> dict[str, str]:
    carrier: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", ()):
        name = raw_name.decode("latin-1").lower()
        if name in {"traceparent", "tracestate"}:
            carrier[name] = raw_value.decode("latin-1")
    return carrier


def _compile_route_template(template: str) -> re.Pattern[str]:
    segments = template.split("/")
    pattern_segments = [
        r"[^/]+"
        if segment.startswith("{") and segment.endswith("}")
        else re.escape(segment)
        for segment in segments
    ]
    return re.compile("^" + "/".join(pattern_segments) + "$")


def _safe_route_template(
    scope: Scope,
    route_templates: tuple[tuple[str, re.Pattern[str]], ...],
) -> str | None:
    path = scope.get("path")
    if not isinstance(path, str):
        return None
    for template, pattern in route_templates:
        if pattern.fullmatch(path):
            return template
    return None


def _safe_exception_category(error: Exception) -> str:
    name = type(error).__name__
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
        return name
    return "Exception"
