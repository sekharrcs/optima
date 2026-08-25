"""Offline proofs for OPTIMA tracing and operational metrics."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
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
    TraceIdRatioBased,
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
    StageOutcome,
)
from optima.observability import azure_monitor as azure_monitor_module
from optima.observability.azure_monitor import (
    AzureMonitorRuntimeRegistry,
    build_observability,
)
from optima.observability.noop import NO_OP_STAGE
from optima.observability.opentelemetry import OpenTelemetryObservability
from optima.observability.resilient import (
    FailureIsolatedRunObservation,
    FailureIsolatedStageObservation,
)
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
    cost_calculator: CostCalculator | None = None,
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
    calculator = cost_calculator or CostCalculator(
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


def run_result_with_model_cost(result: RunResult, cost: Decimal) -> RunResult:
    """Revalidate a terminal result with one exact model-cost representation."""
    payload = result.model_dump(
        exclude={
            "total_input_tokens",
            "total_output_tokens",
            "total_tokens",
            "total_calculated_cost",
            "total_cost_provenance",
        }
    )
    usage = result.model_usages[0].model_dump()
    usage["calculated_cost"] = cost
    payload["model_usages"] = [usage]
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


def test_closed_runtime_registry_rejects_reconstruction() -> None:
    """Do not return a process-global runtime after its owner has closed it."""
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
    observer = build_observability(
        settings,
        registry=registry,
        initializer=initialize,
    )
    observer.close()

    with pytest.raises(RuntimeError, match="registry is closed"):
        build_observability(
            settings,
            registry=registry,
            initializer=initialize,
        )

    assert len(calls) == 1


def test_runtime_leases_close_shared_runtime_after_last_owner() -> None:
    """Keep a shared runtime alive until every equivalent composition closes."""
    registry = AzureMonitorRuntimeRegistry()
    runtimes: list[InMemoryObservability] = []

    def initialize(
        configuration: ApplicationInsightsConfiguration,
    ) -> InMemoryObservability:
        runtime = InMemoryObservability()
        runtimes.append(runtime)
        return runtime

    settings = AppSettings(
        application_insights_enabled=True,
        application_insights_connection_string=SecretStr(_CONNECTION_STRING),
    )
    first = build_observability(settings, registry=registry, initializer=initialize)
    second = build_observability(settings, registry=registry, initializer=initialize)

    first.close()
    first.close()
    assert runtimes[0]._closed is False
    assert second.force_flush() is True

    second.close()
    assert runtimes[0]._closed is True
    with pytest.raises(RuntimeError, match="registry is closed"):
        build_observability(settings, registry=registry, initializer=initialize)
    assert len(runtimes) == 1


def test_runtime_initializer_failure_is_cached_as_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contain startup failure, report it safely once, and never retry."""
    registry = AzureMonitorRuntimeRegistry()
    calls = 0

    def fail(configuration: ApplicationInsightsConfiguration) -> InMemoryObservability:
        nonlocal calls
        calls += 1
        raise RuntimeError(f"SECRET_INITIALIZER_FAILURE {_CONNECTION_STRING}")

    settings = AppSettings(
        application_insights_enabled=True,
        application_insights_connection_string=SecretStr(_CONNECTION_STRING),
    )

    first = build_observability(settings, registry=registry, initializer=fail)
    second = build_observability(settings, registry=registry, initializer=fail)

    assert first.force_flush() is False
    assert second.force_flush() is False
    assert calls == 1
    assert caplog.text.count("Application Insights initialization failed") == 1
    assert "SECRET_INITIALIZER_FAILURE" not in caplog.text
    assert _CONNECTION_STRING not in caplog.text


class TrackingAzureExporter:
    """Capture direct exporter options and lifecycle without network activity."""

    def __init__(self, **options: Any) -> None:
        self.options = options
        self.shutdown_calls = 0

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self, *args: object, **kwargs: object) -> None:
        self.shutdown_calls += 1


class TrackingSpanProcessor:
    """Own one fake trace exporter like an SDK span processor."""

    def __init__(self, exporter: TrackingAzureExporter, **options: Any) -> None:
        self.exporter = exporter
        self.options = options
        self.shutdown_calls = 0

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.exporter.shutdown()


class TrackingMetricReader:
    """Own one fake metric exporter like an SDK metric reader."""

    def __init__(self, exporter: TrackingAzureExporter, **options: Any) -> None:
        self.exporter = exporter
        self.options = options
        self.shutdown_calls = 0

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.exporter.shutdown()


class TrackingInternalMeterProvider:
    """Private meter provider used only for SDK self-observation."""


