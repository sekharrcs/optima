"""Azure Monitor composition isolated from OPTIMA business components."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock, get_ident
from typing import Any

from fastapi import FastAPI

from optima.config import ApplicationInsightsConfiguration, AppSettings
from optima.observability.contracts import Observability, RunObservation
from optima.observability.noop import NO_OP_OBSERVABILITY, NoOpObservability
from optima.observability.opentelemetry import OpenTelemetryObservability
from optima.observability.resilient import FailureIsolatedObservability

ObservabilityInitializer = Callable[[ApplicationInsightsConfiguration], Observability]

_logger = logging.getLogger(__name__)

_AZURE_MONITOR_INITIALIZATION_LOCK = RLock()
_SDK_LOGGER_PREFIXES = ("azure", "opentelemetry")


class AzureMonitorConfigurationConflictError(ValueError):
    """Raised when one process requests two telemetry configurations."""


class _UnavailableObservability(NoOpObservability):
    """Remain inert while reporting that enabled telemetry is unavailable."""

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return False


_UNAVAILABLE_OBSERVABILITY = _UnavailableObservability()


class _ExportFailureSignal:
    """Emit one process-safe diagnostic for owned exporter failures."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._reported = False

    def report(self) -> None:
        with self._lock:
            if self._reported:
                return
            self._reported = True
        _logger.warning("Application Insights telemetry export failed")


@dataclass(frozen=True, slots=True)
class _AzureRuntimeComponents:
    """Factories required to build one isolated Azure Monitor runtime."""

    trace_exporter_factory: Callable[..., Any]
    metric_exporter_factory: Callable[..., Any]
    span_processor_factory: Callable[..., Any]
    metric_reader_factory: Callable[..., Any]
    tracer_provider_factory: Callable[..., Any]
    meter_provider_factory: Callable[..., Any]
    internal_meter_provider_factory: Callable[[], Any]
    resource_factory: Callable[..., Any]
    sampler_factory: Callable[[float], Any]
    span_limits_factory: Callable[[], Any]
    exemplar_filter_factory: Callable[[], Any]


class _OwnedProviderLifecycle:
    """Contain lifecycle failures for one provider and its owned exporter."""

    def __init__(
        self,
        *,
        provider: Any,
        exporter: Any,
        failure_signal: _ExportFailureSignal,
    ) -> None:
        self._provider = provider
        self._exporter = exporter
        self._failure_signal = failure_signal

    def force_flush(self, timeout_millis: int = 30_000) -> bool | None:
        try:
            with _suppress_current_thread_sdk_logs():
                result = self._provider.force_flush(timeout_millis)
            return None if result is None else bool(result)
        except Exception:
            self._failure_signal.report()
            return False

    def shutdown(self) -> None:
        try:
            with _suppress_current_thread_sdk_logs():
                self._provider.shutdown()
        except Exception:
            self._failure_signal.report()
            _try_shutdown(self._exporter)


class _AzureMonitorRuntimeLease:
    """One close-once claim on the process-global Azure Monitor runtime."""

    def __init__(
        self,
        registry: AzureMonitorRuntimeRegistry,
        delegate: Observability,
    ) -> None:
        self._registry = registry
        self._delegate = delegate
        self._lock = RLock()
        self._closed = False

    def start_run(self, *, run_id: str, correlation_id: str) -> RunObservation:
        with self._lock:
            self._raise_if_closed()
            return self._delegate.start_run(
                run_id=run_id,
                correlation_id=correlation_id,
            )

    def instrument_fastapi(self, application: FastAPI) -> None:
        with self._lock:
            self._raise_if_closed()
            self._delegate.instrument_fastapi(application)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        with self._lock:
            self._raise_if_closed()
            return self._delegate.force_flush(timeout_millis)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._registry._release(self._delegate)

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError("Application Insights runtime lease is closed")


