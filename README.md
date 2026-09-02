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

The Azure-backed API uses a separate factory and requires an explicit production
semantic-cache decision. Foundry, Cosmos, user-assigned identity, Application
Insights, evaluator, and active-role pricing settings remain mandatory. Redis
and embedding settings are mandatory only when cache is enabled:

```powershell
uv run uvicorn optima.api.production:create_production_app --factory --port 8000
```

The production lifespan creates one dependency graph and closes constructed
resources in reverse ownership order. Enabled mode validates or creates the
Redis index before readiness. Disabled mode constructs no embedding or Redis
resource and reports typed disabled evidence without cache-hit or cache-savings
claims.

Production selects one explicit evaluator mode. `EXACT_REFERENCE` remains a
benchmark mode that requires `reference_output` before any paid work.
`LLM_JUDGE` serves reference-free requests with a separately configured JUDGE
deployment and fail-closed response evidence. The checked-in hackathon profile
selects `LLM_JUDGE`; it does not infer or silently switch modes.

Monetary cost measurement is optional. The runtime assembles its price catalog
from configured SMALL and STRONG rates plus JUDGE rates in LLM-judge mode.
Embedding pricing is included only when semantic cache is enabled. Absent rates
keep monetary cost unavailable while token usage stays measured, and partial
active-role rates are rejected. Enabling
`OPTIMA_PRODUCTION_COST_MEASUREMENT_REQUIRED` makes startup fail closed unless a
complete active-role catalog is supplied.

## Container images

`Dockerfile.api` packages the production FastAPI factory on port `8000`.
`Dockerfile.ui` packages Streamlit on port `8501`. Both images install the
checked-in lockfile without the development dependency group and run as UID and
GID `10001`.

```powershell
docker build --file Dockerfile.api --tag optima-api:local .
docker build --file Dockerfile.ui --tag optima-ui:local .
docker run --rm --env-file .env -p 8000:8000 optima-api:local
docker run --rm -e OPTIMA_API_BASE_URL=http://host.docker.internal:8000 -p 8501:8501 optima-ui:local
```

The production API command requires real Azure configuration. Use
`optima.api.demo:app` as an explicit command override for cloud-free API smoke
testing; production never falls back to demo providers. Container Apps image
parameters accept manifest digests (`sha256:...`), not mutable tags.

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
creation and role assignment remain Slice 11C.

`build_cosmos_run_history_resources(AppSettings())` creates one asynchronous
Cosmos client and only the selected credential. Inject its `store` into
`ExecutionDependencies`, retain the returned resources for the application
lifetime, and call `CosmosRunHistoryResources.aclose()` during shutdown. The
production factory performs this composition and cleanup.

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

## Azure Managed Redis semantic-cache configuration

Slice 10C adds a read-only Azure Managed Redis implementation of the existing
semantic-cache lookup contract. Azure Managed Redis must be provisioned with
RediSearch, Enterprise clustering, and `NoEviction`. The adapter connects over
TLS on port `10000`, verifies the server hostname, uses RESP2 for a stable raw
`FT.SEARCH` response, and performs no Redis command retries.

Provision a HASH index before starting the configured API. Replace the vector
dimension with the exact output dimension of the injected embedding provider:

```text
FT.CREATE optima-cache-v1 ON HASH PREFIX 1 optima:semantic-cache: SCHEMA schema_version TAG embedding_profile TAG task_type TAG complexity TAG embedding VECTOR FLAT 6 TYPE FLOAT32 DIM <embedding-dimension> DISTANCE_METRIC COSINE
```

Each indexed hash uses schema version `1` and contains these fields:

* `schema_version`, with the value `1`
* `embedding_profile`, the SHA-256 identity of the embedding model, deployment,
  and dimension (see below)
* `task_type` and `complexity`, using the domain enum values
* `embedding`, encoded as a finite, nonzero, little-endian `FLOAT32` vector
* `source_run_id` and `output_text`
* `request_binding_json`, containing a complete serialized `RequestBinding`
* `prior_evaluation_json`, containing a complete serialized `EvaluationResult`
* `contract_compatible` and `safe_to_reuse`, each encoded as `true` or `false`

The adapter runs one `KNN 1` COSINE query filtered by schema version, task type,
complexity, and embedding profile, derives similarity as
`max(0, 1 - vector_distance)` (the cosine similarity clamped into the domain
`[0, 1]` range, so a negative cosine similarity maps to `0`), and strictly
validates all returned evidence. It does not compare the Planner V1 similarity
threshold, current Quality Contract threshold, request binding, compatibility,
or safety. Planner V1 remains authoritative for those gates. Cache population,
invalidation, and write-back remain outside Slice 10C.

Stored and query vectors are bound to a strict, versioned embedding profile
(`embedding-profile-v1`) that hashes the embedding model, deployment, and
dimension into an injection-safe `embedding_profile` tag. The adapter requires
the injected provider to declare that exact profile, filters retrieval by it,
and rejects any stored record whose profile differs — so vectors from a
different model or dimension are never compared as if they were compatible.
Composition also fails fast when the provider profile and the Redis index
profile disagree. The injected embedding provider receives a versioned,
canonical semantic-input payload derived from the request input text and
optional context to produce the query vector, so that boundary must satisfy the
deployment's privacy requirements.