class TrackingLocalTracerProvider:
    """Provide a real local tracer while exposing owned lifecycle calls."""

    def __init__(
        self,
        *,
        sampler: Sampler,
        resource: Resource,
        **options: Any,
    ) -> None:
        self.resource = resource
        self.options = options
        self.shutdown_calls = 0
        self.processors: list[TrackingSpanProcessor] = []
        self._provider = TracerProvider(sampler=sampler, resource=resource)

    def add_span_processor(self, processor: TrackingSpanProcessor) -> None:
        self.processors.append(processor)

    def get_tracer(self, name: str, version: str) -> Any:
        return self._provider.get_tracer(name, version)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return all(
            processor.force_flush(timeout_millis) for processor in self.processors
        )

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        for processor in self.processors:
            processor.shutdown()
        self._provider.shutdown()


class TrackingLocalMeterProvider:
    """Provide a real local meter while exposing owned lifecycle calls."""

    def __init__(
        self,
        *,
        metric_readers: list[TrackingMetricReader],
        resource: Resource,
        **options: Any,
    ) -> None:
        self.resource = resource
        self.readers = metric_readers
        self.options = options
        self.shutdown_calls = 0
        self._provider = MeterProvider(resource=resource)

    def get_meter(self, name: str, version: str) -> Any:
        return self._provider.get_meter(name, version)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return all(reader.force_flush(timeout_millis) for reader in self.readers)

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        for reader in self.readers:
            reader.shutdown()
        self._provider.shutdown()


def tracking_azure_components(state: dict[str, list[Any]]) -> Any:
    """Build direct runtime factories whose created components are inspectable."""

    def capture(name: str, factory: Any) -> Any:
        def build(*args: object, **kwargs: object) -> Any:
            component = factory(*args, **kwargs)
            state.setdefault(name, []).append(component)
            return component

        return build

    return azure_monitor_module._AzureRuntimeComponents(
        trace_exporter_factory=capture("trace_exporters", TrackingAzureExporter),
        metric_exporter_factory=capture("metric_exporters", TrackingAzureExporter),
        span_processor_factory=capture("span_processors", TrackingSpanProcessor),
        metric_reader_factory=capture("metric_readers", TrackingMetricReader),
        tracer_provider_factory=capture(
            "tracer_providers", TrackingLocalTracerProvider
        ),
        meter_provider_factory=capture("meter_providers", TrackingLocalMeterProvider),
        internal_meter_provider_factory=capture(
            "internal_meter_providers", TrackingInternalMeterProvider
        ),
        resource_factory=lambda *, attributes: Resource(attributes),
        sampler_factory=lambda ratio: ParentBased(TraceIdRatioBased(ratio)),
        span_limits_factory=object,
        exemplar_filter_factory=object,
    )


