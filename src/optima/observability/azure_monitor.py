"""Azure Monitor composition isolated from OPTIMA business components."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import RLock
from typing import Any, Protocol

from optima.config import ApplicationInsightsConfiguration, AppSettings
from optima.observability.contracts import Observability
from optima.observability.noop import NO_OP_OBSERVABILITY
from optima.observability.opentelemetry import OpenTelemetryObservability
from optima.observability.resilient import FailureIsolatedObservability

ObservabilityInitializer = Callable[[ApplicationInsightsConfiguration], Observability]

_logger = logging.getLogger(__name__)

_DISABLED_INSTRUMENTATIONS = {
    "azure_sdk": {"enabled": False},
    "django": {"enabled": False},
    "fastapi": {"enabled": False},
    "flask": {"enabled": False},
    "psycopg2": {"enabled": False},
    "requests": {"enabled": False},
    "urllib": {"enabled": False},
    "urllib3": {"enabled": False},
}


class AzureMonitorConfigurator(Protocol):
    """Verified distro entry point accepted by the isolated initializer."""

    def __call__(self, **kwargs: Any) -> None:
        """Configure the process-wide Azure Monitor providers."""
        ...


class AzureMonitorConfigurationConflictError(ValueError):
    """Raised when one process requests two telemetry configurations."""


class AzureMonitorRuntimeRegistry:
    """Create one process-wide Azure Monitor runtime for one configuration."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._configuration: ApplicationInsightsConfiguration | None = None
        self._observability: Observability | None = None

    def get_or_create(
        self,
        configuration: ApplicationInsightsConfiguration,
        initializer: ObservabilityInitializer,
    ) -> Observability:
        """Initialize once or return the existing identically configured runtime."""
        with self._lock:
            if self._observability is not None:
                if configuration != self._configuration:
                    raise AzureMonitorConfigurationConflictError(
                        "Application Insights is already initialized with "
                        "different settings"
                    )
                return self._observability
            try:
                observability = initializer(configuration)
            except Exception:
                _logger.warning(
                    "Application Insights initialization failed; "
                    "observability is disabled"
                )
                observability = NO_OP_OBSERVABILITY
            self._configuration = configuration
            self._observability = observability
            return observability


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
    configurator: AzureMonitorConfigurator | None = None,
    tracer_provider_getter: Callable[[], object] | None = None,
    meter_provider_getter: Callable[[], object] | None = None,
) -> Observability:
    """Configure the verified distro API only after settings pass validation."""
    from opentelemetry import metrics, trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    selected_configurator: AzureMonitorConfigurator
    if configurator is None:
        from azure.monitor.opentelemetry import configure_azure_monitor

        selected_configurator = configure_azure_monitor
    else:
        selected_configurator = configurator
    get_tracer_provider = tracer_provider_getter or trace.get_tracer_provider
    get_meter_provider = meter_provider_getter or metrics.get_meter_provider
    previous_tracer_provider = get_tracer_provider()
    previous_meter_provider = get_meter_provider()
    if not _is_unconfigured_tracer_provider(previous_tracer_provider):
        raise RuntimeError("OpenTelemetry tracer provider is already configured")
    if not _is_unconfigured_meter_provider(previous_meter_provider):
        raise RuntimeError("OpenTelemetry meter provider is already configured")

    resource = Resource(
        attributes={
            "service.name": configuration.service_name,
            "service.version": configuration.service_version,
            "deployment.environment.name": configuration.deployment_environment,
        }
    )
    environment_overrides = {
        "APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED": "true",
        "APPLICATIONINSIGHTS_SDKSTATS_DISABLED": "true",
        "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL": "true",
        "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS": "",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_METRICS_EXPORTER": "",
        "OTEL_RESOURCE_ATTRIBUTES": "",
        "OTEL_SERVICE_NAME": "",
        "OTEL_TRACES_EXPORTER": "",
        "OTEL_TRACES_SAMPLER": "parentbased_trace_id_ratio",
        "OTEL_TRACES_SAMPLER_ARG": str(configuration.sampling_ratio),
    }
    resource_metric_flag = "APPLICATIONINSIGHTS_OPENTELEMETRY_RESOURCE_METRIC_DISABLED"
    original_resource_metric_flag = os.environ.get(resource_metric_flag)
    os.environ[resource_metric_flag] = "true"
    tracer_provider: object = previous_tracer_provider
    meter_provider: object = previous_meter_provider
    try:
        with _temporary_environment(environment_overrides):
            selected_configurator(
                connection_string=(configuration.connection_string.get_secret_value()),
                disable_offline_storage=not configuration.offline_storage_enabled,
                enable_live_metrics=configuration.live_metrics_enabled,
                enable_performance_counters=(
                    configuration.performance_counters_enabled
                ),
                enable_trace_based_sampling_for_logs=False,
                instrumentation_options=_DISABLED_INSTRUMENTATIONS,
                browser_sdk_loader_config={"enabled": False},
                resource=resource,
                retry_total=0,
                retry_connect=0,
                retry_read=0,
                retry_status=0,
            )
        tracer_provider = get_tracer_provider()
        meter_provider = get_meter_provider()
        if not isinstance(tracer_provider, TracerProvider) or not isinstance(
            meter_provider, MeterProvider
        ):
            raise RuntimeError("Azure Monitor did not install owned providers")
        return OpenTelemetryObservability(
            tracer=tracer_provider.get_tracer("optima.observability", "1"),
            meter=meter_provider.get_meter("optima.observability", "1"),
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            fastapi_instrumentation_enabled=(
                configuration.fastapi_instrumentation_enabled
            ),
            exclude_health_routes=configuration.exclude_health_routes,
            close_callbacks=(
                lambda: _restore_environment_value(
                    resource_metric_flag,
                    original_resource_metric_flag,
                ),
            ),
        )
    except Exception:
        try:
            _shutdown_new_providers(
                previous_tracer_provider=previous_tracer_provider,
                previous_meter_provider=previous_meter_provider,
                current_tracer_provider=_current_provider_or_previous(
                    get_tracer_provider,
                    previous_tracer_provider,
                    retained=tracer_provider,
                ),
                current_meter_provider=_current_provider_or_previous(
                    get_meter_provider,
                    previous_meter_provider,
                    retained=meter_provider,
                ),
            )
        finally:
            _restore_environment_value(
                resource_metric_flag,
                original_resource_metric_flag,
            )
        raise


