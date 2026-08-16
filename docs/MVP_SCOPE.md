# OPTIMA Hackathon MVP Scope

## Must have

### Request execution
- Accept a natural-language request plus optional context.
- Accept a Quality Contract including Quality Profile and Optimization Mode.
- Profile the request.
- Build a composable execution plan.
- Execute the plan.
- Evaluate final quality.
- Escalate an eligible small-first execution to the strong model when the Quality Contract is not met.
- Record tokens, cost, latency, plan steps, evaluation results, and decision reasons.
- Return the final result plus an explainable decision trace.

### Planner V1

Planner V1 composes an execution plan from:
- cache policy
- context policy
- model policy
- verification / escalation policy

The normal V1 model policies are:
- `SMALL_FIRST_WITH_FALLBACK`
- `STRONG_DIRECT`

A semantic-cache hit may bypass model execution when reuse is safe and compatible with the current Quality Contract.

V1 invariants:
- every HIGH-complexity request uses `STRONG_DIRECT`
- every small-first plan includes quality verification and a configured strong-model fallback
- Optimization Mode never lowers the minimum quality threshold
- historical policy cannot move HIGH-complexity work from strong-direct to small-first in V1

### Execution capabilities
- semantic cache
- configurable context reduction
- small-model first execution
- quality verification
- strong-model direct execution / escalation
- historical policy statistics
- Foundry Model Router comparison adapter if time permits

Context reduction and semantic cache must be independently bypassable through typed configuration without redesigning planner logic.

### Evaluation
- pluggable evaluator interface
- deterministic evaluator examples
- natural-language LLM-judge evaluator where deterministic evaluation is insufficient
- explicit pass/fail threshold from the Quality Contract
- mandatory evaluation for normal model-executed OPTIMA runs that claim contract compliance

### Experiment/dashboard
- run baseline and OPTIMA on the same demo dataset
- aggregate tokens, cost, latency, and Quality Contract pass rate
- show execution-plan/component distribution
- show escalation and cache metrics
- show context-reduction metrics when enabled
- inspect an individual decision and execution trace
- distinguish measured values from estimates

### UI
- Streamlit MVP
- exactly three primary views: Execute, Dashboard, Run History
- render planner reason codes and backend execution facts rather than inventing explanations or metrics
- no module configuration/admin screen in MVP

### Azure
- model inference through Microsoft Foundry
- deploy API/UI to Azure Container Apps
- application telemetry to Application Insights / Azure Monitor
- Azure-native persistent run history
- infrastructure as code
- GitHub Actions deployment

## Should have
- Azure Managed Redis semantic cache
- API Management AI gateway
- historical policy statistics feeding eligible planner decisions
- reusable benchmark dataset runner

## Could have
- Microsoft Foundry Model Router as comparator
- exportable experiment report
- configurable model price-catalog admin view

## Explicitly not MVP
- reinforcement learning
- autonomous arbitrary plan generation
- multi-agent product runtime
- multi-cloud model marketplace
- production HA/DR
- self-modifying prompts
- full module-configuration admin UI