def test_direct_initializer_uses_only_explicit_local_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build exact resources and exporters without touching host globals or env."""
    from opentelemetry import metrics, trace

    host_tracer_provider = trace.get_tracer_provider()
    host_meter_provider = metrics.get_meter_provider()
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "secret.attribute=SECRET_RESOURCE")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "SECRET_SERVICE")
    environment_before = os.environ.copy()
    state: dict[str, list[Any]] = {}

    observer = azure_monitor_module._initialize_azure_monitor(
        ApplicationInsightsConfiguration(
            connection_string=SecretStr(_CONNECTION_STRING),
            service_name="optima-test",
            service_version="1.2.3",
            deployment_environment="test",
            sampling_ratio=0.25,
        ),
        components=tracking_azure_components(state),
    )

    expected_resource = {
        "service.name": "optima-test",
        "service.version": "1.2.3",
        "deployment.environment.name": "test",
    }
    assert dict(state["tracer_providers"][0].resource.attributes) == expected_resource
    assert dict(state["meter_providers"][0].resource.attributes) == expected_resource
    assert trace.get_tracer_provider() is host_tracer_provider
    assert metrics.get_meter_provider() is host_meter_provider
    assert os.environ.copy() == environment_before
    internal_meter = state["internal_meter_providers"][0]
    span_processor = state["span_processors"][0]
    metric_reader = state["metric_readers"][0]
    tracer_provider = state["tracer_providers"][0]
    meter_provider = state["meter_providers"][0]
    assert span_processor.options == {
        "max_queue_size": 2_048,
        "schedule_delay_millis": 5_000,
        "max_export_batch_size": 512,
        "export_timeout_millis": 30_000,
        "meter_provider": internal_meter,
    }
    assert metric_reader.options == {
        "export_interval_millis": 60_000,
        "export_timeout_millis": 30_000,
    }
    assert tracer_provider.options["shutdown_on_exit"] is False
    assert tracer_provider.options["meter_provider"] is internal_meter
    assert "span_limits" in tracer_provider.options
    assert meter_provider.options["shutdown_on_exit"] is False
    assert "exemplar_filter" in meter_provider.options
    for exporter in (
        state["trace_exporters"][0],
        state["metric_exporters"][0],
    ):
        assert exporter.options["connection_string"] == _CONNECTION_STRING
        assert exporter.options["disable_offline_storage"] is True
        assert exporter.options["retry_total"] == 0
        assert exporter.options["retry_connect"] == 0
        assert exporter.options["retry_read"] == 0
        assert exporter.options["retry_status"] == 0
        assert exporter.options["redirect_max"] == 0
        assert exporter.options["instrumentation_collection"] is True

    observer.close()
    assert state["tracer_providers"][0].shutdown_calls == 1
    assert state["meter_providers"][0].shutdown_calls == 1
    assert state["trace_exporters"][0].shutdown_calls == 1
    assert state["metric_exporters"][0].shutdown_calls == 1


def test_direct_initializer_cleans_only_created_components_on_failure() -> None:
    """Close an already-created exporter when the next owned factory fails."""
    state: dict[str, list[Any]] = {}
    components = tracking_azure_components(state)

    def fail_metric_exporter(**kwargs: Any) -> None:
        raise RuntimeError("SECRET_METRIC_CONSTRUCTION")

    components = replace(components, metric_exporter_factory=fail_metric_exporter)

    with pytest.raises(RuntimeError, match="SECRET_METRIC_CONSTRUCTION"):
        azure_monitor_module._initialize_azure_monitor(
            ApplicationInsightsConfiguration(
                connection_string=SecretStr(_CONNECTION_STRING)
            ),
            components=components,
        )

    assert state["trace_exporters"][0].shutdown_calls == 1
    assert "tracer_providers" not in state
    assert "meter_providers" not in state


def test_direct_initializer_cancellation_cleans_owned_components() -> None:
    """Propagate cancellation after closing components created before it."""
    state: dict[str, list[Any]] = {}
    components = tracking_azure_components(state)

    def cancel_metric_exporter(**kwargs: Any) -> None:
        raise asyncio.CancelledError

    components = replace(components, metric_exporter_factory=cancel_metric_exporter)

    with pytest.raises(asyncio.CancelledError):
        azure_monitor_module._initialize_azure_monitor(
            ApplicationInsightsConfiguration(
                connection_string=SecretStr(_CONNECTION_STRING)
            ),
            components=components,
        )

    assert state["trace_exporters"][0].shutdown_calls == 1


def test_direct_initializer_cleans_complete_runtime_if_adapter_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close both local providers if observer construction fails last."""
    state: dict[str, list[Any]] = {}
    monkeypatch.setattr(
        azure_monitor_module,
        "OpenTelemetryObservability",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("SECRET_ADAPTER_CONSTRUCTION")
        ),
    )

    with pytest.raises(RuntimeError, match="SECRET_ADAPTER_CONSTRUCTION"):
        azure_monitor_module._initialize_azure_monitor(
            ApplicationInsightsConfiguration(
                connection_string=SecretStr(_CONNECTION_STRING)
            ),
            components=tracking_azure_components(state),
        )

    assert state["tracer_providers"][0].shutdown_calls == 1
    assert state["meter_providers"][0].shutdown_calls == 1
    assert state["trace_exporters"][0].shutdown_calls == 1
    assert state["metric_exporters"][0].shutdown_calls == 1


def test_direct_initializers_serialize_without_mutating_host_state() -> None:
    """Serialize owned construction while leaving concurrent host state unchanged."""
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()
    errors: list[BaseException] = []
    observers: list[Any] = []

    def initialize(name: str) -> None:
        state: dict[str, list[Any]] = {}
        components = tracking_azure_components(state)
        original_trace_factory = components.trace_exporter_factory

        def trace_exporter(**kwargs: Any) -> Any:
            if name == "first":
                first_entered.set()
                assert release_first.wait(2)
            else:
                second_entered.set()
            return original_trace_factory(**kwargs)

        try:
            observers.append(
                azure_monitor_module._initialize_azure_monitor(
                    ApplicationInsightsConfiguration(
                        connection_string=SecretStr(_CONNECTION_STRING)
                    ),
                    components=replace(
                        components,
                        trace_exporter_factory=trace_exporter,
                    ),
                )
            )
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=initialize, args=("first",))

    def start_second() -> None:
        second_started.set()
        initialize("second")

    second = threading.Thread(target=start_second)
    first.start()
    assert first_entered.wait(2)
    second.start()
    assert second_started.wait(2)
    assert second_entered.wait(0.05) is False
    release_first.set()
    first.join(2)
    second.join(2)

    assert errors == []
    assert second_entered.is_set()
    for observer in observers:
        observer.close()


def test_sdk_log_suppression_covers_last_resort_and_preserves_host_thread() -> None:
    """Drop owned raw SDK logs while retaining concurrent host SDK diagnostics."""
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_last_resort = logging.lastResort
    output = StringIO()
    handler = logging.StreamHandler(output)
    root.handlers.clear()
    logging.lastResort = handler
    try:
        with azure_monitor_module._suppress_current_thread_sdk_logs():
            logging.getLogger("azure.optima.new").error("SECRET_RAW_SDK_FAILURE")
            host_thread = threading.Thread(
                target=lambda: logging.getLogger("azure.host.new").error(
                    "HOST_SDK_DIAGNOSTIC"
                )
            )
            host_thread.start()
            host_thread.join(2)
            assert not host_thread.is_alive()
    finally:
        root.handlers[:] = previous_handlers
        logging.lastResort = previous_last_resort

    assert "SECRET_RAW_SDK_FAILURE" not in output.getvalue()
    assert "HOST_SDK_DIAGNOSTIC" in output.getvalue()


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