def _is_unconfigured_tracer_provider(provider: object) -> bool:
    return (
        type(provider).__module__ == "opentelemetry.trace"
        and type(provider).__qualname__ == "ProxyTracerProvider"
    )


def _is_unconfigured_meter_provider(provider: object) -> bool:
    return (
        type(provider).__module__ == "opentelemetry.metrics._internal"
        and type(provider).__qualname__ == "_ProxyMeterProvider"
    )


def _shutdown_new_providers(
    *,
    previous_tracer_provider: object,
    previous_meter_provider: object,
    current_tracer_provider: object,
    current_meter_provider: object,
) -> None:
    """Best-effort cleanup of providers created by a failed initialization."""
    for previous, current in (
        (previous_tracer_provider, current_tracer_provider),
        (previous_meter_provider, current_meter_provider),
    ):
        shutdown = getattr(current, "shutdown", None)
        if current is not previous and callable(shutdown):
            try:
                shutdown()
            except Exception:
                continue


def _current_provider_or_previous(
    getter: Callable[[], object],
    previous: object,
    *,
    retained: object,
) -> object:
    """Return the current provider without masking the original failure."""
    if retained is not previous:
        return retained
    try:
        return getter()
    except Exception:
        return previous


@contextmanager
def _temporary_environment(values: dict[str, str]) -> Iterator[None]:
    """Apply deterministic SDK environment inputs only during configuration."""
    original = {name: os.environ.get(name) for name in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _restore_environment_value(name: str, value: str | None) -> None:
    """Restore one process environment value owned by the adapter."""
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