class AzureMonitorRuntimeRegistry:
    """Create one process-wide Azure Monitor runtime for one configuration."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._configuration: ApplicationInsightsConfiguration | None = None
        self._observability: Observability | None = None
        self._lease_count = 0
        self._closed = False

    def get_or_create(
        self,
        configuration: ApplicationInsightsConfiguration,
        initializer: ObservabilityInitializer,
    ) -> Observability:
        """Initialize once or return the existing identically configured runtime."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Application Insights runtime registry is closed")
            if self._observability is not None:
                if configuration != self._configuration:
                    raise AzureMonitorConfigurationConflictError(
                        "Application Insights is already initialized with "
                        "different settings"
                    )
            else:
                try:
                    observability = initializer(configuration)
                except Exception:
                    _logger.warning(
                        "Application Insights initialization failed; "
                        "observability is disabled"
                    )
                    observability = _UNAVAILABLE_OBSERVABILITY
                self._configuration = configuration
                self._observability = observability
            self._lease_count += 1
            return _AzureMonitorRuntimeLease(self, self._observability)

    def _release(self, observability: Observability) -> None:
        delegate_to_close: Observability | None = None
        with self._lock:
            if self._closed or observability is not self._observability:
                return
            self._lease_count -= 1
            if self._lease_count == 0:
                self._closed = True
                delegate_to_close = self._observability
        if delegate_to_close is not None:
            delegate_to_close.close()


_AZURE_MONITOR_REGISTRY = AzureMonitorRuntimeRegistry()


def build_observability(
    settings: AppSettings,
    *,
    registry: AzureMonitorRuntimeRegistry | None = None,
    initializer: ObservabilityInitializer | None = None,
) -> Observability:
    """Build inert or Azure-backed observability from validated settings."""
    configuration = settings.application_insights_configuration()
    if configuration is None:
        return NO_OP_OBSERVABILITY
    selected_registry = registry or _AZURE_MONITOR_REGISTRY
    selected_initializer = initializer or _initialize_azure_monitor
    return FailureIsolatedObservability(
        selected_registry.get_or_create(configuration, selected_initializer)
    )


def _initialize_azure_monitor(
    configuration: ApplicationInsightsConfiguration,
    *,
    components: _AzureRuntimeComponents | None = None,
) -> Observability:
    """Build one isolated runtime without changing global providers or environment."""
    if configuration.live_metrics_enabled:
        raise ValueError("isolated Application Insights does not support Live Metrics")
    if configuration.performance_counters_enabled:
        raise ValueError(
            "isolated Application Insights does not support performance counters"
        )

    selected = components or _azure_runtime_components()
    failure_signal = _ExportFailureSignal()
    trace_exporter: Any | None = None
    metric_exporter: Any | None = None
    span_processor: Any | None = None
    metric_reader: Any | None = None
    tracer_provider: Any | None = None
    meter_provider: Any | None = None
    internal_meter_provider: Any | None = None
    span_processor_added = False

    with _AZURE_MONITOR_INITIALIZATION_LOCK:
        try:
            resource = selected.resource_factory(
                attributes={
                    "service.name": configuration.service_name,
                    "service.version": configuration.service_version,
                    "deployment.environment.name": (
                        configuration.deployment_environment
                    ),
                }
            )
            sampler = selected.sampler_factory(configuration.sampling_ratio)
            exporter_options = _azure_exporter_options(configuration, failure_signal)
            with _suppress_current_thread_sdk_logs():
                trace_exporter = selected.trace_exporter_factory(**exporter_options)
                metric_exporter = selected.metric_exporter_factory(**exporter_options)
                internal_meter_provider = selected.internal_meter_provider_factory()
                span_processor = selected.span_processor_factory(
                    trace_exporter,
                    max_queue_size=2_048,
                    schedule_delay_millis=5_000,
                    max_export_batch_size=512,
                    export_timeout_millis=30_000,
                    meter_provider=internal_meter_provider,
                )
                metric_reader = selected.metric_reader_factory(
                    metric_exporter,
                    export_interval_millis=60_000,
                    export_timeout_millis=30_000,
                )
                meter_provider = selected.meter_provider_factory(
                    metric_readers=[metric_reader],
                    resource=resource,
                    exemplar_filter=selected.exemplar_filter_factory(),
                    shutdown_on_exit=False,
                )
                tracer_provider = selected.tracer_provider_factory(
                    sampler=sampler,
                    resource=resource,
                    shutdown_on_exit=False,
                    span_limits=selected.span_limits_factory(),
                    meter_provider=internal_meter_provider,
                )
                tracer_provider.add_span_processor(span_processor)
                span_processor_added = True
                return OpenTelemetryObservability(
                    tracer=tracer_provider.get_tracer("optima.observability", "1"),
                    meter=meter_provider.get_meter("optima.observability", "1"),
                    tracer_provider=_OwnedProviderLifecycle(
                        provider=tracer_provider,
                        exporter=trace_exporter,
                        failure_signal=failure_signal,
                    ),
                    meter_provider=_OwnedProviderLifecycle(
                        provider=meter_provider,
                        exporter=metric_exporter,
                        failure_signal=failure_signal,
                    ),
                    fastapi_instrumentation_enabled=(
                        configuration.fastapi_instrumentation_enabled
                    ),
                    exclude_health_routes=configuration.exclude_health_routes,
                )
        except BaseException:
            _shutdown_failed_initialization(
                tracer_provider=tracer_provider,
                meter_provider=meter_provider,
                span_processor=span_processor,
                metric_reader=metric_reader,
                trace_exporter=trace_exporter,
                metric_exporter=metric_exporter,
                span_processor_added=span_processor_added,
            )
            raise