Configure the endpoint, existing index, embedding profile, and bounded client
settings:

```powershell
$env:OPTIMA_REDIS_HOST="<name>.<region>.redis.azure.net"
$env:OPTIMA_REDIS_INDEX_NAME="optima-cache-v1"
$env:OPTIMA_REDIS_EMBEDDING_DIMENSION="1536"
$env:OPTIMA_REDIS_EMBEDDING_MODEL="text-embedding-3-small"
$env:OPTIMA_REDIS_EMBEDDING_DEPLOYMENT="<embeddings-deployment-name>"
$env:OPTIMA_REDIS_TIMEOUT_SECONDS="1"
$env:OPTIMA_REDIS_MAX_CONNECTIONS="10"
```

Choose exactly one authentication mode. Access-key mode keeps the key in
untracked secret configuration:

```powershell
$env:OPTIMA_REDIS_AUTH_MODE="ACCESS_KEY"
$env:OPTIMA_REDIS_ACCESS_KEY="<access-key>"
```

Azure CLI mode uses only the signed-in Azure CLI identity. Redis AUTH requires
that identity's object ID, not an application or managed-identity client ID:

```powershell
az login
$env:OPTIMA_REDIS_AUTH_MODE="AZURE_CLI"
$env:OPTIMA_REDIS_OBJECT_ID="<signed-in-identity-object-id>"
```

Managed-identity mode also requires the identity principal's object ID. Set the
client ID only when selecting a user-assigned identity:

```powershell
$env:OPTIMA_REDIS_AUTH_MODE="MANAGED_IDENTITY"
$env:OPTIMA_REDIS_OBJECT_ID="<managed-identity-object-id>"
$env:OPTIMA_REDIS_MANAGED_IDENTITY_CLIENT_ID="<user-assigned-client-id>"
```

