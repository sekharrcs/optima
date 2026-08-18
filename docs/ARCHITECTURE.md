---
title: OPTIMA Architecture
description: Logical architecture, component boundaries, and Azure target for OPTIMA
---

# OPTIMA Architecture

## Logical flow

```text
Client / Streamlit UI
        |
        v
FastAPI API
        |
        v
Quality Contract Builder
        |
        v
Request Profiler
        |
        v
OPTIMA Planner
        |
        +--> Cache Policy
        +--> Context Policy
        +--> Model Policy
        +--> Verification / Escalation Policy
        |
        v
Composable Execution Plan
        |
        v
Plan Executor
        |
        +--> Semantic Cache
        +--> Context Reducer (optional/configurable)
        +--> Small Model (eligible LOW/MEDIUM requests only)
        +--> Quality Evaluator
        +--> Strong Model (direct or fallback)
        +--> Foundry Model Router comparator (optional comparison path)
        |
        v
Cost + Token Accounting
        |
        v
Run History / Historical Policy Statistics / Telemetry
        |
        v
Result + Decision Explanation + Dashboard
```

Planner V1 does not permanently encode every combination as a separate strategy implementation. It composes plan components and derives a friendly plan label for the UI.

Examples:

```text
Semantic Cache Hit

Small -> Verify -> Escalate if needed

Context Reduce -> Small -> Verify -> Escalate if needed

Strong -> Verify
```

## Planner V1 model-policy invariant

The planner has two normal model policies:

- `SMALL_FIRST_WITH_FALLBACK`
- `STRONG_DIRECT`

Every HIGH-complexity request uses `STRONG_DIRECT` in V1.

Every small-first plan includes:
- mandatory quality verification
- `STRONG` fallback if the small result does not meet the Quality Contract

Context reduction is independent of model policy and may be disabled through typed application configuration.

## Azure target

```text
GitHub
  |
  | GitHub Actions / azd
  v
Azure Container Registry
  |
  v
Azure Container Apps
  |- OPTIMA FastAPI
  |- OPTIMA Streamlit UI
  |
  +--> Azure API Management AI Gateway
  |       |
  |       +--> Microsoft Foundry model deployments
  |
  +--> Azure Managed Redis
  |       |- semantic cache
  |
  +--> Azure Cosmos DB
  |       |- run history
  |       |- historical policy statistics
  |
  +--> Application Insights / Azure Monitor
  |
  +--> Key Vault / Managed Identity
```

## Design boundaries

### Quality Contract Builder
Translates user-facing Quality Profile / Optimization Mode controls into explicit domain values and thresholds.

### Request Profiler
Describes the request using task type, complexity, token/context characteristics, risk, and cache eligibility.
It does not choose a model.

### Planner
Builds the composable execution plan.
It does not invoke models, evaluators, Redis, Cosmos DB, or provider SDKs.

`docs/PLANNER_V1.md` is authoritative for planner behavior.

### Plan Executor
Executes the selected plan components in order and emits structured step results.
It honors the planner's verification/escalation policy rather than inventing routing decisions.

### Provider abstraction
Hides Microsoft Foundry/APIM request details from planner and execution-policy logic.
Maps conceptual model roles such as `SMALL` and `STRONG` to configured deployments.

### Evaluator
Produces a structured evaluation result against the Quality Contract.
It measures quality; it does not independently choose the next model.

### Cost Calculator
Calculates measured cost from actual model usage using centralized pricing configuration.
It is separate from planner logic.

### Historical Policy Statistics
Stores aggregated evidence such as task/profile/mode-specific pass rate, cost, latency, and escalation rate.
Planner V1 may use this evidence only within the guardrails defined in `docs/PLANNER_V1.md`.

### Telemetry
Records actual execution facts including correlation ID, plan, steps, usage, latency, evaluation, escalation, and final outcome.
It must not silently convert predictions/estimates into measured actuals.

## Module configuration

Optional optimizer capabilities such as semantic cache, context reduction, and historical policy are controlled through typed configuration as defined in `docs/MODULE_CONFIGURATION.md`.

Quality evaluation is not a normal optional optimizer flag for production/hackathon execution.

## Local development

Use in-memory/fake implementations for cache, run history, historical statistics, evaluator behavior, and model clients so core planner/executor behavior can be developed and tested without Azure resources or paid model calls.

Azure adapters should implement the same interfaces.
