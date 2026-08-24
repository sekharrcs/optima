"""Offline proofs for OPTIMA tracing and operational metrics."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
)
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import SecretStr, ValidationError

from optima.api.app import create_app
from optima.api.dependencies import ExecutionDependencies
from optima.api.models import RunRequest
from optima.cache import FakeSemanticCache
from optima.config import ApplicationInsightsConfiguration, AppSettings
from optima.context import (
    ContextPreservationEvidence,
    ContextReductionResult,
    FakeContextReducer,
    RegexTokenCounter,
)
from optima.context.safety import DeterministicExtractiveSafetyPolicy
from optima.cost import CostCalculator, PriceCatalog, PriceCatalogEntry
from optima.domain.cache import CacheCandidate
from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import ModelRole
from optima.domain.request_binding import build_request_binding
from optima.domain.run import RunResult
from optima.evaluation import EvaluationEvidence, FakeEvaluator
from optima.observability import (
    CacheLookupResult,
    CacheStageOutcome,
    ContextStageOutcome,
    FailureCategory,
    InMemoryObservability,
    ModelStageOutcome,
    ObservationStage,
    ObservationStatus,
    PersistenceStageOutcome,
)
from optima.observability import azure_monitor as azure_monitor_module
from optima.observability.azure_monitor import (
    AzureMonitorRuntimeRegistry,
    build_observability,
)
from optima.observability.noop import NO_OP_STAGE
from optima.observability.opentelemetry import OpenTelemetryObservability
from optima.observability.resilient import FailureIsolatedStageObservation
from optima.providers import (
    FakeModelProvider,
    FakeProviderResponse,
    ModelProviderRequest,
    ModelProviderResult,
    build_fake_small_provider,
    build_fake_strong_provider,
)
from optima.storage import RunHistoryNotFoundError

_CONNECTION_STRING = (
    "InstrumentationKey=00000000-0000-0000-0000-000000000001;"
    "IngestionEndpoint=https://example.applicationinsights.azure.com/"
)


class IncrementingClock:
    """Deterministic monotonic clock for telemetry tests."""

    def __init__(self) -> None:
        self._value = 0.0

    def now(self) -> float:
        value = self._value
        self._value += 0.001
        return value


class TimeoutSmallProvider:
    """Record one SMALL call and fail with a sensitive timeout message."""

    provider_name = "fake"
    deployment_name = "small"
    model_role = ModelRole.SMALL

    def __init__(self) -> None:
        self.calls: list[ModelProviderRequest] = []

    async def generate(self, request: ModelProviderRequest) -> ModelProviderResult:
        self.calls.append(request)
        raise TimeoutError("SECRET_PROVIDER_TIMEOUT_DETAIL")


class FailingHistoryStore:
    """Fail saves while satisfying the run-history contract for composition."""

    def __init__(self) -> None:
        self.save_calls = 0

    async def save(self, result: RunResult) -> None:
        self.save_calls += 1
        raise RuntimeError("SECRET_COSMOS_DOCUMENT")

    async def get(self, run_id: str) -> RunResult:
        raise RunHistoryNotFoundError

    async def list_recent(self, limit: int) -> tuple[RunResult, ...]:
        return ()


class FailingProjectionRun:
    """Raise only during terminal telemetry projection."""

    def __init__(self) -> None:
        self.exit_calls = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exit_calls += 1

    def start_stage(self, stage: ObservationStage) -> Any:
        return NO_OP_STAGE

    def project_result(self, result: RunResult) -> None:
        raise RuntimeError("SECRET_EXPORTER_FAILURE")

    def record_pre_result_failure(self, category: FailureCategory) -> None:
        return None


class FailingProjectionObservability:
    """Return a recorder that fails after the authoritative result exists."""

    def __init__(self) -> None:
        self.run = FailingProjectionRun()

    def start_run(self, *, run_id: str, correlation_id: str) -> FailingProjectionRun:
        return self.run

    def instrument_fastapi(self, application: FastAPI) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        raise RuntimeError("SECRET_FLUSH_FAILURE")

    def close(self) -> None:
        raise RuntimeError("SECRET_CLOSE_FAILURE")


def request_payload(**updates: object) -> dict[str, object]:
    """Build one strict request routed to SMALL first by default."""
    payload: dict[str, object] = {
        "input_text": "SECRET_PROMPT summarize the incident",
        "context": "SECRET_CONTEXT incident facts",
        "request_profile": {
            "task_type": "SUMMARIZATION",
            "complexity": "LOW",
            "input_tokens": 100,
            "risk_tier": "LOW",
            "cache_eligible": False,
            "has_large_context": False,
        },
        "quality_profile": "HIGH",
        "optimization_mode": "COST",
        "risk_tier": "LOW",
        "reference_output": "SECRET_REFERENCE",
        "criteria": ["SECRET_CRITERION"],
        "metadata": {"private": "SECRET_METADATA"},
    }
    payload.update(updates)
    return payload


def dependencies(
    scores: Iterable[float],
    *,
    observability: Any,
    small_provider: Any | None = None,
) -> tuple[ExecutionDependencies, Any, FakeModelProvider, FakeEvaluator]:
    """Build one complete offline API composition."""
    small = small_provider or build_fake_small_provider(
        provider_name="fake",
        deployment_name="small",
        responses=(
            FakeProviderResponse(
                output_text="SECRET_MODEL_OUTPUT",
                input_tokens=100,
                output_tokens=20,
            ),
        ),
        clock=IncrementingClock(),
    )
    strong = build_fake_strong_provider(
        provider_name="fake",
        deployment_name="strong",
        responses=(
            FakeProviderResponse(
                output_text="SECRET_STRONG_OUTPUT",
                input_tokens=110,
                output_tokens=30,
            ),
        ),
        clock=IncrementingClock(),
    )
    evaluator = FakeEvaluator(
        responses=tuple(
            EvaluationEvidence(
                evaluator_type="fake-deterministic",
                evaluator_valid=True,
                score=score,
                metadata={"private": "SECRET_EVALUATOR_METADATA"},
            )
            for score in scores
        )
    )
    calculator = CostCalculator(
        PriceCatalog(
            version="telemetry-test-v1",
            currency="TEST",
            entries=(
                PriceCatalogEntry(
                    provider="fake",
                    deployment="small",
                    input_rate_per_million_tokens=Decimal("2"),
                    output_rate_per_million_tokens=Decimal("40"),
                ),
                PriceCatalogEntry(
                    provider="fake",
                    deployment="strong",
                    input_rate_per_million_tokens=Decimal("30"),
                    output_rate_per_million_tokens=Decimal("190"),
                ),
            ),
        )
    )
    configured = ExecutionDependencies(
        settings=AppSettings(
            semantic_cache_enabled=False,
            context_reduction_enabled=False,
            historical_policy_enabled=False,
        ),
        small_provider=small,
        strong_provider=strong,
        evaluator=evaluator,
        cost_calculator=calculator,
        monotonic_clock=IncrementingClock(),
        utc_now=lambda: datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
        run_id_factory=lambda: "run-observability-1",
        correlation_id_factory=lambda: "correlation-observability-1",
        observability=observability,
    )
    return configured, small, strong, evaluator


def cache_candidate(payload: dict[str, object]) -> CacheCandidate:
    """Build a cache candidate bound to the supplied request."""
    request = RunRequest.model_validate(payload)
    binding = build_request_binding(
        input_text=request.input_text,
        context=request.context,
        reference_output=request.reference_output,
        criteria=request.criteria,
        metadata=request.metadata,
        task_type=request.request_profile.task_type,
        complexity=request.request_profile.complexity,
    )
    return CacheCandidate(
        source_run_id="run-cache-source",
        output_text="SECRET_CACHE_OUTPUT",
        request_binding=binding,
        similarity=0.99,
        prior_evaluation=EvaluationResult(
            evaluator_type="source-evaluator",
            evaluator_valid=True,
            score=0.96,
            threshold=0.80,
            mandatory_checks_passed=True,
            passed=True,
            reasons=("SECRET_SOURCE_REASON",),
            metadata={"private": "SECRET_SOURCE_METADATA"},
        ),
        contract_compatible=True,
        safe_to_reuse=True,
    )


def reduction_result(context: str) -> ContextReductionResult:
    """Build one measured deterministic reduction result."""
    counter = RegexTokenCounter()
    reduced = "Incident ARC-9 was resolved."
    return ContextReductionResult(
        reduced_context=reduced,
        original_token_count=counter.count(context),
        reduced_token_count=counter.count(reduced),
        reducer_name="fake-context-reducer",
        method="EXTRACTIVE_TEST",
        token_counter_name=counter.counter_name,
        preservation=ContextPreservationEvidence(
            source_order_preserved=True,
            original_segment_count=2,
            retained_segment_indexes=(0,),
            removed_duplicate_count=1,
            removed_irrelevant_count=0,
            task_terms_used=("incident",),
        ),
    )


def stage_records(observer: InMemoryObservability) -> list[Any]:
    """Return closed child observations without the run root."""
    return [item for item in observer.observations if item.name != "optima.run"]


def assert_children_share_root(observer: InMemoryObservability) -> None:
    """Assert every recorded operation is a direct child of one run root."""
    root = next(item for item in observer.observations if item.name == "optima.run")
    assert all(
        item.parent_observation_id == root.observation_id
        for item in stage_records(observer)
    )


def local_otel_observer(
    sampler: Sampler = ALWAYS_ON,
) -> tuple[
    OpenTelemetryObservability,
    InMemorySpanExporter,
    InMemoryMetricReader,
    TracerProvider,
    MeterProvider,
]:
    """Build isolated OpenTelemetry providers without touching global state."""
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider(sampler=sampler)
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    observer = OpenTelemetryObservability(
        tracer=tracer_provider.get_tracer("optima.test", "1"),
        meter=meter_provider.get_meter("optima.test", "1"),
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )
    return observer, span_exporter, metric_reader, tracer_provider, meter_provider


def metric_points(reader: InMemoryMetricReader) -> list[tuple[str, Any]]:
    """Flatten local metric data into instrument name and data-point pairs."""
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        (metric.name, point)
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        for point in metric.data.data_points
    ]


def run_result_from_response(response: Any) -> RunResult:
    """Revalidate one API result after removing serialized computed fields."""
    payload = response.json()
    for computed_field in (
        "total_input_tokens",
        "total_output_tokens",
        "total_tokens",
        "total_calculated_cost",
        "total_cost_provenance",
    ):
        payload.pop(computed_field)
    return RunResult.model_validate(payload)


def test_disabled_observability_is_inert(
    tmp_path: Path,
) -> None:
    """Do not invoke Azure composition, start threads, or persist files by default."""
    calls = 0
    before_threads = {thread.ident for thread in threading.enumerate()}

    def forbidden_initializer(
        configuration: ApplicationInsightsConfiguration,
    ) -> InMemoryObservability:
        nonlocal calls
        calls += 1
        raise AssertionError("disabled observability initialized Azure")

    observer = build_observability(
        AppSettings(),
        registry=AzureMonitorRuntimeRegistry(),
        initializer=forbidden_initializer,
    )
    application = create_app()
    observer.instrument_fastapi(application)
    observer.force_flush()
    observer.close()

    assert calls == 0
    assert {thread.ident for thread in threading.enumerate()} == before_threads
    assert list(tmp_path.iterdir()) == []


def test_enabled_invalid_observability_fails_before_initialization() -> None:
    """Reject missing configuration before any initializer can run."""
    with pytest.raises(ValidationError, match="requires a configured connection"):
        AppSettings(application_insights_enabled=True)


def test_runtime_registry_initializes_once_and_rejects_conflicts() -> None:
    """Do not create duplicate exporters for repeated application composition."""
    registry = AzureMonitorRuntimeRegistry()
    calls: list[ApplicationInsightsConfiguration] = []

    def initialize(
        configuration: ApplicationInsightsConfiguration,
    ) -> InMemoryObservability:
        calls.append(configuration)
        return InMemoryObservability()

    settings = AppSettings(
        application_insights_enabled=True,
        application_insights_connection_string=SecretStr(_CONNECTION_STRING),
    )
    first = build_observability(settings, registry=registry, initializer=initialize)
    second = build_observability(settings, registry=registry, initializer=initialize)

    assert first is not second
    assert len(calls) == 1

    conflicting = AppSettings(
        application_insights_enabled=True,
        application_insights_connection_string=SecretStr(_CONNECTION_STRING),
        application_insights_sampling_ratio=0.5,
    )
    with pytest.raises(ValueError, match="different settings"):
        build_observability(
            conflicting,
            registry=registry,
            initializer=initialize,
        )
    assert len(calls) == 1


def test_runtime_initializer_failure_is_cached_as_inert() -> None:
    """Contain exporter startup failure and never retry process initialization."""
    registry = AzureMonitorRuntimeRegistry()
    calls = 0

    def fail(configuration: ApplicationInsightsConfiguration) -> InMemoryObservability:
        nonlocal calls
        calls += 1
        raise RuntimeError("SECRET_INITIALIZER_FAILURE")

    settings = AppSettings(
        application_insights_enabled=True,
        application_insights_connection_string=SecretStr(_CONNECTION_STRING),
    )

    first = build_observability(settings, registry=registry, initializer=fail)
    second = build_observability(settings, registry=registry, initializer=fail)

    assert first.force_flush() is True
    assert second.force_flush() is True
    assert calls == 1


def test_azure_initializer_passes_explicit_privacy_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configure only trace/metric export with every auto-instrumentation disabled."""
    captured: dict[str, Any] = {}
    observed_environment: dict[str, str | None] = {}
    processed_resource_attributes: dict[str, Any] = {}
    from opentelemetry import metrics, trace

    previous_tracer_provider = trace.get_tracer_provider()
    previous_meter_provider = metrics.get_meter_provider()
    tracer_provider = TracerProvider()
    meter_provider = MeterProvider()
    initialized = False

    def capture_configuration(**kwargs: Any) -> None:
        nonlocal initialized
        from azure.monitor.opentelemetry._utils.configurations import (
            _get_configurations,
        )

        captured.update(kwargs)
        processed = _get_configurations(**kwargs)
        processed_resource = processed["resource"]
        assert isinstance(processed_resource, Resource)
        processed_resource_attributes.update(processed_resource.attributes)
        for name in (
            "APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED",
            "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED",
            "APPLICATIONINSIGHTS_SDKSTATS_DISABLED",
            "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL",
            "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS",
            "OTEL_LOGS_EXPORTER",
            "OTEL_METRICS_EXPORTER",
            "OTEL_RESOURCE_ATTRIBUTES",
            "OTEL_SERVICE_NAME",
            "OTEL_TRACES_EXPORTER",
            "OTEL_TRACES_SAMPLER",
            "OTEL_TRACES_SAMPLER_ARG",
        ):
            observed_environment[name] = os.environ.get(name)
        initialized = True

    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "console")
    monkeypatch.delenv(
        "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED",
        raising=False,
    )
    monkeypatch.setenv("OTEL_SERVICE_NAME", "SECRET_SERVICE")
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        "secret.attribute=SECRET_RESOURCE,enduser.id=SECRET_USER,server.address=SECRET_HOST",
    )
    configuration = ApplicationInsightsConfiguration(
        connection_string=SecretStr(_CONNECTION_STRING),
        service_name="optima-test",
        service_version="1.2.3",
        deployment_environment="test",
        sampling_ratio=0.25,
    )

    observer = azure_monitor_module._initialize_azure_monitor(
        configuration,
        configurator=capture_configuration,
        tracer_provider_getter=(
            lambda: tracer_provider if initialized else previous_tracer_provider
        ),
        meter_provider_getter=(
            lambda: meter_provider if initialized else previous_meter_provider
        ),
    )

    assert captured["connection_string"] == _CONNECTION_STRING
    assert captured["disable_offline_storage"] is True
    assert captured["enable_live_metrics"] is False
    assert captured["enable_performance_counters"] is False
    assert captured["enable_trace_based_sampling_for_logs"] is False
    assert captured["browser_sdk_loader_config"] == {"enabled": False}
    assert captured["retry_total"] == 0
    assert captured["retry_connect"] == 0
    assert captured["retry_read"] == 0
    assert captured["retry_status"] == 0
    assert captured["redirect_max"] == 0
    assert all(
        option == {"enabled": False}
        for option in captured["instrumentation_options"].values()
    )
    assert observed_environment == {
        "APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED": "true",
        "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED": "true",
        "APPLICATIONINSIGHTS_SDKSTATS_DISABLED": "true",
        "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL": "true",
        "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS": "",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_METRICS_EXPORTER": "",
        "OTEL_RESOURCE_ATTRIBUTES": "",
        "OTEL_SERVICE_NAME": "",
        "OTEL_TRACES_EXPORTER": "",
        "OTEL_TRACES_SAMPLER": "parentbased_trace_id_ratio",
        "OTEL_TRACES_SAMPLER_ARG": "0.25",
    }
    assert os.environ["OTEL_LOGS_EXPORTER"] == "console"
    assert os.environ["OTEL_SERVICE_NAME"] == "SECRET_SERVICE"
    assert "SECRET_RESOURCE" in os.environ["OTEL_RESOURCE_ATTRIBUTES"]
    assert processed_resource_attributes["service.name"] == "optima-test"
    assert processed_resource_attributes["service.version"] == "1.2.3"
    assert processed_resource_attributes["deployment.environment.name"] == "test"
    assert not any(
        "SECRET" in str(value) for value in processed_resource_attributes.values()
    )
    assert (
        os.environ["APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED"]
        == "true"
    )
    observer.close()
    assert (
        "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED" not in os.environ
    )


