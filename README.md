---
title: OPTIMA
description: Quality-constrained AI execution optimizer for efficient and verifiable model execution
---

# OPTIMA

OPTIMA is a quality-constrained AI execution optimizer.

Instead of asking only "Which model should answer this request?", OPTIMA asks:

> What is the most efficient execution plan that can satisfy the Quality Contract under the selected Optimization Mode?

OPTIMA optimizes the execution path, not only model selection. Depending on the request, it may reuse a safe cached result, reduce context, start with a lower-cost model and verify quality, escalate to a stronger model when required, or go directly to the strong model when a cheaper attempt is expected to waste cost or latency.

This repository is intentionally bootstrapped with specifications and GitHub Copilot instructions before application code is written. The implementation should be created incrementally with GitHub Copilot using the repository as the source of truth.

## MVP execution capabilities

1. Semantic cache
2. Configurable context reduction
3. Small-model first execution with mandatory quality verification and strong-model fallback
4. Strong-model direct execution for HIGH-complexity or policy-required requests
5. Quality evaluation against the Quality Contract
6. Explainable historical policy statistics
7. Microsoft Foundry Model Router as a comparison path, not the OPTIMA differentiator

Planner V1 builds these capabilities into a composable execution plan. Friendly plan labels shown in the UI are presentation names, not separate routing engines.

Examples:

```text
Semantic Cache Hit

Small -> Verify -> Escalate if needed

Context Reduce -> Small -> Verify -> Escalate if needed

Strong -> Verify
```

## Quality Contract

The user selects two independent controls:

- **Quality Profile**: Standard / High / Critical — defines the minimum acceptable quality.
- **Optimization Mode**: Cost / Balanced / Quality — controls how aggressively OPTIMA pursues lower-cost execution paths.

Optimization Mode never lowers the Quality Contract threshold.

Planner V1 does not always try the small model first. Every HIGH-complexity request uses strong-direct execution in V1, and every small-first plan contains a strong fallback if the small result fails quality.

## Development method

Use HVE Core's Research -> Plan -> Implement -> Review workflow with GitHub Copilot.

Before implementing any feature:
1. Read `docs/PRODUCT_SPEC.md`
2. Read `docs/MVP_SCOPE.md`
3. Read `docs/ARCHITECTURE.md`
4. Read the relevant domain specification, especially `docs/PLANNER_V1.md`
5. Research the current code
6. Produce a concrete implementation plan
7. Implement the smallest vertical slice
8. Run tests
9. Review against acceptance criteria

All implementation work must occur on a task branch and merge through a pull request into `main`, as defined in `.github/copilot-instructions.md`.

## Local development