def test_otel_cache_hit_has_no_model_or_evaluator_spans() -> None:
    """Export only the cache path and terminal projection for a reused result."""
    observer, exporter, reader, _, _ = local_otel_observer()
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
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=True,
            context_reduction_enabled=False,
            historical_policy_enabled=False,
        ),
        semantic_cache=FakeSemanticCache((cache_candidate(payload),)),
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=payload
    )

    spans = exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "optima.run")
    names = {span.name for span in spans}
    assert response.status_code == 200
    assert ObservationStage.SEMANTIC_CACHE_LOOKUP in names
    assert ObservationStage.MODEL_GENERATE not in names
    assert ObservationStage.EVALUATION_EVALUATE not in names
    outcome = next(
        span for span in spans if span.name == ObservationStage.OUTCOME_PROJECT
    )
    assert outcome.parent is not None
    assert outcome.parent.span_id == root.context.span_id
    assert any(name == "optima.cache.lookups" for name, _ in metric_points(reader))
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


def test_projection_failure_does_not_change_response_or_call_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
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
    assert caplog.text.count("Terminal telemetry projection failed") == 1
    assert "SECRET_EXPORTER_FAILURE" not in caplog.text


def test_otel_spans_are_parented_and_privacy_safe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Export one safe server/run hierarchy without user or secret material."""
    observer, exporter, reader, _, _ = local_otel_observer()
    small = build_fake_small_provider(
        provider_name="fake",
        deployment_name="small",
        responses=(
            FakeProviderResponse(
                output_text="SECRET_MODEL_OUTPUT SECRET_RESPONSE_BODY",
                input_tokens=100,
                output_tokens=20,
            ),
        ),
        clock=IncrementingClock(),
    )
    configured, _, _, _ = dependencies(
        (0.93,), observability=observer, small_provider=small
    )
    application = create_app(execution_dependencies=configured)

    response = TestClient(application).post(
        "/api/v1/runs?user_id=SECRET_QUERY",
        json=request_payload(
            context=(
                "SECRET_CONTEXT SECRET_ENDPOINT SECRET_VECTOR "
                "SECRET_REQUEST_BODY SECRET_RAW_EXCEPTION"
            ),
        ),
        headers={
            "Authorization": "Bearer SECRET_ACCESS_TOKEN",
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
                (span.status.status_code, span.status.description),
                dict(span.resource.attributes),
            )
            for span in spans
        ]
    )
    points = metric_points(reader)
    metric_serialized = repr([(name, dict(point.attributes)) for name, point in points])
    metric_data = reader.get_metrics_data()
    metric_resources = repr(
        []
        if metric_data is None
        else [
            dict(resource_metrics.resource.attributes)
            for resource_metrics in metric_data.resource_metrics
        ]
    )

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
        "SECRET_RESPONSE_BODY",
        "SECRET_QUERY",
        "SECRET_ACCESS_TOKEN",
        "SECRET_API_KEY",
        "SECRET_COOKIE",
        "SECRET_ENDPOINT",
        "SECRET_VECTOR",
        "SECRET_REQUEST_BODY",
        "SECRET_RAW_EXCEPTION",
        _CONNECTION_STRING,
    ):
        assert secret not in serialized
        assert secret not in metric_serialized
        assert secret not in metric_resources
        assert secret not in caplog.text
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
    observer, exporter, reader, _, _ = local_otel_observer()
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


def test_otel_escalation_exports_small_and_strong_attempts() -> None:
    """Export both actual model attempts and one escalation in the same run tree."""
    observer, exporter, reader, _, _ = local_otel_observer()
    configured, small, strong, evaluator = dependencies(
        (0.70, 0.95), observability=observer
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )

    spans = exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "optima.run")
    model_spans = [
        span for span in spans if span.name == ObservationStage.MODEL_GENERATE
    ]
    evaluation_spans = [
        span for span in spans if span.name == ObservationStage.EVALUATION_EVALUATE
    ]
    assert response.json()["escalated"] is True
    assert [
        dict(span.attributes or {})["optima.model.role"] for span in model_spans
    ] == [
        "SMALL",
        "STRONG",
    ]
    assert [
        dict(span.attributes or {})["optima.model.role"] for span in evaluation_spans
    ] == [
        "SMALL",
        "STRONG",
    ]
    assert all(
        span.parent is not None and span.parent.span_id == root.context.span_id
        for span in (*model_spans, *evaluation_spans)
    )
    escalation_points = [
        point for name, point in metric_points(reader) if name == "optima.escalations"
    ]
    assert len(escalation_points) == 1
    assert escalation_points[0].value == 1
    assert len(small.calls) == len(strong.calls) == 1
    assert len(evaluator.calls) == 2


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


def test_incomplete_pricing_omits_exact_cost_attribute() -> None:
    """Do not expose a partial cost when a successful call has no catalog entry."""
    observer, exporter, _, _, _ = local_otel_observer()
    calculator = CostCalculator(
        PriceCatalog(
            version="missing-small-v1",
            currency="TEST",
            entries=(
                PriceCatalogEntry(
                    provider="other-provider",
                    deployment="other-deployment",
                    input_rate_per_million_tokens=Decimal("1"),
                    output_rate_per_million_tokens=Decimal("1"),
                ),
            ),
        )
    )
    configured, _, _, _ = dependencies(
        (0.93,),
        observability=observer,
        cost_calculator=calculator,
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )

    result = run_result_from_response(response)
    root = next(
        span for span in exporter.get_finished_spans() if span.name == "optima.run"
    )
    attributes = dict(root.attributes or {})
    assert response.status_code == 200
    assert result.total_calculated_cost is None
    assert attributes["optima.measurement.total_cost.available"] is False
    assert "optima.run.total_cost_exact" not in attributes


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
        def __init__(self, *, fail: bool) -> None:
            self._fail = fail

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

        def shutdown(self) -> None:
            shutdown_guards.append(os.environ.get(flag))
            if self._fail:
                raise RuntimeError("SECRET_PROVIDER_SHUTDOWN")

    tracer_provider = TracerProvider()
    meter_provider = MeterProvider()

    def restore_guard() -> None:
        os.environ.pop(flag, None)

    observer = OpenTelemetryObservability(
        tracer=tracer_provider.get_tracer("optima.close", "1"),
        meter=meter_provider.get_meter("optima.close", "1"),
        tracer_provider=CloseProvider(fail=True),
        meter_provider=CloseProvider(fail=False),
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


@pytest.mark.parametrize(
    ("terminal_cost", "expected"),
    (
        (Decimal("0"), "0"),
        (Decimal("0.00"), "0"),
        (Decimal("7.5E-7"), "0.00000075"),
        (Decimal("0.0100"), "0.01"),
        (
            Decimal("0.1234567890123456789012345678"),
            "0.1234567890123456789012345678",
        ),
        (Decimal("1E+3"), "1000"),
        (Decimal("1E+40"), "1" + "0" * 40),
    ),
)
def test_total_cost_exact_is_numerically_canonical_fixed_point(
    terminal_cost: Decimal,
    expected: str,
) -> None:
    """Export one exact numerical form without float or scientific notation."""
    seed = InMemoryObservability()
    configured, _, _, _ = dependencies((0.93,), observability=seed)
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )
    result = run_result_with_model_cost(
        run_result_from_response(response),
        terminal_cost,
    )
    observer, exporter, _, _, _ = local_otel_observer()

    with observer.start_run(
        run_id=result.run_id,
        correlation_id=result.correlation_id,
    ) as run:
        run.project_result(result)

    root = next(
        span for span in exporter.get_finished_spans() if span.name == "optima.run"
    )
    cost_attribute = dict(root.attributes or {})["optima.run.total_cost_exact"]
    assert cost_attribute == expected
    assert isinstance(cost_attribute, str)
    assert "E" not in cost_attribute
    assert "e" not in cost_attribute
    assert Decimal(cost_attribute) == result.total_calculated_cost


def test_total_cost_exact_omitted_when_exponent_width_is_unbounded() -> None:
    """Omit the exact cost rather than expand an extreme-exponent Decimal."""
    seed = InMemoryObservability()
    configured, _, _, _ = dependencies((0.93,), observability=seed)
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )
    result = run_result_with_model_cost(
        run_result_from_response(response),
        Decimal("1E+100000"),
    )
    assert result.total_calculated_cost == Decimal("1E+100000")
    observer, exporter, _, _, _ = local_otel_observer()

    with observer.start_run(
        run_id=result.run_id,
        correlation_id=result.correlation_id,
    ) as run:
        run.project_result(result)

    root = next(
        span for span in exporter.get_finished_spans() if span.name == "optima.run"
    )
    attributes = dict(root.attributes or {})
    assert "optima.run.total_cost_exact" not in attributes
    assert attributes["optima.measurement.total_cost.available"] is True


def test_concurrent_runs_keep_isolated_span_parentage() -> None:
    """Prove async runs never inherit another run's telemetry context."""
    observer, exporter, _, _, _ = local_otel_observer()

    async def drive(run_id: str) -> None:
        with observer.start_run(run_id=run_id, correlation_id=f"corr-{run_id}") as run:
            with run.start_stage(ObservationStage.PLANNER_SELECT) as stage:
                await asyncio.sleep(0)
                stage.finish(StageOutcome(status=ObservationStatus.SUCCEEDED))
            with run.start_stage(ObservationStage.MODEL_GENERATE) as stage:
                await asyncio.sleep(0)
                stage.finish(
                    ModelStageOutcome(
                        status=ObservationStatus.SUCCEEDED,
                        model_role=ModelRole.SMALL,
                        latency_ms=1,
                    )
                )

    async def main() -> None:
        await asyncio.gather(drive("run-a"), drive("run-b"))

    asyncio.run(main())

    spans = exporter.get_finished_spans()
    roots = {
        dict(span.attributes or {})["optima.run.id"]: span
        for span in spans
        if span.name == "optima.run"
    }
    assert set(roots) == {"run-a", "run-b"}
    assert roots["run-a"].context.trace_id != roots["run-b"].context.trace_id
    for root in roots.values():
        children = [
            span
            for span in spans
            if span.name != "optima.run"
            and span.context.trace_id == root.context.trace_id
        ]
        other_span_ids = {
            span.context.span_id
            for span in spans
            if span.context.trace_id != root.context.trace_id
        }
        assert len(children) == 2
        assert all(
            child.parent is not None and child.parent.span_id == root.context.span_id
            for child in children
        )
        assert all(
            child.parent is not None and child.parent.span_id not in other_span_ids
            for child in children
        )


