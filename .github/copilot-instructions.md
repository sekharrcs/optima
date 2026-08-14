# OPTIMA repository instructions for GitHub Copilot

## Mission

Build OPTIMA: a quality-constrained AI execution optimizer for a Tokenomics hackathon.

OPTIMA must optimize the entire execution path, not merely select a model.

The central decision is:

> Select the lowest-cost execution strategy that is expected to satisfy the request's Quality Contract, then verify the result and escalate only when required.

## Mandatory source of truth

Before making architectural or product decisions, read:
- `docs/PRODUCT_SPEC.md`
- `docs/MVP_SCOPE.md`
- `docs/ARCHITECTURE.md`
- `docs/QUALITY_CONTRACT.md`
- `docs/EXECUTION_STRATEGIES.md`
- `docs/DECISIONS.md`

If code and documentation conflict, stop and identify the conflict before silently changing behavior.

## Development workflow

Use a Research -> Plan -> Implement -> Review workflow.

For non-trivial work:
1. Research the relevant repository files and existing tests.
2. State assumptions and unresolved risks in the plan.
3. Produce a file-by-file implementation plan.
4. Implement only the approved/current scope.
5. Add or update tests.
6. Run formatting, linting, type checking, and tests.
7. Review the implementation against the product acceptance criteria.

Prefer small vertical slices over broad scaffolding.

Do not generate large amounts of placeholder code.
Do not implement roadmap features unless explicitly moved into MVP scope.
Do not rewrite unrelated files.

## Architecture rules

Use Python for the backend and optimization engine.
Use FastAPI for HTTP APIs.
Use Streamlit for the hackathon UI unless `docs/DECISIONS.md` is changed.
Use Pydantic models for API/domain contracts.

Keep these concepts separate:
- Quality Contract
- Request Profile
- Planner
- Execution Strategy
- Strategy Executor
- Quality Evaluator
- Cost Calculator
- Telemetry/Run History
- Learning/Policy Statistics

The planner must not contain provider-specific API calls.
Model access must be behind provider/gateway abstractions.
Evaluation logic must not be embedded in UI code.
Cost calculation must use configuration, never scattered hard-coded prices.

## Azure rules

Target an Azure-native deployment.

Preferred services:
- Microsoft Foundry model deployments for LLM inference
- Azure API Management AI gateway for governed model access
- Azure Managed Redis for semantic cache
- Azure Cosmos DB for OPTIMA run history and strategy statistics
- Azure Application Insights / Azure Monitor for operational telemetry
- Azure Container Apps for API and demo UI
- Azure Container Registry for container images
- Azure Key Vault and managed identities for secrets/authentication

Infrastructure must be defined as code using Bicep and orchestrated with Azure Developer CLI (`azd`) where practical.

Never commit secrets, API keys, connection strings, or model credentials.
Prefer managed identity for Azure-to-Azure authentication.
Local development secrets belong in `.env` and `.env` must remain ignored.

## MVP execution strategies

Only these strategies are MVP unless the docs change:

1. `semantic_cache`
   - Look for a sufficiently similar previously accepted result.
   - Return only results that meet cache safety/quality rules.
   - Record cache similarity and avoided model cost.

2. `small_direct`
   - Send the optimized request to the configured small model.

3. `reduce_context_small`
   - Reduce irrelevant context.
   - Call the small model with reduced context.
   - Record before/after token counts.

4. `small_verify_escalate`
   - Call the small model.
   - Evaluate the answer.
   - Return it if the Quality Contract is met.
   - Otherwise call the strong model and evaluate the final result.
   - Record the escalation reason.

5. `strong_direct`
   - Control/baseline strategy.

6. `foundry_model_router`
   - Comparison strategy only.
   - Do not present Microsoft Foundry Model Router as OPTIMA's own innovation.

## Planner principles

The hackathon planner should be deterministic and explainable before attempting ML/RL.

Inputs may include:
- task type
- estimated complexity
- input token count
- context length
- semantic-cache similarity
- Quality Contract
- historical strategy success/cost by task type

Planner output must include:
- selected strategy
- reason codes
- human-readable explanation
- expected quality if available
- expected cost if available

Every decision must be inspectable in the UI.

## Quality rules

Never treat an LLM judge score as objective truth.

Quality evaluation must expose:
- evaluator type
- score
- pass/fail
- reasons
- threshold
- evaluation metadata

Use deterministic evaluators when possible:
- exact/reference checks for deterministic tasks
- test execution for code where feasible
- structured assertions for schema/format requirements

Use LLM-as-judge only for natural-language qualities that cannot be measured deterministically.

A strategy "succeeds" only when its final result meets the Quality Contract.

## Cost and token rules

For every model call record:
- provider/deployment
- input tokens
- output tokens
- cached tokens if exposed
- latency
- calculated cost
- request/run ID

Cost formulas must live in one pricing/catalog component.
Support configuration changes without changing planner code.

Never claim cost savings without a baseline comparison.

## Learning loop

The MVP learning loop is historical policy statistics, not reinforcement learning.

Aggregate by task type, contract profile, and strategy:
- number of executions
- pass rate
- average cost
- average tokens
- average latency
- escalation rate

The planner may use those statistics as evidence for future strategy choices.
Keep this logic explainable.

## Observability

Every run needs a correlation ID.
Capture plan selection, strategy steps, evaluation, token/cost metrics, and final outcome.
Do not log secrets.
Make prompt/output logging configurable because it may contain sensitive data.

## API conventions

Use versioned routes under `/api/v1`.
Return structured error payloads.
Keep health endpoints lightweight.
Use async I/O for external network calls where supported.
Use dependency injection for model clients, stores, and evaluators.

## Python quality bar

Use Python 3.12+.
Use type hints on public functions.
Use `ruff` for lint/format checks.
Use `mypy` for type checking where practical.
Use `pytest` for tests.
Prefer small pure functions in planner/cost logic.
Avoid unnecessary inheritance.
Avoid global mutable state.

## Testing requirements

At minimum add:
- unit tests for Quality Contract translation
- unit tests for planner decisions
- unit tests for cost calculation
- unit tests for quality threshold/pass-fail behavior
- unit tests for escalation
- integration tests with fake model clients
- smoke test for API health

Tests must not require paid model calls by default.
Live Azure model tests must be opt-in.

## Hackathon credibility

For every demo request store a baseline and OPTIMA result so the dashboard can compare:
- total tokens
- total model cost
- latency
- quality
- selected plan
- escalation
- cache/context savings

Do not fabricate savings.
If a metric cannot be measured, mark it unavailable.

## Out of scope unless explicitly approved

- reinforcement learning
- arbitrary agent graphs
- every LLM provider
- GPU/quantization optimization
- autonomous prompt evolution
- production multi-region HA
- enterprise billing
- complex human feedback pipelines
- generalized RAG platform