OPTIMA requires Python 3.12 or later and [uv](https://docs.astral.sh/uv/). The project uses the configured Microsoft package feed rather than public PyPI.

Synchronize the locked runtime and development dependencies:

```powershell
uv sync --all-groups
```

Run the FastAPI service locally:

```powershell
uv run uvicorn optima.api.app:app --reload
```

The lightweight health endpoint is available at `http://127.0.0.1:8000/api/v1/health`.

The default API intentionally has no model or evaluator composition. Start the
explicit local demo API to exercise the existing planner and executor with
deterministic fake providers, a fake evaluator, and the centralized price
catalog:

```powershell
uv run uvicorn optima.api.demo:app --port 8000
```

In a second terminal, start the Streamlit decision demo:

```powershell
uv run streamlit run src/ui/app.py
```

The UI uses `http://127.0.0.1:8000` by default. Set `OPTIMA_API_BASE_URL` or use
the advanced demo input to target another configured OPTIMA API.

The local demo remains intentionally narrow:

- Request Profile fields are supplied demo inputs because no backend request
  profiler exists yet.
- The plan executor supports small-first with mandatory verification and strong
  fallback, plus Planner V1 strong-direct execution with mandatory verification.
- Baseline savings remain unavailable until a compatible measured baseline is
  supplied through a future API boundary.
- Dashboard and Run History retain actual results only for the current
  Streamlit session; refreshing or restarting clears them.

## Foundry and APIM provider configuration

Corrective Slice 10A adds an explicit provider composition for an Azure OpenAI
v1 endpoint exposed directly by Microsoft Foundry or through Azure API
Management. The base URL must end at the v1 API root, and each conceptual role
maps to a configured deployment name:

```powershell
$env:OPTIMA_FOUNDRY_BASE_URL="https://<gateway-host>/openai/v1"
$env:OPTIMA_FOUNDRY_SMALL_DEPLOYMENT="<small-deployment-name>"
$env:OPTIMA_FOUNDRY_STRONG_DEPLOYMENT="<strong-deployment-name>"
$env:OPTIMA_FOUNDRY_TIMEOUT_SECONDS="30"
```

Choose one authentication mode. For local API-key development, keep the value
in your untracked `.env` or process environment:

```powershell
$env:OPTIMA_FOUNDRY_AUTH_MODE="API_KEY"
$env:OPTIMA_FOUNDRY_API_KEY="<api-key>"
```

`API_KEY` mode sends the value as the Azure OpenAI `api-key` header. Use it with a
direct Foundry endpoint or an APIM policy that accepts that header. APIM
subscription keys (`Ocp-Apim-Subscription-Key`) and caller bearer tokens to APIM
are out of Slice 10A scope.

For local passwordless development, sign in with Azure CLI and configure the
scope accepted by the APIM inbound policy. A direct Foundry endpoint commonly
uses `https://ai.azure.com/.default`; an APIM-protected API can require its own
application ID URI:

```powershell
az login
$env:OPTIMA_FOUNDRY_AUTH_MODE="AZURE_CLI"
$env:OPTIMA_FOUNDRY_TOKEN_SCOPE="api://<apim-application-id>/.default"
```

For an Azure-hosted deployment, use managed identity. Set the client ID only
for a user-assigned identity:

```powershell
$env:OPTIMA_FOUNDRY_AUTH_MODE="MANAGED_IDENTITY"
$env:OPTIMA_FOUNDRY_TOKEN_SCOPE="api://<apim-application-id>/.default"
$env:OPTIMA_FOUNDRY_MANAGED_IDENTITY_CLIENT_ID="<user-assigned-client-id>"
```

`build_foundry_provider_pair(AppSettings())` creates role-specific providers
that share one asynchronous HTTP client and use only the selected credential.
Call `FoundryProviderPair.aclose()` during application shutdown. The default
`optima.api.app` and deterministic `optima.api.demo` do not invoke this builder,
so importing or starting them never probes Azure credentials.

The adapter sends one request per provider call and does not retry throttling or
transient service failures. Retry policy requires separate execution evidence
and is not part of Slice 10A. Evaluation, escalation, and authoritative cost
calculation remain in their existing OPTIMA components.

## Cosmos DB run-history configuration

Slice 10B adds a provider-independent run-history contract with deterministic
in-memory and Azure Cosmos DB for NoSQL implementations. A configured API saves
the exact terminal `RunResult` after execution and exposes validated history at:

* `GET /api/v1/runs/{run_id}`
* `GET /api/v1/runs?limit=<1-100>`

The default API and demo remain cloud-free and do not configure persistent run
history. History reads return a structured `503` until a store is injected.
The current Streamlit history remains session-local; migrating it to these API
routes is outside Slice 10B.

Configure the account and bounded operational settings:

```powershell
$env:OPTIMA_COSMOS_ENDPOINT="https://<account>.documents.azure.com:443/"
$env:OPTIMA_COSMOS_DATABASE_NAME="<database-name>"
$env:OPTIMA_COSMOS_CONTAINER_NAME="<container-name>"
$env:OPTIMA_COSMOS_HISTORY_LIST_LIMIT="50"
$env:OPTIMA_COSMOS_TIMEOUT_SECONDS="10"
$env:OPTIMA_COSMOS_RETRY_TOTAL="3"
```

Choose exactly one authentication mode. Account-key mode is intended for local
or manual operation, and the key must remain in untracked secret configuration:

```powershell
$env:OPTIMA_COSMOS_AUTH_MODE="ACCOUNT_KEY"
$env:OPTIMA_COSMOS_ACCOUNT_KEY="<account-key>"
```

Azure CLI mode uses only the identity selected by `az login`:

```powershell
az login
$env:OPTIMA_COSMOS_AUTH_MODE="AZURE_CLI"
```

Managed-identity mode uses the system-assigned identity unless a user-assigned
client ID is present:

```powershell
$env:OPTIMA_COSMOS_AUTH_MODE="MANAGED_IDENTITY"
$env:OPTIMA_COSMOS_MANAGED_IDENTITY_CLIENT_ID="<user-assigned-client-id>"
```

The Cosmos container must use `/id` as its partition-key path. The deterministic
recent-history query orders by one descending `sort_key` property, so the
default container index serves it without a composite index. Infrastructure
creation and role assignment remain Slice 11.

`build_cosmos_run_history_resources(AppSettings())` creates one asynchronous
Cosmos client and only the selected credential. Inject its `store` into
`ExecutionDependencies`, retain the returned resources for the application
lifetime, and call `CosmosRunHistoryResources.aclose()` during shutdown. Full
production lifespan composition remains Slice 11.

Persisted evidence uses create-only writes. A duplicate run ID succeeds only
when a point read validates to the same complete `RunResult`; different evidence
is a conflict and is never overwritten. The version 1 document stores exact
`RunResult.model_dump_json()` output as a string so Decimal costs round-trip
without Cosmos binary64 loss. Every read revalidates the strict current domain
model and cross-checks ID, timestamp, and sort-key metadata. Items larger than
Cosmos DB's 2-MB UTF-8 JSON limit fail before write without truncation.

Execution and run-history persistence cannot be atomic because external model
execution precedes storage. `POST /api/v1/runs` returns the authoritative
completed `RunResult` once constructed and reports persistence as a best-effort
side effect through response headers: `X-OPTIMA-Run-History` is `PERSISTED`,
`FAILED`, or `NOT_CONFIGURED`, and `X-OPTIMA-Run-History-Error` carries one
sanitized `RunHistoryErrorCode` only on failure. A failed save is not evidence
that model execution failed, so callers should not resubmit a completed run;
those needing durable history inspect the header instead. Application Insights in
Slice 10D and lifecycle and infrastructure in Slice 11 do not replace this
contract.

No live Cosmos account is exercised by the default test suite. The adapter's
SDK calls, error mapping, and lifecycle are validated with offline fakes.

On Windows ARM64, use an x64 Python 3.12 interpreter for Streamlit because its
Pandas and PyArrow dependencies may not have Windows ARM64 wheels in the
configured package feed.

Run the complete local validation suite:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
```

## Branch workflow

Create a task branch before changing implementation, tests, infrastructure, or feature documentation. Push the completed branch and open a draft pull request targeting `main`. Do not implement directly on or automatically merge into `main`.

## Guiding rule

Do not add features because they sound intelligent. Every feature must improve or protect at least one of:
- measured cost
- token usage
- measured quality
- latency
- explainability
- experimental credibility