def _azure_runtime_components() -> _AzureRuntimeComponents:
    """Load the direct exporter and local OpenTelemetry provider factories."""
    from azure.monitor.opentelemetry.exporter import ApplicationInsightsSampler
    from opentelemetry.metrics import NoOpMeterProvider
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics._internal.exemplar.exemplar_filter import (
        AlwaysOffExemplarFilter,
    )
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanLimits, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased

    class OptimaTracerProvider(TracerProvider):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._disabled = False

    class OptimaMeterProvider(MeterProvider):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._disabled = False

    trace_exporter, metric_exporter = _owned_exporter_types()
    return _AzureRuntimeComponents(
        trace_exporter_factory=trace_exporter,
        metric_exporter_factory=metric_exporter,
        span_processor_factory=BatchSpanProcessor,
        metric_reader_factory=PeriodicExportingMetricReader,
        tracer_provider_factory=OptimaTracerProvider,
        meter_provider_factory=OptimaMeterProvider,
        internal_meter_provider_factory=NoOpMeterProvider,
        resource_factory=Resource,
        sampler_factory=lambda ratio: ParentBased(ApplicationInsightsSampler(ratio)),
        span_limits_factory=lambda: SpanLimits(
            max_attributes=128,
            max_events=128,
            max_links=128,
            max_span_attributes=128,
            max_event_attributes=128,
            max_link_attributes=128,
            max_attribute_length=None,
            max_span_attribute_length=None,
        ),
        exemplar_filter_factory=AlwaysOffExemplarFilter,
    )


def _owned_exporter_types() -> tuple[type[Any], type[Any]]:
    """Build exporter types that suppress SDK internals and raw diagnostics."""
    from azure.monitor.opentelemetry.exporter import (
        AzureMonitorMetricExporter,
        AzureMonitorTraceExporter,
    )
    from opentelemetry.sdk.metrics.export import MetricExportResult
    from opentelemetry.sdk.trace.export import SpanExportResult

    class OptimaAzureMonitorTraceExporter(AzureMonitorTraceExporter):
        def __init__(self, **kwargs: Any) -> None:
            self._optima_failure_signal = kwargs.pop("optima_failure_signal")
            self._optima_initializing = True
            try:
                with _suppress_current_thread_sdk_logs():
                    super().__init__(**kwargs)
            finally:
                self._optima_initializing = False

        def _is_stats_exporter(self) -> bool:
            return self._optima_initializing or bool(
                getattr(self, "_is_sdkstats", False)
            )

        def _should_collect_stats(self) -> bool:
            return False

        def _should_collect_customer_sdkstats(self) -> bool:
            return False

        def _should_collect_otel_resource_metric(self) -> bool:
            return False

        def export(self, spans: Any, **kwargs: Any) -> Any:
            try:
                with _suppress_current_thread_sdk_logs():
                    result = super().export(spans, **kwargs)
            except Exception:
                self._optima_failure_signal.report()
                return SpanExportResult.FAILURE
            if result is not SpanExportResult.SUCCESS:
                self._optima_failure_signal.report()
            return result

        def shutdown(self) -> None:
            try:
                with _suppress_current_thread_sdk_logs():
                    super().shutdown()
            except Exception:
                self._optima_failure_signal.report()

    class OptimaAzureMonitorMetricExporter(AzureMonitorMetricExporter):
        def __init__(self, **kwargs: Any) -> None:
            self._optima_failure_signal = kwargs.pop("optima_failure_signal")
            self._optima_initializing = True
            try:
                with _suppress_current_thread_sdk_logs():
                    super().__init__(**kwargs)
            finally:
                self._optima_initializing = False

        def _is_stats_exporter(self) -> bool:
            return self._optima_initializing or bool(
                getattr(self, "_is_sdkstats", False)
            )

        def _should_collect_stats(self) -> bool:
            return False

        def _should_collect_customer_sdkstats(self) -> bool:
            return False

        def _determine_metrics_to_log_analytics(self) -> bool:
            return True

        def export(
            self,
            metrics_data: Any,
            timeout_millis: float = 10_000,
            **kwargs: Any,
        ) -> Any:
            try:
                with _suppress_current_thread_sdk_logs():
                    result = super().export(
                        metrics_data,
                        timeout_millis=timeout_millis,
                        **kwargs,
                    )
            except Exception:
                self._optima_failure_signal.report()
                return MetricExportResult.FAILURE
            if result is not MetricExportResult.SUCCESS:
                self._optima_failure_signal.report()
            return result

        def shutdown(
            self,
            timeout_millis: float = 30_000,
            **kwargs: Any,
        ) -> None:
            try:
                with _suppress_current_thread_sdk_logs():
                    super().shutdown(timeout_millis=timeout_millis, **kwargs)
            except Exception:
                self._optima_failure_signal.report()

    return OptimaAzureMonitorTraceExporter, OptimaAzureMonitorMetricExporter


