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
        +-------------------------------+
        |                               |
        v                               v
Strategy Executor                 Historical Policy Stats
        |
        +--> Semantic Cache
        +--> Small Model
        +--> Context Reducer -> Small Model
        +--> Small Model -> Evaluator -> Strong Model on failure
        +--> Strong Model
        +--> Foundry Model Router comparator
        |
        v
Quality Evaluator
        |
        v
Cost + Token Accounting
        |
        v
Run History / Telemetry
        |
        v
Result + Decision Explanation + Dashboard
```

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
  |       |- strategy outcome statistics
  |
  +--> Application Insights / Azure Monitor
  |
  +--> Key Vault / Managed Identity
```

## Design boundaries

### Planner
Chooses a strategy. It does not invoke models.

### Strategy executor
Runs the selected strategy and emits structured step results.

### Provider abstraction
Hides Microsoft Foundry/APIM request details from strategy code.

### Evaluator
Produces a structured evaluation result. It does not choose the next plan; escalation behavior belongs to the strategy/planner policy.

### Telemetry
Records actual execution facts. It must not silently convert estimates into actuals.

## Local development

Use in-memory/fake implementations for cache, run history, and model clients so core behavior can be developed and tested without Azure resources.

Azure adapters should implement the same interfaces.