def test_concurrent_http_requests_have_distinct_trace_trees() -> None:
    """Keep each concurrent server request, run, and stage hierarchy isolated."""
    observer, exporter, _, _, _ = local_otel_observer()
    small = build_fake_small_provider(
        provider_name="fake",
        deployment_name="small",
        responses=(
            FakeProviderResponse(
                output_text="first output",
                input_tokens=100,
                output_tokens=20,
            ),
            FakeProviderResponse(
                output_text="second output",
                input_tokens=100,
                output_tokens=20,
            ),
        ),
        clock=IncrementingClock(),
    )
    configured, _, _, _ = dependencies(
        (0.93, 0.94),
        observability=observer,
        small_provider=small,
    )
    run_ids = iter(("run-concurrent-a", "run-concurrent-b"))
    correlation_ids = iter(("corr-concurrent-a", "corr-concurrent-b"))
    configured = replace(
        configured,
        run_id_factory=lambda: next(run_ids),
        correlation_id_factory=lambda: next(correlation_ids),
    )
    application = create_app(execution_dependencies=configured)

    async def drive() -> tuple[Any, Any]:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            return await asyncio.gather(
                client.post("/api/v1/runs", json=request_payload()),
                client.post("/api/v1/runs", json=request_payload()),
            )

    responses = asyncio.run(drive())
    spans = exporter.get_finished_spans()
    server_spans = [span for span in spans if span.kind is SpanKind.SERVER]
    roots = [span for span in spans if span.name == "optima.run"]

    assert all(response.status_code == 200 for response in responses)
    assert len(server_spans) == len(roots) == 2
    assert len({root.context.trace_id for root in roots}) == 2
    for root in roots:
        server = next(
            span
            for span in server_spans
            if span.context.trace_id == root.context.trace_id
        )
        assert root.parent is not None
        assert root.parent.span_id == server.context.span_id
        children = [
            span
            for span in spans
            if span.name.startswith("optima.")
            and span.name != "optima.run"
            and span.context.trace_id == root.context.trace_id
        ]
        assert children
        assert all(
            child.parent is not None and child.parent.span_id == root.context.span_id
            for child in children
        )


