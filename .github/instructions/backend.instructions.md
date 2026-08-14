---
applyTo: "src/backend/**/*.py,src/optima/**/*.py"
---
# Backend-specific instructions

Keep HTTP/API concerns separate from OPTIMA domain logic.

Recommended layering:
- `domain/`: Pydantic/domain models and enums
- `planner/`: request profiling and plan selection
- `strategies/`: strategy implementations
- `providers/`: model/gateway abstractions
- `evaluation/`: quality evaluators
- `cost/`: token/cost calculation
- `storage/`: cache, run history, policy statistics
- `telemetry/`: tracing/metrics
- `api/`: FastAPI routes and dependencies

Domain and planner tests should use fake/in-memory dependencies.
Do not require Azure for unit tests.
