# OPTIMA repository instructions for GitHub Copilot

## Mission

Build OPTIMA: a quality-constrained AI execution optimizer for a Tokenomics hackathon.

OPTIMA must optimize the entire execution path, not merely select a model.

The central decision is:

> Select the most efficient execution plan allowed by the Quality Contract and Optimization Mode, verify the result, and escalate when required.

## Mandatory source of truth

Before making architectural or product decisions, read:
- `docs/PRODUCT_SPEC.md`
- `docs/MVP_SCOPE.md`
- `docs/ARCHITECTURE.md`
- `docs/QUALITY_CONTRACT.md`
- `docs/EXECUTION_STRATEGIES.md`
- `docs/PLANNER_V1.md`
- `docs/UI_SPEC.md`
- `docs/MODULE_CONFIGURATION.md`
- `docs/DECISIONS.md`

If code and documentation conflict, stop and identify the conflict before silently changing behavior.

## Git workflow — mandatory

`main` is the protected integration branch.

Never implement application code, tests, infrastructure, or feature documentation directly on `main`.

Before starting implementation work:
1. Confirm the current branch.
2. If currently on `main`, create and switch to a feature branch before editing implementation files.
3. Use branch naming:
   - `feature/<short-description>` for product features
   - `fix/<short-description>` for fixes
   - `docs/<short-description>` for documentation-only work
   - `infra/<short-description>` for infrastructure work
4. Perform implementation and commits only on the working branch.
5. Run required validation before proposing merge.
6. Open a pull request targeting `main`.
7. Do not automatically merge the pull request unless explicitly instructed.

If branch creation fails, stop implementation rather than modifying `main`.

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
- Execution Plan
- Strategy Executor
- Quality Evaluator
- Cost Calculator
- Telemetry/Run History
- Learning/Policy Statistics

Planner V1 is defined by `docs/PLANNER_V1.md`.
The planner must build a composable execution plan across cache, context, model, verification, and escalation policies.

The planner must not contain provider-specific API calls.
Model access must be behind provider/gateway abstractions.
Evaluation logic must not be embedded in UI code.
Cost calculation must use configuration, never scattered hard-coded prices.

Optional optimizer capabilities such as semantic cache, context reduction, and historical policy must be controlled through typed configuration as defined in `docs/MODULE_CONFIGURATION.md`.
Do not treat quality evaluation as a normal enable/disable optimization flag.

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

## MVP execution capabilities

Planner V1 composes the following capabilities into an execution plan:

1. Semantic cache
   - Look for a sufficiently similar previously accepted result.
   - Return only results that meet cache safety/quality rules.
   - Record cache similarity and avoided model cost.

2. Context reduction
   - Optional, configurable optimization.
   - Reduce irrelevant context when policy permits.
   - Record before/after token counts.
   - Must be bypassable through typed configuration.

3. Small model
   - Lower-cost first-line model role.

4. Quality verification
   - Evaluate output against the Quality Contract.

5. Strong-model escalation
   - Used when policy requires strong direct execution or small-model quality is insufficient.

6. Foundry Model Router comparator
   - Comparison strategy only.
   - Do not present Microsoft Foundry Model Router as OPTIMA's own innovation.

Friendly UI strategy labels may combine these capabilities, e.g.:
`Context Reduce -> Small -> Verify -> Escalate if needed`.

## Planner principles


The hackathon planner must be deterministic and explainable before attempting ML/RL.

Inputs may include:
- task type
- estimated complexity
- input token count
- context length
- module configuration
- semantic-cache similarity
- Quality Contract
- historical strategy success/cost by task type

Planner output must include:
- selected plan components
- reason codes
- human-readable explanation/plan name
- expected quality if available
- expected cost if available

Every decision must be inspectable in the UI.

- OPTIMA does not always attempt the small model first.
- Every `HIGH`-complexity request uses `STRONG_DIRECT` in Planner V1, regardless of Quality Profile or Optimization Mode.
- Every V1 plan that starts with `SMALL` must include quality verification and a configured `STRONG` fallback.
- There is no normal `small_direct_without_fallback` policy in V1.
- Historical policy may move an eligible small-first request to strong-direct when small-first performance is poor.
- Historical policy must not move a HIGH-complexity V1 request from strong-direct to small-first.
- Context reduction remains independent of model policy and may be disabled through typed configuration.

Planner V1 is authoritative in `docs/PLANNER_V1.md`.

Quality Profile defines the minimum acceptable quality threshold.
Optimization Mode (`COST`, `BALANCED`, `QUALITY`) controls how aggressively the planner pursues lower-cost execution plans. It must materially affect Planner V1 behavior and must never lower the Quality Contract threshold.

The planner builds a composable execution plan across cache, context, model, and verification/escalation policies. Friendly UI plan names are derived labels, not separate routing implementations.

## Quality rules

Quality evaluation is mandatory for any normal OPTIMA execution that claims Quality Contract compliance.
Tests and local development may inject fake evaluators, but the production/hackathon path must not silently disable evaluation.

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
- unit tests for Planner V1 decisions
- unit tests for module-disabled planner behavior
- unit tests for cost calculation
- unit tests for quality threshold/pass-fail behavior
- unit tests for escalation
- integration tests with fake model clients
- smoke test for API health
- unit tests proving every HIGH-complexity planner combination selects strong-direct
- unit tests proving every small-first plan has strong fallback
- integration test proving failed small-model quality escalates exactly once
- integration test proving passing small-model quality avoids the strong-model call

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
- full module-configuration admin UI
