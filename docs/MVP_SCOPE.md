# OPTIMA Hackathon MVP Scope

## Must have

### Request execution
- Accept a natural-language request plus optional context.
- Accept a Quality Contract profile.
- Profile the request.
- Select one supported execution strategy.
- Execute it.
- Evaluate final quality.
- Record tokens, cost, latency, strategy steps, and decision reasons.
- Return result plus explanation.

### Strategies
- semantic cache
- small direct
- context reduction -> small
- small -> evaluate -> escalate to strong
- strong direct baseline
- Foundry Model Router comparison adapter if time permits

### Evaluation
- pluggable evaluator interface
- deterministic evaluator examples
- natural-language LLM judge evaluator
- explicit pass/fail threshold

### Experiment/dashboard
- run baseline and OPTIMA on a demo dataset
- aggregate tokens, cost, latency, quality
- show per-strategy distribution
- show escalation and cache metrics
- inspect an individual decision

### Azure
- model inference through Microsoft Foundry
- deploy API/UI to Azure Container Apps
- application telemetry to Application Insights
- Azure-native persistent run history
- infrastructure as code
- GitHub Actions deployment

## Should have
- Azure Managed Redis semantic cache
- API Management AI gateway
- historical policy statistics feeding planner decisions
- reusable benchmark dataset runner

## Could have
- Microsoft Foundry Model Router as comparator
- exportable experiment report
- configurable model price catalog admin view

## Explicitly not MVP
- reinforcement learning
- autonomous arbitrary plan generation
- multi-agent product runtime
- multi-cloud model marketplace
- production HA/DR
- self-modifying prompts