def test_run_observation_preserves_cancellation_and_stays_incomplete() -> None:
    """Propagate CancelledError while marking the run span incomplete."""
    observer, exporter, reader, _, _ = local_otel_observer()

    with pytest.raises(asyncio.CancelledError):
        with observer.start_run(
            run_id="run-cancel", correlation_id="corr-cancel"
        ) as run:
            with run.start_stage(ObservationStage.MODEL_GENERATE):
                raise asyncio.CancelledError

    spans = exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "optima.run")
    attributes = dict(root.attributes or {})
    assert attributes.get("optima.run.observation_incomplete") is True
    assert root.status.status_code is StatusCode.ERROR
    assert all(span.name != ObservationStage.OUTCOME_PROJECT for span in spans)
    assert not any(name == "optima.runs" for name, _ in metric_points(reader))


def test_failure_isolation_never_swallows_cancellation() -> None:
    """Keep CancelledError propagating through telemetry containment."""

    class CancellingStage:
        def __enter__(self) -> Self:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            return None

        def finish(self, outcome: Any) -> None:
            raise asyncio.CancelledError

    class CancellingRun:
        def __enter__(self) -> Self:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            return None

        def start_stage(self, stage: ObservationStage) -> Any:
            return CancellingStage()

        def project_result(self, result: RunResult) -> None:
            raise asyncio.CancelledError

        def record_pre_result_failure(self, category: FailureCategory) -> None:
            raise asyncio.CancelledError

    isolated_stage = FailureIsolatedStageObservation(CancellingStage())
    with pytest.raises(asyncio.CancelledError):
        isolated_stage.finish(StageOutcome(status=ObservationStatus.SUCCEEDED))

    isolated_run = FailureIsolatedRunObservation(CancellingRun())
    with pytest.raises(asyncio.CancelledError):
        isolated_run.record_pre_result_failure(FailureCategory.TIMEOUT)