def _azure_exporter_options(
    configuration: ApplicationInsightsConfiguration,
    failure_signal: _ExportFailureSignal,
) -> dict[str, Any]:
    """Build explicit exporter options without exposing connection details."""
    return {
        "connection_string": configuration.connection_string.get_secret_value(),
        "disable_offline_storage": not configuration.offline_storage_enabled,
        "retry_total": 0,
        "retry_connect": 0,
        "retry_read": 0,
        "retry_status": 0,
        "instrumentation_collection": True,
        "optima_failure_signal": failure_signal,
    }


def _shutdown_failed_initialization(
    *,
    tracer_provider: Any | None,
    meter_provider: Any | None,
    span_processor: Any | None,
    metric_reader: Any | None,
    trace_exporter: Any | None,
    metric_exporter: Any | None,
    span_processor_added: bool,
) -> None:
    """Shut down only components created by this failed initializer."""
    tracer_closed = _try_shutdown(tracer_provider)
    if not tracer_closed or not span_processor_added:
        processor_closed = _try_shutdown(span_processor)
        if not processor_closed:
            _try_shutdown(trace_exporter)

    meter_closed = _try_shutdown(meter_provider)
    if not meter_closed:
        reader_closed = _try_shutdown(metric_reader)
        if not reader_closed:
            _try_shutdown(metric_exporter)


def _try_shutdown(component: Any | None) -> bool:
    """Best-effort close one known-owned SDK component."""
    if component is None:
        return False
    shutdown = getattr(component, "shutdown", None)
    if not callable(shutdown):
        return False
    try:
        with _suppress_current_thread_sdk_logs():
            shutdown()
    except BaseException:
        return False
    return True


class _ExcludeOwnedSdkLogs(logging.Filter):
    """Drop dependency records only for the current owned SDK operation."""

    def __init__(self) -> None:
        super().__init__()
        self._thread_id = get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.thread == self._thread_id
            and record.name.startswith(_SDK_LOGGER_PREFIXES)
        )


@contextmanager
def _suppress_current_thread_sdk_logs() -> Iterator[None]:
    """Suppress raw SDK logs without changing concurrent host diagnostics."""
    log_filter = _ExcludeOwnedSdkLogs()
    acquire_lock = logging._acquireLock  # type: ignore[attr-defined]
    release_lock = logging._releaseLock  # type: ignore[attr-defined]
    acquire_lock()
    try:
        sdk_loggers = [
            candidate
            for name, candidate in logging.Logger.manager.loggerDict.items()
            if isinstance(candidate, logging.Logger)
            and name.startswith(_SDK_LOGGER_PREFIXES)
        ]
        handlers = set(logging.getLogger().handlers)
        for logger in sdk_loggers:
            handlers.update(logger.handlers)
            logger.addFilter(log_filter)
        if logging.lastResort is not None:
            handlers.add(logging.lastResort)
        for handler in handlers:
            handler.addFilter(log_filter)
    finally:
        release_lock()

    try:
        yield
    finally:
        acquire_lock()
        try:
            for logger in sdk_loggers:
                logger.removeFilter(log_filter)
            for handler in handlers:
                handler.removeFilter(log_filter)
        finally:
            release_lock()