`build_redis_semantic_cache_resources(AppSettings(), embedding_provider)`
creates one application-lifetime client and only the selected credential. Inject
its `cache` into `ExecutionDependencies`, retain the returned resources, and call
`RedisSemanticCacheResources.aclose()` during application shutdown. The resource
owner renews Microsoft Entra tokens with a bounded acquisition timeout and
bounded retries that apply only to transient failures — authentication and
authorization errors stop renewal immediately, every scheduled refresh and retry
must fit its full next-attempt budget (delay plus operation timeout plus a
pre-expiry margin defaulting to Microsoft's recommended three minutes) before the
relevant token expires, reauthentication retries are measured against the current
still-serving token, and a renewed token is published only after the pool accepts
it — and stops renewal before closing Redis and Azure Identity. The default API
and deterministic demo do not call this builder or probe Azure credentials.

`build_foundry_embedding_provider(AppSettings())` composes the production
embedding provider from the Slice 10A Foundry base URL and authentication mode
(API key, Azure CLI, or managed identity — never `DefaultAzureCredential` and
never a fallback) plus the Redis embedding profile. It calls one Azure OpenAI v1
`/embeddings` request per lookup with no retry, strictly validates the response
(exactly one embedding at the expected index, exact dimension, finite non-boolean
values, a `model` that matches the configured profile, `total_tokens` equal to
`prompt_tokens`, and non-fabricated usage), and never leaks the endpoint, token,
prompt, or response body. A deterministic `FakeEmbeddingProvider` serves offline
tests. The production FastAPI lifespan owns and closes these resources.

A cache lookup against a paid embeddings deployment consumes input tokens and
cost even when it produces a hit, and a failed request may already have been
billed. The lookup therefore carries a typed `EmbeddingAttempt` — recording
whether the provider was invoked, whether an outbound request may have been
attempted, and any measured usage priced through the same authoritative cost
catalog as model calls. `RunResult.total_input_tokens`, `total_tokens`, and
`total_calculated_cost` include measured embedding consumption and become `None`
when an attempt was possibly billed but returned no usage; a failure proven to
occur before any outbound request leaves model-only totals intact. Output-token
totals stay exact because embeddings produce no output tokens. A cache hit is
never reported as free when an embedding request was made.

This project deliberately implements the streaming credential provider directly
on `azure-identity` rather than adopting `redis-entraid`. The direct
implementation supports explicit `AzureCliCredential` and
`ManagedIdentityCredential` modes consistent with the Foundry and Cosmos slices,
uses the configured identity object ID as the Redis `AUTH` username, and keeps
token acquisition, renewal bounds, and failure reporting under OPTIMA's own
typed control without a `DefaultAzureCredential` chain.

No live Redis resource is exercised by the default test suite. Lookup parsing,
query construction, embedding-profile enforcement, authentication selection,
token renewal, client options, and lifecycle are validated with offline fakes.

## Application Insights observability

Corrective Slice 10D adds provider-independent run and stage observation backed
by the Azure Monitor OpenTelemetry exporter. Observability is disabled by default.
The default API and deterministic demo therefore create no exporter, Azure
credential, background telemetry thread, network request, or offline telemetry
file.

Enable Application Insights only in an explicitly composed API process:

```powershell
$env:OPTIMA_APPLICATION_INSIGHTS_ENABLED="true"
$env:OPTIMA_APPLICATION_INSIGHTS_CONNECTION_STRING="InstrumentationKey=<uuid>;IngestionEndpoint=https://<region>.in.applicationinsights.azure.com/"
$env:OPTIMA_APPLICATION_INSIGHTS_SERVICE_NAME="optima-api"
$env:OPTIMA_APPLICATION_INSIGHTS_SERVICE_VERSION="0.1.0"
$env:OPTIMA_APPLICATION_INSIGHTS_DEPLOYMENT_ENVIRONMENT="demo"
$env:OPTIMA_APPLICATION_INSIGHTS_SAMPLING_RATIO="1.0"
```

`AppSettings` rejects enabled telemetry without a syntactically valid
connection string before exporter construction. The connection string must
include an instrumentation-key UUID and an explicit credential-free HTTPS
Azure Application Insights ingestion endpoint. Ambiguous suffix-based endpoint
construction, unknown fields, plaintext endpoints, URL credentials, queries,
and fragments are rejected. `create_app` resolves the configured observer when
it receives `ExecutionDependencies`; tests can inject `InMemoryObservability`
instead. Repeated application composition reuses one identically configured
process-wide Azure Monitor runtime and rejects a conflicting second
configuration before initialization. Each composition receives a close-once
lease; the final lease closes the owned providers, and the closed registry
rejects reconstruction instead of returning a stale runtime.

The default privacy and volume controls are explicit:

- Azure Monitor log export is disabled
- Live Metrics and performance counters are unsupported by the isolated adapter;
  typed configuration rejects enabling either feature
- offline retry storage is disabled
- no distro auto-instrumentation package is installed; OPTIMA creates only its
  custom FastAPI, run, and stage spans
- local tracer and meter providers receive an explicit resource containing only
  validated service name, service version, and deployment environment
- the adapter never mutates OpenTelemetry/Azure Monitor environment settings or
  replaces process-global providers; ambient values do not control OPTIMA's
  providers, resources, root sampling, or custom-metric routing
- control-plane configuration, Statsbeat, SDK statistics, resource metrics, and
  raw dependency logs are suppressed on OPTIMA-owned exporters
- Azure Core transport retries are disabled (`retry_total=0`); the installed
  exporter hardcodes `RedirectPolicy(permit_redirects=False)` for the pipeline
  and separately reads `redirect_max` for its manual 307/308 recursion; OPTIMA
  sets `redirect_max=0`, so neither path follows a redirect
- one custom FastAPI server span is created per non-health request
- request and response bodies, headers, query strings, raw URLs, user IDs,
  endpoints, exception messages, and exception stack contents are not exported
- `/api/v1/health` is excluded by default

The sampling ratio is a validated parent-based trace-ID ratio from `0.0` through
`1.0`. The root decision uses the configured ratio, while local and remote child
spans preserve their parent's sampled flag. The default `1.0` retains complete
traces for the hackathon demo; production operators should lower it to control
ingestion cost. Sampling applies only to traces. Terminal metrics are projected
exactly once from each validated `RunResult` and remain unsampled. Concurrent
finalizers serialize on the run observation. A partial metric failure is not
retried or reported as complete; one failed projection span and one redacted
warning expose the incomplete telemetry without retrying business work.

When complete pricing evidence exists, `optima.run.total_cost_exact` is the
numerically canonical fixed-point text of the terminal `Decimal`. It never uses
float or scientific notation, emits zero as `0`, and removes insignificant
fractional trailing zeros. It preserves the exact numeric value, not the input
Decimal's original exponent or trailing-zero representation. Incomplete pricing
evidence omits the attribute.

Application Insights configuration also supports these explicit switches:

```powershell
$env:OPTIMA_APPLICATION_INSIGHTS_OFFLINE_STORAGE_ENABLED="false"
$env:OPTIMA_APPLICATION_INSIGHTS_FASTAPI_INSTRUMENTATION_ENABLED="true"
$env:OPTIMA_APPLICATION_INSIGHTS_EXCLUDE_HEALTH_ROUTES="true"
```

`OPTIMA_APPLICATION_INSIGHTS_LIVE_METRICS_ENABLED` and
`OPTIMA_APPLICATION_INSIGHTS_PERFORMANCE_COUNTERS_ENABLED` must remain `false`.
The installed SDK implements them with process-global singleton state, so the
isolated adapter rejects `true` before constructing an exporter.

The adapter exposes idempotent `force_flush()` and `close()` operations. The
production FastAPI lifespan invokes close during deterministic reverse cleanup.
A pre-existing process-wide OpenTelemetry installation can coexist with OPTIMA:
the adapter uses separate local providers and never replaces or shuts down host
providers. Runtime initializer failures
are cached as unavailable and are not retried: run behavior stays unchanged,
`force_flush()` returns `false`, and one bounded warning omits connection data
and raw exception text. Typed configuration conflicts still fail fast. No live
Application Insights resource is exercised by the default test suite.

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