def test_partial_metric_projection_does_not_duplicate_points(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Guard exactly-once projection when a meter instrument fails midway."""
    seed = InMemoryObservability()
    configured, _, _, _ = dependencies((0.93,), observability=seed)
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )
    result = run_result_from_response(response)
    observer, exporter, reader, _, _ = local_otel_observer()

    class FailingCounter:
        def add(self, amount: int, attributes: dict[str, str]) -> None:
            raise RuntimeError("SECRET_METER_FAILURE")

    monkeypatch.setattr(observer, "_contract_results", FailingCounter())
    raw_run = observer.start_run(
        run_id=result.run_id,
        correlation_id=result.correlation_id,
    )
    run = FailureIsolatedRunObservation(raw_run)
    run.__enter__()
    run.project_result(result)
    run.project_result(result)
    run.__exit__(None, None, None)

    spans = exporter.get_finished_spans()
    run_points = [
        point for name, point in metric_points(reader) if name == "optima.runs"
    ]
    assert len(run_points) == 1
    assert run_points[0].value == 1
    assert sum(span.name == ObservationStage.OUTCOME_PROJECT for span in spans) == 1
    outcome = next(
        span for span in spans if span.name == ObservationStage.OUTCOME_PROJECT
    )
    assert outcome.status.status_code is StatusCode.ERROR
    assert not any(
        name == "optima.telemetry.projections" for name, _ in metric_points(reader)
    )
    assert caplog.text.count("Terminal telemetry projection failed") == 1
    assert "SECRET_METER_FAILURE" not in caplog.text
    assert "SECRET_METER_FAILURE" not in repr(metric_points(reader))


def test_concurrent_projection_calls_do_not_interleave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block a repeated finalizer until the active metric batch completes."""
    seed = InMemoryObservability()
    configured, _, _, _ = dependencies((0.93,), observability=seed)
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs", json=request_payload()
    )
    result = run_result_from_response(response)
    observer, exporter, reader, _, _ = local_otel_observer()
    run = observer.start_run(run_id=result.run_id, correlation_id=result.correlation_id)
    projection_entered = threading.Event()
    release_projection = threading.Event()
    second_started = threading.Event()
    second_returned = threading.Event()
    errors: list[BaseException] = []
    original_project_metrics = observer.project_metrics

    def blocking_project_metrics(projected: RunResult) -> None:
        projection_entered.set()
        assert release_projection.wait(2)
        original_project_metrics(projected)

    monkeypatch.setattr(observer, "project_metrics", blocking_project_metrics)

    def first_projection() -> None:
        try:
            with run:
                run.project_result(result)
        except BaseException as error:
            errors.append(error)

    def second_projection() -> None:
        second_started.set()
        try:
            run.project_result(result)
        except BaseException as error:
            errors.append(error)
        finally:
            second_returned.set()

    first = threading.Thread(target=first_projection)
    second = threading.Thread(target=second_projection)
    first.start()
    assert projection_entered.wait(2)
    second.start()
    assert second_started.wait(2)
    assert second_returned.wait(0.05) is False
    release_projection.set()
    first.join(2)
    second.join(2)

    assert errors == []
    assert not first.is_alive()
    assert not second.is_alive()
    assert (
        sum(
            span.name == ObservationStage.OUTCOME_PROJECT
            for span in exporter.get_finished_spans()
        )
        == 1
    )
    assert (
        len([point for name, point in metric_points(reader) if name == "optima.runs"])
        == 1
    )


def test_disabled_observability_has_no_azure_import_in_subprocess() -> None:
    """Prove disabled mode never imports Azure Monitor or starts a thread."""
    import subprocess
    import sys

    script = (
        "import sys, threading\n"
        "from optima.config import AppSettings\n"
        "from optima.observability.azure_monitor import build_observability\n"
        "from optima.observability.noop import NO_OP_OBSERVABILITY\n"
        "baseline = threading.active_count()\n"
        "obs = build_observability("
        "AppSettings(application_insights_enabled=False))\n"
        "assert obs is NO_OP_OBSERVABILITY, 'disabled must be inert'\n"
        "assert threading.active_count() == baseline, 'no new thread'\n"
        "assert 'azure.monitor' not in sys.modules, 'no azure import'\n"
        "assert obs.force_flush() is True\n"
        "obs.close()\n"
        "print('DISABLED_OK')\n"
    )
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment = {**os.environ, "PYTHONPATH": source_root}
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "DISABLED_OK" in completed.stdout