def test_preconfigured_global_provider_disables_azure_initialization() -> None:
    """Do not orphan or claim ownership of an existing telemetry provider."""
    registry = AzureMonitorRuntimeRegistry()
    configure_calls = 0

    def initialize(
        configuration: ApplicationInsightsConfiguration,
    ) -> Any:
        nonlocal configure_calls

        def configure(**kwargs: Any) -> None:
            nonlocal configure_calls
            configure_calls += 1

        return azure_monitor_module._initialize_azure_monitor(
            configuration,
            configurator=configure,
            tracer_provider_getter=lambda: TracerProvider(),
            meter_provider_getter=lambda: MeterProvider(),
        )

    settings = AppSettings(
        application_insights_enabled=True,
        application_insights_connection_string=SecretStr(_CONNECTION_STRING),
    )

    observer = build_observability(
        settings,
        registry=registry,
        initializer=initialize,
    )

    assert observer.force_flush() is True
    assert configure_calls == 0


def test_partial_initializer_failure_closes_new_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean up SDK providers created before a configurator failure."""
    from opentelemetry import metrics, trace

    previous_tracer_provider = trace.get_tracer_provider()
    previous_meter_provider = metrics.get_meter_provider()
    monkeypatch.delenv(
        "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED",
        raising=False,
    )

    class TrackingTracerProvider(TracerProvider):
        def __init__(self) -> None:
            super().__init__()
            self.shutdown_calls = 0
            self.guard_values: list[str | None] = []

        def shutdown(self) -> None:
            self.shutdown_calls += 1
            self.guard_values.append(
                os.environ.get(
                    "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED"
                )
            )

    class TrackingMeterProvider(MeterProvider):
        def __init__(self) -> None:
            super().__init__()
            self.shutdown_calls = 0
            self.guard_values: list[str | None] = []

        def shutdown(self, timeout_millis: float = 30_000) -> None:
            self.shutdown_calls += 1
            self.guard_values.append(
                os.environ.get(
                    "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED"
                )
            )

    tracer_provider = TrackingTracerProvider()
    meter_provider = TrackingMeterProvider()
    initialized = False

    def fail(**kwargs: Any) -> None:
        nonlocal initialized
        initialized = True
        raise RuntimeError("SECRET_PARTIAL_INITIALIZATION")

    configuration = ApplicationInsightsConfiguration(
        connection_string=SecretStr(_CONNECTION_STRING)
    )

    with pytest.raises(RuntimeError, match="SECRET_PARTIAL_INITIALIZATION"):
        azure_monitor_module._initialize_azure_monitor(
            configuration,
            configurator=fail,
            tracer_provider_getter=(
                lambda: tracer_provider if initialized else previous_tracer_provider
            ),
            meter_provider_getter=(
                lambda: meter_provider if initialized else previous_meter_provider
            ),
        )

    assert tracer_provider.shutdown_calls == 1
    assert meter_provider.shutdown_calls == 1
    assert tracer_provider.guard_values == ["true"]
    assert meter_provider.guard_values == ["true"]
    assert (
        "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED" not in os.environ
    )


def test_adapter_construction_failure_closes_owned_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean up providers when adapter construction fails after SDK setup."""
    from opentelemetry import metrics, trace

    previous_tracer_provider = trace.get_tracer_provider()
    previous_meter_provider = metrics.get_meter_provider()
    tracer_provider = TracerProvider()
    meter_provider = MeterProvider()
    initialized = False
    shutdown_guards: list[str | None] = []

    monkeypatch.delenv(
        "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED",
        raising=False,
    )
    monkeypatch.setattr(
        tracer_provider,
        "shutdown",
        lambda: shutdown_guards.append(
            os.environ.get("APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED")
        ),
    )
    monkeypatch.setattr(
        meter_provider,
        "shutdown",
        lambda timeout_millis=30_000: shutdown_guards.append(
            os.environ.get("APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED")
        ),
    )

    def configure(**kwargs: Any) -> None:
        nonlocal initialized
        initialized = True

    def fail_adapter(**kwargs: Any) -> None:
        raise RuntimeError("SECRET_ADAPTER_CONSTRUCTION")

    monkeypatch.setattr(
        azure_monitor_module,
        "OpenTelemetryObservability",
        fail_adapter,
    )
    configuration = ApplicationInsightsConfiguration(
        connection_string=SecretStr(_CONNECTION_STRING)
    )

    with pytest.raises(RuntimeError, match="SECRET_ADAPTER_CONSTRUCTION"):
        azure_monitor_module._initialize_azure_monitor(
            configuration,
            configurator=configure,
            tracer_provider_getter=(
                lambda: tracer_provider if initialized else previous_tracer_provider
            ),
            meter_provider_getter=(
                lambda: meter_provider if initialized else previous_meter_provider
            ),
        )

    assert shutdown_guards == ["true", "true"]
    assert (
        "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED" not in os.environ
    )


