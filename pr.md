---
title: Application Insights tracing and operational metrics
description: Draft pull request summary for OPTIMA Corrective Slice 10D
---

## Summary

- Add a provider-independent observability boundary with no-op, deterministic
  in-memory, and OpenTelemetry implementations
- Trace the actual contract, cache, planner, context, model, evaluator,
  persistence, and terminal-outcome operations beneath one OPTIMA run
- Configure Azure Monitor Application Insights through the current OpenTelemetry
  distro with strict privacy, sampling, and initialization controls
- Emit bounded metrics from validated terminal evidence without weakening exact
  cost or missing-measurement semantics
- Preserve every existing planner, provider, evaluator, cache, persistence, API,
  and cost behavior

## Validation

- `uv lock --check`: passed
- `uv sync --frozen --all-groups --dry-run`: passed
- `ruff format --check .`: 116 files formatted
- `ruff check .`: passed
- `mypy src tests`: 98 source files passed
- focused settings and observability tests: 87 passed
- full `pytest`: 1,006 passed with one existing FastAPI TestClient deprecation
  warning
- `git diff --check`: passed
- added-content secret-pattern scan: zero matches
- project dependency-source scan: zero `pypi.org` or `pythonhosted.org`
  matches in `pyproject.toml` and `uv.lock`

Two independent adversarial review passes found and then verified remediation
of ambient-resource leakage, endpoint validation, startup failure isolation,
global-provider ownership, parent sampling, cleanup after recorder failure,
exporter retries, distro background components, flush deadlines, fake-observer
identity, and duplicate sample configuration. The final pass reported no open
BLOCKING, HIGH, or MEDIUM issue. Its two LOW findings were also corrected.

## Operational Notes

- Application Insights remains disabled by default
- Live Metrics, performance counters, offline storage, logs, and dependency
  auto-instrumentation default to disabled
- exporter redirects, retries, control-plane configuration, Statsbeat, SDK
  statistics, and resource metrics are disabled
- exact aggregate cost remains domain evidence and a decimal-string trace
  attribute, not a floating-point metric
- Slice 11 remains responsible for production lifespan ownership and live Azure
  validation