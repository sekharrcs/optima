---
title: Application Insights tracing and operational metrics
description: Draft pull request summary for OPTIMA Corrective Slice 10D
---

## Summary

- Add a provider-independent observability boundary with no-op, deterministic
  in-memory, and OpenTelemetry implementations
- Trace the actual contract, cache, planner, context, model, evaluator,
  persistence, and terminal-outcome operations beneath one OPTIMA run
- Configure Azure Monitor Application Insights through direct exporters and
  locally owned OpenTelemetry providers
- Emit bounded metrics from validated terminal evidence without weakening exact
  cost or missing-measurement semantics
- Preserve every existing planner, provider, evaluator, cache, persistence, API,
  and cost behavior

## Validation

- `uv lock --check`: passed
- `uv sync --frozen --all-groups --dry-run`: passed
- `ruff format --check .`: passed (116 files)
- `ruff check .`: passed
- `mypy src tests`: passed (98 source files)
- focused observability tests (`tests/test_observability.py`): 54 passed
- focused observability/settings/health/API tests: 162 passed
- full `pytest`: 1,028 passed with one existing FastAPI TestClient deprecation
  warning
- `git diff --check`: passed
- added-content secret-pattern scan: zero matches
- project dependency-source scan: zero `pypi.org` or `pythonhosted.org`
  matches in `pyproject.toml` and `uv.lock`

Three independent adversarial review passes found and then verified remediation
of ambient-resource leakage, endpoint validation, startup failure isolation,
global-provider ownership, parent sampling, cleanup after recorder failure,
exporter retries, SDK background components, flush deadlines, fake-observer
identity, and duplicate sample configuration. The third pass additionally
grounded every Azure Monitor claim in the installed `azure-monitor-opentelemetry`
`1.8.9` source and corrected two truthfulness defects: small exact costs that
rendered in scientific notation, and imprecise redirect documentation. A later
source/runtime check proved `redirect_max` is consumed by the exporter's manual
307/308 path, so OPTIMA restores `redirect_max=0`. It added offline regressions for
fixed-point cost, installed-SDK configuration and sampler resolution,
concurrent-run span isolation, cancellation preservation, exactly-once partial
metric projection, and a disabled-mode subprocess proof.

Focused round-two verification found three HIGH operability/ownership defects
and one MEDIUM representation ambiguity. A closed registry could return a stale
observer, export-time SDK flags required stronger process isolation, enabled
initialization failure looked like successful no-op telemetry, and "canonical"
did not distinguish numerical normalization from Decimal exponent preservation.
The correction now:

- issues close-once runtime leases, closes providers after the final lease, and
  rejects reconstruction after registry close
- replaces the process-global distro path with direct exporters and local
  providers, leaving host environment, providers, and SDK classes unchanged;
  temporary same-thread log filters preserve concurrent host diagnostics
- marks failed enabled initialization unavailable through `force_flush() ==
  false` plus one redacted warning
- serializes terminal projection, never retries a partial metric batch, and
  emits one failed projection span plus one redacted warning
- defines exact cost text as numerical fixed-point canonicalization: no float,
  no scientific notation, zero as `0`, and no insignificant fractional zeros

Subprocess regressions exercise disabled mode and the actual direct exporter
initialization beside pre-existing host providers without sending telemetry.

## Operational Notes

- Application Insights remains disabled by default
- Live Metrics and performance counters are rejected; offline storage and logs
  default to disabled; dependency auto-instrumentation is not installed
- exporter transport retries, control-plane configuration, Statsbeat, SDK
  statistics, and resource metrics are disabled
- Azure Core automatic redirects are disabled by the exporter, and
  `redirect_max=0` disables its separate manual 307/308 recursion
- exact aggregate cost remains domain evidence and a numerically canonical
  fixed-point decimal-string trace attribute, not a floating-point metric
- initialization failure is detectable but cannot alter a run result or expose
  raw exception, credential, connection-string, or endpoint text
- Slice 11 remains responsible for production lifespan ownership and live Azure
  validation