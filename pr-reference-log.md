---
title: Application Insights tracing reference log
description: Primary sources and verified package APIs used for OPTIMA Corrective Slice 10D
---

## Repository Baseline

- Repository: `sekharrcs/optima`
- Base branch: `main`
- Verified base SHA: `6a4ef6772d6a70e4798a1cbb944ea6741c222665`
- Verified baseline: 958 passing tests
- Feature branch: `feature/application-insights-tracing`

## Primary Documentation

- [Enable OpenTelemetry in Application Insights](https://learn.microsoft.com/en-us/azure/azure-monitor/app/opentelemetry-enable?tabs=python)
- [Configure OpenTelemetry in Application Insights](https://learn.microsoft.com/en-us/azure/azure-monitor/app/opentelemetry-configuration)
- [Sampling in Application Insights with OpenTelemetry](https://learn.microsoft.com/en-us/azure/azure-monitor/app/opentelemetry-sampling)
- [Azure Monitor OpenTelemetry Distro for Python](https://learn.microsoft.com/en-us/python/api/overview/azure/monitor-opentelemetry-readme?view=azure-python)
- [OpenTelemetry FastAPI instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/fastapi/fastapi.html)
- [OpenTelemetry HTTPX instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/httpx/httpx.html)
- [OpenTelemetry Python trace SDK](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.html)
- [OpenTelemetry Python sampling SDK](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.sampling.html)
- [OpenTelemetry Python metrics API](https://opentelemetry-python.readthedocs.io/en/latest/api/metrics.html)

The Microsoft guidance verified that the distro is the recommended Python
integration, metrics are not sampled, fixed-percentage and rate-limited sampling
are supported, Live Metrics and performance counters default to enabled, and
offline retry storage defaults to enabled. The implementation overrides those
defaults explicitly for OPTIMA.

## Verified Package APIs

The approved Microsoft package feed
`https://packagefeedproxy.microsoft.io/pypi/simple` resolved:

- `azure-monitor-opentelemetry==1.8.9`
- `azure-monitor-opentelemetry-exporter==1.0.0b56`
- `opentelemetry-api==1.43.0`
- `opentelemetry-sdk==1.43.0`
- `opentelemetry-instrumentation-fastapi==0.64b0`

The installed source confirmed:

- `configure_azure_monitor(**kwargs)` accepts connection string, resource,
  `sampling_ratio`, Live Metrics, performance-counter, offline-storage,
  instrumentation, span-processor, metric-reader, and browser-loader options
- environment sampler values take precedence over code parameters
- `parentbased_trace_id_ratio` preserves local and remote parent sampling
  decisions while applying the configured ratio to root traces
- distro-supported automatic instrumentations are Azure SDK, Django, FastAPI,
  Flask, psycopg2, requests, urllib, and urllib3
- distro logging disablement in 1.8.9 is controlled by
  `OTEL_LOGS_EXPORTER=none`
- FastAPI instrumentation can exclude URLs and header capture, but its exception
  middleware records raw exception details; OPTIMA therefore uses a smaller
  explicit server middleware that does not record exception events or messages
- OpenTelemetry providers expose bounded `force_flush` and `shutdown` lifecycle
  operations
- Azure Core transport defaults to retries and redirects unless the exporter
  receives explicit zero values
- the distro recognizes environment switches for control-plane configuration,
  Statsbeat, SDK statistics, and OpenTelemetry resource metrics

## Validation Boundary

No live Azure resource, credential, or ingestion endpoint is used by automated
tests. Azure initialization arguments are captured through an injected
configurator, and trace/metric behavior is validated with local in-memory
OpenTelemetry providers and exporters.

## Final Validation

- `uv lock --check`: passed
- `uv sync --frozen --all-groups --dry-run`: passed
- `ruff format --check .`: 116 files formatted
- `ruff check .`: passed
- `mypy src tests`: 98 source files passed
- focused settings and observability tests: 90 passed
- full `pytest`: 1,006 passed
- `git diff --check`: passed
- secret-pattern scan: zero added-content matches
- blocked package-source scan: zero matches in `pyproject.toml` and `uv.lock`

The only test warning is the existing Starlette deprecation warning for
FastAPI's `httpx` TestClient integration.

## Adversarial Review

The first independent review found one BLOCKING, three HIGH, two MEDIUM, and
three LOW issues. The implementation then added strict Azure HTTPS endpoint
validation, ambient resource scrubbing, parent-based sampling, runtime startup
containment, global-provider ownership checks, cleanup-preserving wrappers,
zero exporter redirects and retries, disabled control-plane/statistics
components, one-deadline flushing, and stronger deterministic fakes.

The second independent review confirmed all prior BLOCKING, HIGH, and MEDIUM
findings were closed and reported two LOW edge cases. Both were corrected:
malformed DNS labels now fail endpoint validation, and pre-result failures and
terminal projections are mutually exclusive in the in-memory recorder. A
focused flush-deadline regression test was also added.

An additional installed-source audit found that the distro checks the resource
metric disable flag during export rather than only during initialization. The
adapter now owns that flag for its full lifetime and restores the prior process
value during idempotent close.

## Dependency Assessment

The project adds one direct dependency:
`azure-monitor-opentelemetry==1.8.9`. Its transitive OpenTelemetry and Azure
Monitor packages are required by the distro. `uv lock` repinned Microsoft feed
mirror aliases across the lockfile while preserving approved feed URLs and
cryptographic hashes. The lockfile was generated by `uv`, not hand-edited.

## Live Azure Limitation

No live Application Insights resource was used. Azure resource provisioning,
secret injection, production FastAPI lifespan ownership, and an opt-in live
ingestion smoke test remain Slice 11 work.