def test_meter_retrieval_failure_closes_retained_tracer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not lose a retrieved tracer when the later meter lookup fails."""
    from opentelemetry import metrics, trace

    previous_tracer_provider = trace.get_tracer_provider()
    previous_meter_provider = metrics.get_meter_provider()
    tracer_provider = TracerProvider()
    initialized = False
    tracer_shutdown_guards: list[str | None] = []

    monkeypatch.delenv(
        "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED",
        raising=False,
    )
    monkeypatch.setattr(
        tracer_provider,
        "shutdown",
        lambda: tracer_shutdown_guards.append(
            os.environ.get("APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED")
        ),
    )

    def configure(**kwargs: Any) -> None:
        nonlocal initialized
        initialized = True

    def get_tracer_provider() -> object:
        return tracer_provider if initialized else previous_tracer_provider

    def get_meter_provider() -> object:
        if initialized:
            raise RuntimeError("SECRET_METER_RETRIEVAL")
        return previous_meter_provider

    configuration = ApplicationInsightsConfiguration(
        connection_string=SecretStr(_CONNECTION_STRING)
    )

    with pytest.raises(RuntimeError, match="SECRET_METER_RETRIEVAL"):
        azure_monitor_module._initialize_azure_monitor(
            configuration,
            configurator=configure,
            tracer_provider_getter=get_tracer_provider,
            meter_provider_getter=get_meter_provider,
        )

    assert tracer_shutdown_guards == ["true"]
    assert (
        "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED" not in os.environ
    )


def test_stage_emission_failure_still_closes_entered_delegate() -> None:
    """Detach and close an entered stage even when telemetry finishing fails."""

    class FailingStage:
        def __init__(self) -> None:
            self.exit_calls = 0

        def __enter__(self) -> Self:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            self.exit_calls += 1

        def finish(self, outcome: Any) -> None:
            raise RuntimeError("SECRET_STAGE_FAILURE")

    delegate = FailingStage()
    stage = FailureIsolatedStageObservation(delegate)
    with stage:
        stage.finish(
            ModelStageOutcome(
                status=ObservationStatus.SUCCEEDED,
                model_role=ModelRole.SMALL,
                latency_ms=1,
            )
        )

    assert delegate.exit_calls == 1


def test_successful_small_run_records_exact_hierarchy_once() -> None:
    """Observe the actual small-first path and one terminal projection."""
    observer = InMemoryObservability()
    configured, small, strong, evaluator = dependencies((0.93,), observability=observer)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(),
    )

    assert response.status_code == 200
    assert [record.name for record in stage_records(observer)] == [
        ObservationStage.QUALITY_CONTRACT_BUILD,
        ObservationStage.PLANNER_SELECT,
        ObservationStage.MODEL_GENERATE,
        ObservationStage.EVALUATION_EVALUATE,
        ObservationStage.OUTCOME_PROJECT,
    ]
    assert observer.projected_run_ids == ("run-observability-1",)
    assert_children_share_root(observer)
    assert len(small.calls) == 1
    assert len(strong.calls) == 0
    assert len(evaluator.calls) == 1


def test_strong_direct_records_only_actual_attempts() -> None:
    """Do not create SMALL or escalation observations for strong-direct execution."""
    observer = InMemoryObservability()
    configured, small, strong, evaluator = dependencies((0.93,), observability=observer)
    payload = request_payload(
        request_profile={
            "task_type": "GENERAL_REASONING",
            "complexity": "HIGH",
            "input_tokens": 100,
            "risk_tier": "LOW",
            "cache_eligible": False,
            "has_large_context": False,
        }
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=payload
    )

    model_outcomes = [
        record.outcome
        for record in stage_records(observer)
        if record.name == ObservationStage.MODEL_GENERATE
    ]
    assert response.status_code == 200
    assert len(model_outcomes) == 1
    assert isinstance(model_outcomes[0], ModelStageOutcome)
    assert model_outcomes[0].model_role is ModelRole.STRONG
    assert len(small.calls) == 0
    assert len(strong.calls) == 1
    assert len(evaluator.calls) == 1


def test_escalation_records_distinct_model_and_evaluation_attempts() -> None:
    """Record SMALL and STRONG attempts as distinct children of one run."""
    observer = InMemoryObservability()
    configured, small, strong, evaluator = dependencies(
        (0.70, 0.95), observability=observer
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )

    model_outcomes = [
        record.outcome
        for record in stage_records(observer)
        if record.name == ObservationStage.MODEL_GENERATE
    ]
    evaluation_outcomes = [
        record.outcome
        for record in stage_records(observer)
        if record.name == ObservationStage.EVALUATION_EVALUATE
    ]
    assert response.status_code == 200
    assert response.json()["escalated"] is True
    assert [
        outcome.model_role
        for outcome in model_outcomes
        if isinstance(outcome, ModelStageOutcome)
    ] == [
        ModelRole.SMALL,
        ModelRole.STRONG,
    ]
    assert len(evaluation_outcomes) == 2
    assert_children_share_root(observer)
    assert len(small.calls) == len(strong.calls) == 1
    assert len(evaluator.calls) == 2


def test_cache_hit_records_lookup_without_model_or_evaluator_spans() -> None:
    """Observe a reused result without fabricating model or evaluator operations."""
    observer = InMemoryObservability()
    configured, small, strong, evaluator = dependencies((0.93,), observability=observer)
    payload = request_payload(
        request_profile={
            "task_type": "SUMMARIZATION",
            "complexity": "LOW",
            "input_tokens": 100,
            "risk_tier": "LOW",
            "cache_eligible": True,
            "has_large_context": False,
        }
    )
    cache = FakeSemanticCache((cache_candidate(payload),))
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=True,
            context_reduction_enabled=False,
            historical_policy_enabled=False,
        ),
        semantic_cache=cache,
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=payload
    )

    names = [record.name for record in stage_records(observer)]
    assert response.status_code == 200
    assert response.json()["semantic_cache"]["outcome"] == "REUSED"
    assert ObservationStage.SEMANTIC_CACHE_LOOKUP in names
    assert ObservationStage.MODEL_GENERATE not in names
    assert ObservationStage.EVALUATION_EVALUATE not in names
    assert len(cache.calls) == 1
    assert small.calls == ()
    assert strong.calls == ()
    assert evaluator.calls == ()


def test_cache_miss_and_binding_rejection_continue_to_observed_execution() -> None:
    """Keep lookup outcomes distinct while observing the real model path."""
    eligible_profile = {
        "task_type": "SUMMARIZATION",
        "complexity": "LOW",
        "input_tokens": 100,
        "risk_tier": "LOW",
        "cache_eligible": True,
        "has_large_context": False,
    }
    source_payload = request_payload(request_profile=eligible_profile)

    miss_observer = InMemoryObservability()
    miss_dependencies, _, _, _ = dependencies((0.93,), observability=miss_observer)
    miss_dependencies = replace(
        miss_dependencies,
        settings=AppSettings(
            semantic_cache_enabled=True,
            context_reduction_enabled=False,
            historical_policy_enabled=False,
        ),
        semantic_cache=FakeSemanticCache((None,)),
    )
    miss_response = TestClient(
        create_app(execution_dependencies=miss_dependencies)
    ).post("/api/v1/runs", json=source_payload)
    miss_cache = next(
        record.outcome
        for record in stage_records(miss_observer)
        if record.name == ObservationStage.SEMANTIC_CACHE_LOOKUP
    )

    rejected_observer = InMemoryObservability()
    rejected_dependencies, _, _, _ = dependencies(
        (0.93,), observability=rejected_observer
    )
    rejected_dependencies = replace(
        rejected_dependencies,
        settings=AppSettings(
            semantic_cache_enabled=True,
            context_reduction_enabled=False,
            historical_policy_enabled=False,
        ),
        semantic_cache=FakeSemanticCache((cache_candidate(source_payload),)),
    )
    rejected_response = TestClient(
        create_app(execution_dependencies=rejected_dependencies)
    ).post(
        "/api/v1/runs",
        json=request_payload(
            input_text="A different request",
            request_profile=eligible_profile,
        ),
    )
    rejected_cache = next(
        record.outcome
        for record in stage_records(rejected_observer)
        if record.name == ObservationStage.SEMANTIC_CACHE_LOOKUP
    )

    assert miss_response.json()["semantic_cache"]["outcome"] == "MISS"
    assert isinstance(miss_cache, CacheStageOutcome)
    assert miss_cache.lookup_result is CacheLookupResult.MISS
    assert ObservationStage.MODEL_GENERATE in {
        record.name for record in stage_records(miss_observer)
    }
    assert rejected_response.json()["semantic_cache"]["outcome"] == "MATCH_REJECTED"
    assert isinstance(rejected_cache, CacheStageOutcome)
    assert rejected_cache.lookup_result is CacheLookupResult.CANDIDATE_FOUND
    assert ObservationStage.MODEL_GENERATE in {
        record.name for record in stage_records(rejected_observer)
    }


def test_selected_context_reduction_creates_one_measured_span() -> None:
    """Observe one actual reduction with authoritative before and after counts."""
    original = "Incident ARC-9 was resolved.\nIncident ARC-9 was resolved."
    observer = InMemoryObservability()
    configured, _, _, _ = dependencies((0.93,), observability=observer)
    reducer = FakeContextReducer((reduction_result(original),))
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=False,
            context_reduction_enabled=True,
            historical_policy_enabled=False,
        ),
        context_reducer=reducer,
        token_counter=RegexTokenCounter(),
        context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            context=original,
            request_profile={
                "task_type": "SUMMARIZATION",
                "complexity": "LOW",
                "input_tokens": 4_000,
                "risk_tier": "LOW",
                "cache_eligible": False,
                "has_large_context": True,
            },
        ),
    )

    reduction = next(
        record.outcome
        for record in stage_records(observer)
        if record.name == ObservationStage.CONTEXT_REDUCTION
    )
    assert response.status_code == 200
    assert isinstance(reduction, ContextStageOutcome)
    assert reduction.status is ObservationStatus.SUCCEEDED
    assert reduction.effective_tokens < reduction.original_tokens
    assert len(reducer.calls) == 1


def test_cache_failure_is_child_failure_but_completed_run() -> None:
    """Keep a cache adapter failure separate from successful model execution."""
    observer = InMemoryObservability()
    configured, small, _, evaluator = dependencies((0.93,), observability=observer)
    cache = FakeSemanticCache((RuntimeError("SECRET_REDIS_RECORD"),))
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=True,
            context_reduction_enabled=False,
            historical_policy_enabled=False,
        ),
        semantic_cache=cache,
    )
    payload = request_payload(
        request_profile={
            "task_type": "SUMMARIZATION",
            "complexity": "LOW",
            "input_tokens": 100,
            "risk_tier": "LOW",
            "cache_eligible": True,
            "has_large_context": False,
        }
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=payload
    )

    cache_record = next(
        record
        for record in stage_records(observer)
        if record.name == ObservationStage.SEMANTIC_CACHE_LOOKUP
    )
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["semantic_cache"]["outcome"] == "LOOKUP_FAILED"
    assert cache_record.outcome is not None
    assert cache_record.outcome.status is ObservationStatus.FAILED
    assert len(small.calls) == len(evaluator.calls) == 1


def test_model_timeout_and_contract_miss_have_distinct_semantics() -> None:
    """Separate system timeout status from a completed business rejection."""
    timeout_observer = InMemoryObservability()
    timeout_provider = TimeoutSmallProvider()
    timeout_dependencies, _, _, _ = dependencies(
        (0.93,),
        observability=timeout_observer,
        small_provider=timeout_provider,
    )
    timeout_response = TestClient(
        create_app(execution_dependencies=timeout_dependencies)
    ).post("/api/v1/runs", json=request_payload())

    timeout_model = next(
        record.outcome
        for record in stage_records(timeout_observer)
        if record.name == ObservationStage.MODEL_GENERATE
    )
    assert timeout_response.json()["status"] == "TIMED_OUT"
    assert isinstance(timeout_model, ModelStageOutcome)
    assert timeout_model.status is ObservationStatus.TIMED_OUT
    assert timeout_model.failure_category is FailureCategory.TIMEOUT

    miss_observer = InMemoryObservability()
    miss_dependencies, _, _, _ = dependencies((0.70, 0.80), observability=miss_observer)
    miss_response = TestClient(
        create_app(execution_dependencies=miss_dependencies)
    ).post("/api/v1/runs", json=request_payload())
    evaluation_records = [
        record
        for record in stage_records(miss_observer)
        if record.name == ObservationStage.EVALUATION_EVALUATE
    ]
    assert miss_response.json()["status"] == "COMPLETED"
    assert miss_response.json()["contract_met"] is False
    assert all(
        outcome.outcome is not None
        and outcome.outcome.status is ObservationStatus.SUCCEEDED
        for outcome in evaluation_records
    )


def test_history_failure_is_separate_from_completed_run() -> None:
    """Observe failed persistence without rewriting terminal execution status."""
    observer = InMemoryObservability()
    configured, _, _, _ = dependencies((0.93,), observability=observer)
    store = FailingHistoryStore()
    configured = replace(configured, run_history_store=store)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )

    history = next(
        record.outcome
        for record in stage_records(observer)
        if record.name == ObservationStage.RUN_HISTORY_SAVE
    )
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.headers["X-OPTIMA-Run-History"] == "FAILED"
    assert isinstance(history, PersistenceStageOutcome)
    assert history.status is ObservationStatus.FAILED
    assert store.save_calls == 1


def test_pre_result_configuration_failure_is_bounded() -> None:
    """Record a safe failure category without fabricating a terminal result."""
    observer = InMemoryObservability()
    configured, small, strong, evaluator = dependencies((0.93,), observability=observer)
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=True,
            context_reduction_enabled=False,
            historical_policy_enabled=False,
        ),
        semantic_cache=None,
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(
            request_profile={
                "task_type": "SUMMARIZATION",
                "complexity": "LOW",
                "input_tokens": 100,
                "risk_tier": "LOW",
                "cache_eligible": True,
                "has_large_context": False,
            }
        ),
    )

    assert response.status_code == 503
    assert observer.projected_run_ids == ()
    assert observer.pre_result_failures == (
        ("run-observability-1", FailureCategory.CONFIGURATION),
    )
    assert small.calls == ()
    assert strong.calls == ()
    assert evaluator.calls == ()


def test_projection_failure_does_not_change_response_or_call_counts() -> None:
    """Contain a recorder failure after result construction and persistence."""
    failing = FailingProjectionObservability()
    configured, small, strong, evaluator = dependencies((0.93,), observability=failing)
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )

    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert len(small.calls) == 1
    assert len(strong.calls) == 0
    assert len(evaluator.calls) == 1
    assert failing.run.exit_calls == 1


def test_otel_spans_are_parented_and_privacy_safe() -> None:
    """Export one safe server/run hierarchy without user or secret material."""
    observer, exporter, reader, _, _ = local_otel_observer()
    configured, _, _, _ = dependencies((0.93,), observability=observer)
    application = create_app(execution_dependencies=configured)

    response = TestClient(application).post(
        "/api/v1/runs?user_id=SECRET_QUERY",
        json=request_payload(),
        headers={
            "Authorization": "Bearer SECRET_TOKEN",
            "X-API-Key": "SECRET_API_KEY",
            "Cookie": "session=SECRET_COOKIE",
        },
    )

    spans = exporter.get_finished_spans()
    server_spans = [span for span in spans if span.kind is SpanKind.SERVER]
    root = next(span for span in spans if span.name == "optima.run")
    children = [
        span for span in spans if span.name.startswith("optima.") and span is not root
    ]
    serialized = repr(
        [
            (
                span.name,
                dict(span.attributes or {}),
                [(event.name, dict(event.attributes or {})) for event in span.events],
                span.status.status_code,
            )
            for span in spans
        ]
    )
    points = metric_points(reader)
    metric_serialized = repr([(name, dict(point.attributes)) for name, point in points])

    assert response.status_code == 200
    assert len(server_spans) == 1
    assert server_spans[0].name == "POST /api/v1/runs"
    assert root.parent is not None
    assert root.parent.span_id == server_spans[0].context.span_id
    assert root.status.status_code is StatusCode.OK
    assert all(
        span.parent is not None and span.parent.span_id == root.context.span_id
        for span in children
    )
    for secret in (
        "SECRET_PROMPT",
        "SECRET_CONTEXT",
        "SECRET_REFERENCE",
        "SECRET_CRITERION",
        "SECRET_METADATA",
        "SECRET_MODEL_OUTPUT",
        "SECRET_QUERY",
        "SECRET_TOKEN",
        "SECRET_API_KEY",
        "SECRET_COOKIE",
        _CONNECTION_STRING,
    ):
        assert secret not in serialized
        assert secret not in metric_serialized
    assert "authorization" not in serialized.lower()
    assert "cookie" not in serialized.lower()
    assert "url.query" not in serialized.lower()
    assert "url.full" not in serialized.lower()
    assert all(
        "optima.run.id" not in point.attributes
        and "optima.correlation.id" not in point.attributes
        and "optima.provider.request_id" not in point.attributes
        for _, point in points
    )

    observer.close()
    observer.close()


def test_metric_schema_uses_only_bounded_dimensions() -> None:
    """Lock the names and dimension keys emitted for a successful small run."""
    observer, _, reader, _, _ = local_otel_observer()
    configured, _, _, _ = dependencies((0.93,), observability=observer)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )

    points = metric_points(reader)
    attributes_by_name: dict[str, set[frozenset[str]]] = {}
    for name, point in points:
        attributes_by_name.setdefault(name, set()).add(frozenset(point.attributes))
    assert response.status_code == 200
    assert set(attributes_by_name) == {
        "optima.runs",
        "optima.run.duration",
        "optima.model.attempts",
        "optima.model.duration",
        "optima.tokens",
        "optima.quality_contract.results",
        "optima.evaluation.score",
        "optima.telemetry.projections",
    }
    assert attributes_by_name["optima.runs"] == {
        frozenset({"optima.run.status", "optima.plan.family"})
    }
    assert (
        attributes_by_name["optima.run.duration"] == attributes_by_name["optima.runs"]
    )
    assert attributes_by_name["optima.model.attempts"] == {
        frozenset({"optima.model.role", "optima.model.result"})
    }
    assert (
        attributes_by_name["optima.model.duration"]
        == attributes_by_name["optima.model.attempts"]
    )
    assert attributes_by_name["optima.tokens"] == {
        frozenset({"optima.model.role", "optima.token.category"})
    }
    assert attributes_by_name["optima.quality_contract.results"] == {
        frozenset({"optima.contract.result"})
    }
    assert attributes_by_name["optima.evaluation.score"] == {
        frozenset({"optima.evaluation.result"})
    }
    assert attributes_by_name["optima.telemetry.projections"] == {
        frozenset({"optima.telemetry.projection_result"})
    }


def test_health_route_is_excluded_from_http_tracing() -> None:
    """Avoid routine health-check volume in the explicit server instrumentation."""
    observer, exporter, _, _, _ = local_otel_observer()
    configured, _, _, _ = dependencies((0.93,), observability=observer)

    response = TestClient(create_app(execution_dependencies=configured)).get(
        "/api/v1/health"
    )

    assert response.status_code == 200
    assert exporter.get_finished_spans() == ()


def test_business_rejection_and_history_failure_have_separate_otel_statuses() -> None:
    """Keep completed contract misses successful while marking persistence failure."""
    observer, exporter, _, _, _ = local_otel_observer()
    configured, _, _, _ = dependencies((0.70, 0.80), observability=observer)
    configured = replace(configured, run_history_store=FailingHistoryStore())

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )

    spans = exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "optima.run")
    evaluations = [
        span for span in spans if span.name == ObservationStage.EVALUATION_EVALUATE
    ]
    history = next(
        span for span in spans if span.name == ObservationStage.RUN_HISTORY_SAVE
    )
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["contract_met"] is False
    assert root.status.status_code is StatusCode.OK
    assert all(span.status.status_code is StatusCode.OK for span in evaluations)
    assert history.status.status_code is StatusCode.ERROR


def test_unsampled_traces_do_not_suppress_terminal_metrics() -> None:
    """Apply trace sampling consistently while retaining unsampled metrics."""
    observer, exporter, reader, _, _ = local_otel_observer(ALWAYS_OFF)
    configured, _, _, _ = dependencies((0.93,), observability=observer)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )

    assert response.status_code == 200
    assert exporter.get_finished_spans() == ()
    points = metric_points(reader)
    assert any(name == "optima.runs" for name, _ in points)
    assert any(name == "optima.telemetry.projections" for name, _ in points)


def test_parent_based_sampling_preserves_remote_trace_decisions() -> None:
    """Respect sampled and unsampled W3C parent flags across every child span."""
    unsampled, unsampled_exporter, unsampled_reader, _, _ = local_otel_observer(
        ParentBased(ALWAYS_ON)
    )
    unsampled_dependencies, _, _, _ = dependencies((0.93,), observability=unsampled)
    unsampled_response = TestClient(
        create_app(execution_dependencies=unsampled_dependencies)
    ).post(
        "/api/v1/runs",
        json=request_payload(),
        headers={
            "traceparent": ("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00")
        },
    )

    sampled, sampled_exporter, _, _, _ = local_otel_observer(ParentBased(ALWAYS_OFF))
    sampled_dependencies, _, _, _ = dependencies((0.93,), observability=sampled)
    sampled_response = TestClient(
        create_app(execution_dependencies=sampled_dependencies)
    ).post(
        "/api/v1/runs",
        json=request_payload(),
        headers={
            "traceparent": ("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
        },
    )

    assert unsampled_response.status_code == 200
    assert unsampled_exporter.get_finished_spans() == ()
    assert any(name == "optima.runs" for name, _ in metric_points(unsampled_reader))
    assert sampled_response.status_code == 200
    sampled_spans = sampled_exporter.get_finished_spans()
    assert any(span.kind is SpanKind.SERVER for span in sampled_spans)
    assert any(span.name == "optima.run" for span in sampled_spans)


def test_unavailable_measurements_are_not_emitted_as_zero() -> None:
    """Omit unavailable token, cost, and evaluation measurements."""
    observer, exporter, reader, _, _ = local_otel_observer()
    configured, _, _, _ = dependencies(
        (0.93,),
        observability=observer,
        small_provider=TimeoutSmallProvider(),
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )

    root = next(
        span for span in exporter.get_finished_spans() if span.name == "optima.run"
    )
    metric_names = {name for name, _ in metric_points(reader)}
    attributes = dict(root.attributes or {})
    assert response.json()["status"] == "TIMED_OUT"
    assert attributes["optima.measurement.total_tokens.available"] is False
    assert attributes["optima.measurement.total_cost.available"] is False
    assert attributes["optima.measurement.final_evaluation.available"] is False
    assert "optima.run.total_tokens" not in attributes
    assert "optima.run.total_cost_exact" not in attributes
    assert "optima.tokens" not in metric_names
    assert "optima.evaluation.score" not in metric_names
    assert not any(name.startswith("optima.cost") for name in metric_names)


def test_terminal_projection_and_close_are_emit_once() -> None:
    """Ignore repeated terminal projection and cleanup requests."""
    seed = InMemoryObservability()
    configured, _, _, _ = dependencies((0.93,), observability=seed)
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )
    result = run_result_from_response(response)
    observer, exporter, reader, _, _ = local_otel_observer()
    run = observer.start_run(
        run_id=result.run_id,
        correlation_id=result.correlation_id,
    )
    run.__enter__()
    run.project_result(result)
    run.project_result(result)
    run.__exit__(None, None, None)
    run.__exit__(None, None, None)

    spans = exporter.get_finished_spans()
    assert sum(span.name == "optima.run" for span in spans) == 1
    assert sum(span.name == ObservationStage.OUTCOME_PROJECT for span in spans) == 1
    run_points = [
        point for name, point in metric_points(reader) if name == "optima.runs"
    ]
    assert len(run_points) == 1
    assert run_points[0].value == 1


def test_force_flush_shares_one_timeout_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass only the remaining deadline budget to the second provider."""
    times = iter((10.0, 10.0, 10.6))
    monkeypatch.setattr(
        "optima.observability.opentelemetry.monotonic",
        lambda: next(times),
    )

    class FlushProvider:
        def __init__(self) -> None:
            self.timeouts: list[int] = []

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            self.timeouts.append(timeout_millis)
            return True

        def shutdown(self) -> None:
            return None

    tracer = FlushProvider()
    meter = FlushProvider()
    tracer_provider = TracerProvider()
    meter_provider = MeterProvider()

    observer = OpenTelemetryObservability(
        tracer=tracer_provider.get_tracer("optima.flush", "1"),
        meter=meter_provider.get_meter("optima.flush", "1"),
        tracer_provider=tracer,
        meter_provider=meter,
    )

    assert observer.force_flush(1_000) is True
    assert tracer.timeouts == [1_000]
    assert meter.timeouts == [400]