def test_actual_direct_initializer_preserves_host_state_in_subprocess() -> None:
    """Run direct exporters beside host providers without global mutation."""
    import subprocess
    import sys

    script = (
        "import os\n"
        "os.environ['OTEL_RESOURCE_ATTRIBUTES'] = 'secret.attribute=SECRET_HOST'\n"
        "os.environ['OTEL_SERVICE_NAME'] = 'SECRET_SERVICE'\n"
        "os.environ['AZURE_MONITOR_DISTRO_VERSION'] = 'HOST_DISTRO'\n"
        "from opentelemetry import metrics, trace\n"
        "from opentelemetry.sdk.metrics import MeterProvider\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "class HostTracer(TracerProvider):\n"
        "    shutdown_calls = 0\n"
        "    def shutdown(self): self.shutdown_calls += 1\n"
        "class HostMeter(MeterProvider):\n"
        "    shutdown_calls = 0\n"
        "    get_meter_calls = 0\n"
        "    def shutdown(self, timeout_millis=30000): self.shutdown_calls += 1\n"
        "    def get_meter(self, *args, **kwargs):\n"
        "        self.get_meter_calls += 1\n"
        "        return super().get_meter(*args, **kwargs)\n"
        "host_tracer = HostTracer()\n"
        "host_meter = HostMeter()\n"
        "trace.set_tracer_provider(host_tracer)\n"
        "metrics.set_meter_provider(host_meter)\n"
        "os.environ['OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED'] = 'true'\n"
        "os.environ['OTEL_BSP_MAX_QUEUE_SIZE'] = 'SECRET_BAD_BSP'\n"
        "os.environ['OTEL_SDK_DISABLED'] = 'true'\n"
        "os.environ['APPLICATIONINSIGHTS_METRICS_TO_LOGANALYTICS_ENABLED'] = 'false'\n"
        "from pydantic import SecretStr\n"
        "from optima.config import ApplicationInsightsConfiguration\n"
        "from optima.observability.azure_monitor import _initialize_azure_monitor\n"
        "config = ApplicationInsightsConfiguration("
        "connection_string=SecretStr("
        f"{_CONNECTION_STRING!r}),"
        "service_name='optima-proof',service_version='1.2.3',"
        "deployment_environment='test',sampling_ratio=0.25)\n"
        "observer = _initialize_azure_monitor(config)\n"
        "tracer_lifecycle = observer._tracer_provider\n"
        "meter_lifecycle = observer._meter_provider\n"
        "trace_exporter = tracer_lifecycle._exporter\n"
        "expected = {'service.name': 'optima-proof', "
        "'service.version': '1.2.3', "
        "'deployment.environment.name': 'test'}\n"
        "assert dict(tracer_lifecycle._provider.resource.attributes) == expected\n"
        "assert dict(meter_lifecycle._provider._sdk_config.resource.attributes) "
        "== expected\n"
        "assert trace.get_tracer_provider() is host_tracer\n"
        "assert metrics.get_meter_provider() is host_meter\n"
        "assert host_meter.get_meter_calls == 0\n"
        "assert tracer_lifecycle._provider._disabled is False\n"
        "assert meter_lifecycle._provider._disabled is False\n"
        "assert trace_exporter._should_collect_stats() is False\n"
        "assert trace_exporter._should_collect_customer_sdkstats() is False\n"
        "assert trace_exporter._should_collect_otel_resource_metric() is False\n"
        "metric_exporter = meter_lifecycle._exporter\n"
        "assert metric_exporter._determine_metrics_to_log_analytics() is True\n"
        "sampler = tracer_lifecycle._provider.sampler\n"
        "sampled = next(sampler.should_sample(None, trace_id, 'root') "
        "for trace_id in range(1, 10000) "
        "if sampler.should_sample(None, trace_id, 'root').decision.is_sampled())\n"
        "assert sampled.attributes['_MS.sampleRate'] == 25.0\n"
        "retry = trace_exporter.client._config.retry_policy\n"
        "assert (retry.total_retries, retry.connect_retries, "
        "retry.read_retries, retry.status_retries) == (0, 0, 0, 0)\n"
        "policies = trace_exporter.client._client._pipeline._impl_policies\n"
        "redirect = next(policy for policy in policies "
        "if type(policy).__name__ == 'RedirectPolicy')\n"
        "assert redirect.allow is False\n"
        "assert trace_exporter.client._config.redirect_policy.max_redirects == 0\n"
        "import azure.monitor.opentelemetry as parent_package\n"
        "assert not hasattr(parent_package, 'configure_azure_monitor')\n"
        "assert os.environ['OTEL_RESOURCE_ATTRIBUTES'] == "
        "'secret.attribute=SECRET_HOST'\n"
        "assert os.environ['OTEL_SERVICE_NAME'] == 'SECRET_SERVICE'\n"
        "assert os.environ['AZURE_MONITOR_DISTRO_VERSION'] == 'HOST_DISTRO'\n"
        "observer.close()\n"
        "observer.close()\n"
        "assert host_tracer.shutdown_calls == 0\n"
        "assert host_meter.shutdown_calls == 0\n"
        "print('DIRECT_INITIALIZER_OK')\n"
    )
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": source_root},
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "DIRECT_INITIALIZER_OK" in completed.stdout
    assert "SECRET_BAD_BSP" not in completed.stderr