def test_normal_close_shuts_providers_before_restoring_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the export-time privacy guard active through provider shutdown."""
    flag = "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED"
    monkeypatch.setenv(flag, "true")
    shutdown_guards: list[str | None] = []

    class CloseProvider:
        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

        def shutdown(self) -> None:
            shutdown_guards.append(os.environ.get(flag))

    tracer_provider = TracerProvider()
    meter_provider = MeterProvider()

    def restore_guard() -> None:
        os.environ.pop(flag, None)

    observer = OpenTelemetryObservability(
        tracer=tracer_provider.get_tracer("optima.close", "1"),
        meter=meter_provider.get_meter("optima.close", "1"),
        tracer_provider=CloseProvider(),
        meter_provider=CloseProvider(),
        close_callbacks=(restore_guard,),
    )

    observer.close()
    observer.close()

    assert shutdown_guards == ["true", "true"]
    assert flag not in os.environ


def test_in_memory_projection_rejects_cross_run_identity() -> None:
    """Make the deterministic recorder expose cross-run projection defects."""
    seed = InMemoryObservability()
    configured, _, _, _ = dependencies((0.93,), observability=seed)
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )
    result = run_result_from_response(response)
    observer = InMemoryObservability()
    run = observer.start_run(
        run_id=result.run_id,
        correlation_id=result.correlation_id,
    )

    with run, pytest.raises(ValueError, match="identity"):
        run.project_result(
            result.model_copy(update={"correlation_id": "correlation-other"})
        )

    assert observer.projected_run_ids == ()


def test_in_memory_failure_prevents_terminal_projection() -> None:
    """Do not record contradictory pre-result and terminal evidence."""
    seed = InMemoryObservability()
    configured, _, _, _ = dependencies((0.93,), observability=seed)
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )
    result = run_result_from_response(response)
    observer = InMemoryObservability()
    with observer.start_run(
        run_id=result.run_id,
        correlation_id=result.correlation_id,
    ) as run:
        run.record_pre_result_failure(FailureCategory.VALIDATION)
        run.project_result(result)

    assert observer.projected_run_ids == ()
    assert all(
        record.name != ObservationStage.OUTCOME_PROJECT
        for record in observer.observations
    )


def test_repeated_app_construction_adds_one_http_middleware_per_app() -> None:
    """Avoid duplicate HTTP instrumentation when composition is repeated."""
    observer, _, _, _, _ = local_otel_observer()
    configured, _, _, _ = dependencies((0.93,), observability=observer)

    first = create_app(execution_dependencies=configured)
    second = create_app(execution_dependencies=configured)
    observer.instrument_fastapi(first)
    observer.instrument_fastapi(second)

    assert (
        sum(
            getattr(middleware.cls, "__name__", None)
            == "PrivacySafeOpenTelemetryMiddleware"
            for middleware in first.user_middleware
        )
        == 1
    )
    assert (
        sum(
            getattr(middleware.cls, "__name__", None)
            == "PrivacySafeOpenTelemetryMiddleware"
            for middleware in second.user_middleware
        )
        == 1
    )
