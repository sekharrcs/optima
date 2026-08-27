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
GitHub Actions
        |
        | OIDC (later slice)
        v
Subscription-scope Bicep
        |
        +--> rg-optima-hackathon
                |
                +--> Azure Container Registry Basic
                +--> Azure Container Apps Consumption
                |     |- internal OPTIMA FastAPI API
                |     |- public OPTIMA Streamlit UI
                +--> Azure Cosmos DB for NoSQL serverless
                +--> Azure Managed Redis Balanced B0
                +--> Log Analytics + Application Insights
                +--> separate API and UI managed identities
                +--> optional runtime access assignments

OPTIMA API --> Foundry or APIM Azure OpenAI v1 endpoint
```

Slices 11A and 11B omit Key Vault because runtime service authentication uses managed
identity. The generated Application Insights connection string identifies the
telemetry destination and is not a security token. APIM and Foundry resources
remain external reviewed inputs because the current provider supports either
direct Foundry or an APIM gateway and no gateway is required by application
behavior.

Public Azure service endpoints are an intentional hackathon tradeoff. Cosmos
local auth, Redis access keys, and ACR admin credentials are disabled. The API
is internal to the Container Apps environment because it has no caller
authentication; only the UI has public ingress.

See `docs/AZURE_INFRASTRUCTURE.md` for resource configuration, configuration
mapping, identity scopes, provider registrations, cost controls, and deployment
gates.

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

### Semantic cache runtime boundary

The application resolves at most one provider-independent semantic-cache match
after building the current Quality Contract and before invoking Planner V1. The
lookup returns a typed cached output with source-run identity, similarity, prior
evaluation, complete source-request binding, contract compatibility, and
reuse-safety evidence. The API normalizes and deeply revalidates adapter values
inside the lookup failure boundary before planning. Malformed adapter values
produce a typed lookup failure and normal model fallback. The adapter does not
decide whether reuse is allowed.

The request binding is a non-sensitive, versioned SHA-256 fingerprint of
canonical typed JSON. It covers input text, original context, reference output,
ordered criteria, caller metadata, task type, and complexity. The API derives it
before lookup, the lookup request verifies it, Planner V1 compares it with the
candidate binding, and every plan snapshots it with the selected Quality
Profile. The serialized binding exposes task type and complexity for profile
verification but no raw input, context, reference, criteria, or metadata.
`ExecutionRequest` recomputes the digest from complete request facts.
`RunResult` verifies profile identity, Quality Profile, Optimization Mode, and
binding equality across the planner assessment, accepted candidate when
present, plan, and runtime evidence.

Planner V1 remains authoritative for every threshold, quality, compatibility,
and safety gate. Every assessed match produces a detached planner assessment
containing the candidate binding, source identity, similarity, prior evaluation,
compatibility, and safety facts without the cached output. Rejected runtime
evidence must match that assessment. When Planner V1 accepts the match, the
selected plan also carries the exact resolved value. The executor consumes the
bound snapshot and never performs a second lookup, preventing a different output
or source run from being substituted between planning and execution.

A binding mismatch is a typed rejected match and continues through normal model
execution. All semantic-cache outcomes share one domain contract for module and
profile state, planner reason, candidate assessment, source and error evidence,
cache policy, execution-step status, and exact event codes. Enabled
cache-eligible requests must carry an outcome. Attempted lookups produce one
leading cache step; disabled and ineligible bypasses produce none. A reused hit
terminates only with one successful cache step and one successful return step.
Model paths require contiguous step sequences and causal model, evaluation,
escalation, and return ordering that matches the selected model policy. A
successful return belongs only to a completed run and carries exact model-role
and contract-result facts. A failed return is allowed only when invalid final
evaluator evidence causes the run to fail closed.

A healthy miss, rejected match, lookup failure, or timeout continues through the
existing context and model path. Typed runtime evidence distinguishes those
outcomes. An enabled application without a semantic-cache dependency fails as a
structural configuration error before model execution. The local in-memory
implementation remains deterministic test and demo infrastructure only.

The Slice 10C Azure Managed Redis adapter is read-only. The injected embedding
provider returns a vector bound to a strict, versioned embedding profile
(`embedding-profile-v1`, hashing the embedding model, deployment, and dimension).
The adapter validates and encodes the vector as a finite, nonzero, little-endian
`FLOAT32` buffer, requires the returned provider profile to equal the configured
profile, and sends one bounded `KNN 1` COSINE query against a pre-provisioned
HASH index filtered by schema version, task type, complexity, and embedding
profile. It derives the candidate similarity from Redis vector distance, rejects
any record whose stored embedding profile differs, and strictly reconstructs the
complete `CacheCandidate`; it does not apply any Planner V1 reuse gate. The
source `RequestBinding` is returned unchanged.

Redis schema version 1 contains:

* `schema_version`
* TAG fields `embedding_profile`, `task_type`, and `complexity`
* the `embedding` VECTOR field, configured as `FLAT`, `FLOAT32`, and `COSINE`
* `source_run_id` and `output_text`
* complete `request_binding_json` and `prior_evaluation_json` payloads
* canonical `contract_compatible` and `safe_to_reuse` booleans

Malformed, unsupported, non-finite, incomplete, or contradictory Redis evidence
fails the lookup boundary. The existing API maps timeout and failure to typed
runtime evidence before normal model execution. There is no lookup retry, cache
write-back, invalidation, or second execution-time lookup.

Azure Managed Redis configuration explicitly selects access key, Azure CLI, or
managed identity. Microsoft Entra modes carry a separately configured identity
object ID as the Redis AUTH username; a client ID is used only to select a
user-assigned managed identity. `DefaultAzureCredential` and implicit credential
fallback are not used. The client uses TLS with hostname verification on Azure
Managed Redis port `10000`, RESP2 raw responses, bounded connections and
timeouts, and zero Redis command retries. Background Microsoft Entra token
renewal bounds each token acquisition with a timeout, retries only transient
failures (stopping immediately on authentication or authorization errors), and
schedules and retries every step against a conservative safe deadline so the
whole next-attempt budget — actual delay plus the operation timeout plus a
pre-expiry margin that defaults to Microsoft's recommended three minutes — fits
before expiry; reauthentication retries are measured against the current
still-serving token, and a renewed token is published only after the pool accepts
it, so one transient failure does not permanently disable renewal. One resource
owner stops renewal before closing the Redis client and selected Azure
credential.

The production embedding provider is a Foundry/APIM Azure OpenAI v1 `/embeddings`
adapter that reuses the Slice 10A authentication modes, issues one non-retried
request per lookup, and strictly validates the response, including verifying that
the response `model` matches the configured profile and that reported
`total_tokens` equals `prompt_tokens`. The embedding input is a versioned,
canonical semantic-input payload built from the generation request. Because a
lookup against a paid embeddings deployment consumes tokens and cost even on a
hit, the lookup carries a typed `EmbeddingAttempt` (priced through the
authoritative cost catalog) that distinguishes measured usage from a
possibly-billed attempt with no usage; `RunResult` includes measured embedding
consumption and reports input/token/cost totals as unavailable when an attempt
was possibly billed but unmeasured, so a cache hit is never reported as free.
The production FastAPI lifespan owns Redis and embedding resources. Startup
inspects `FT.INFO`, validates an immutable companion profile contract, and
creates the index only when absent. Incompatible indexes fail startup without
replacement or data deletion. Role assignment, cache population, and live
deployment remain Slice 11C responsibilities.

### Run-history persistence boundary

Run history is a provider-independent asynchronous contract that saves one
terminal `RunResult`, retrieves one run by opaque `run_id`, and lists a bounded
newest-first sequence. The application persists only after the executor returns
a fully validated terminal result. Persistence does not participate in planning,
model execution, evaluation, escalation, semantic caching, or cost calculation.
An absent store preserves local execution behavior, while history reads return a
structured unavailable response.

Completed run evidence is immutable. The first write uses create semantics. A
duplicate ID is idempotent only when a point read validates to the same complete
`RunResult`; different evidence is a typed conflict and is never replaced.
Every point and list read validates the authoritative payload through the current
strict domain model. Unsupported schema versions, malformed payloads, and
metadata contradictions fail closed.

Cosmos schema version 1 contains only:

* `id`, equal to `RunResult.run_id`
* `schema_version`
* canonical UTC `created_at` metadata, validated against the payload
* `sort_key`, a descending recent-history ordering key checked against the payload
* `run_result_json`, the authoritative `RunResult.model_dump_json()` payload

The container partition-key path is `/id`. Opaque run IDs provide high
cardinality and permit point reads with the exact item ID and partition key.
Recent-history listing is therefore a bounded cross-partition query ordered by
the single descending `sort_key` property (canonical `created_at` plus
`run_id`), so it runs on the default container index without a composite index.
This is an intentional MVP tradeoff for direct lookup integrity over cheap
global history scans.

The authoritative result is stored as a JSON string because Cosmos JSON numbers
use binary64 and cannot preserve arbitrary exact Decimal cost evidence. Query
metadata is derived from that validated payload and checked against it on read.
The adapter runs a conservative UTF-8 JSON size pre-check and rejects oversized
evidence before writing, while Cosmos DB's server-side 2-MB limit (surfaced as a
mapped 413) stays the authoritative backstop rather than truncating evidence.

The Cosmos adapter translates SDK exceptions into sanitized not-found,
conflict, invalid-document, authentication, timeout, throttling, oversized-item,
and service-unavailable errors. Execution and persistence cannot be atomic
because external model execution precedes storage. `POST /api/v1/runs` therefore
returns the authoritative completed `RunResult` once constructed and reports
run-history persistence as a separate best-effort side effect through response
headers: `X-OPTIMA-Run-History` is `PERSISTED`, `FAILED`, or `NOT_CONFIGURED`,
and `X-OPTIMA-Run-History-Error` carries one sanitized `RunHistoryErrorCode` only
on failure. A failed save is not evidence that execution failed, so callers
requiring durable history inspect the header instead of resubmitting a completed
run. Application Insights in Slice 10D and lifecycle and infrastructure in Slice
11 do not replace this API contract.

Cosmos configuration explicitly selects account key, Azure CLI credential, or
managed identity. `DefaultAzureCredential` and implicit credential fallback are
not used. One closeable resource owner holds the application-lifetime async
Cosmos client and any selected async Azure Identity credential. The production
FastAPI lifespan closes that owner in reverse construction order. Cosmos
provisioning, indexing policy, and role assignments remain infrastructure and
Slice 11C responsibilities.

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

Telemetry observes existing domain evidence and never participates in planning,
execution, evaluation, caching, pricing, or persistence decisions. The
provider-independent boundary exposes run observation, bounded stage
observation, validated terminal-result projection, safe pre-result failure
classification, and explicit flush/close operations. No domain, planner,
executor, evaluator, cache, provider, or history contract depends on Azure
Monitor or OpenTelemetry types.

The default implementation is inert. Tests can inject a deterministic
context-local in-memory recorder. The production adapter translates the same
contract to locally owned OpenTelemetry providers and direct Azure Monitor
exporters only when `application_insights_enabled` is true and its complete
typed configuration is valid.

Telemetry schema version 1 uses this hierarchy beneath one explicitly
instrumented FastAPI server span:

```text
POST /api/v1/runs
        optima.run
                optima.quality_contract.build
                optima.semantic_cache.lookup        # only when attempted
                optima.planner.select
                optima.context_reduction            # only when attempted
                optima.model.generate               # once per actual provider call
                optima.evaluation.evaluate          # once per actual evaluator call
                optima.run_history.save             # only when configured and attempted
                optima.outcome.project              # one validated terminal projection
```

Context-local activation preserves parentage across asynchronous calls. Every
stage and terminal projection is close-once or emit-once. A failure-isolation
wrapper contains recorder, exporter, instrumentation, flush, and shutdown
exceptions so telemetry cannot alter the API response or any business call
count. Terminal projection is serialized per run. Its emitted guard is set
before metrics begin, so a partial metric batch cannot be retried into duplicate
points or presented as a complete projection. The failed projection span and a
single redacted warning remain the operational signal.

The `optima.run` span carries only bounded or validated trace attributes:

- schema version, run ID, and correlation ID
- plan family, Optimization Mode, Quality Profile, and task type
- authoritative terminal status and contract result
- escalation state and actual model-attempt count
- semantic-cache and context-reduction outcomes
- availability flags for token, cost, and evaluation measurements
- measured totals only when present
- exact aggregate cost as a numerically canonical fixed-point decimal string
        when complete pricing evidence is present

Run ID, correlation ID, and validated provider request ID are trace-only
attributes. They are never metric dimensions. Expected or planned values are
not emitted as actual measurements.

Schema version 1 defines these custom metrics:

- `optima.runs` by terminal status and plan family
- `optima.run.duration` by terminal status and plan family
- `optima.model.attempts` by model role and operation result
- `optima.model.duration` by model role and operation result
- `optima.tokens` by `INPUT`, `OUTPUT`, `CACHED`, or `EMBEDDING` category and
        model role where applicable
- `optima.cache.lookups` by semantic-cache outcome
- `optima.embedding.attempts` by measured, unmeasured outbound, or pre-outbound
        result
- `optima.escalations` by plan family
- `optima.quality_contract.results` by met, not met, or unavailable result
- `optima.evaluation.score` by valid pass, valid rejection, or invalid evidence
- `optima.run_history.persistence` by persisted or failed result
- `optima.telemetry.projections` by terminal or pre-result projection category

Completed contract misses are successful system operations. Failed and timed-out
runs use OpenTelemetry error status. Cache failure can coexist with a successful
root run, and history-save failure marks only the history child operation. Raw
exceptions are never recorded; spans contain only bounded failure categories or
validated exception class names at the HTTP boundary.

The FastAPI middleware extracts only W3C `traceparent` and `tracestate` headers,
uses registered route templates, and excludes the health route by default. It
does not inspect bodies or export raw paths, query strings, headers, cookies,
authorization values, API keys, or user IDs. Distro auto-instrumentation for
FastAPI, Azure SDK, requests, urllib, urllib3, Django, Flask, and psycopg2 is
not installed. OPTIMA's custom middleware is the only HTTP instrumentation.

The Azure adapter uses a parent-based trace-ID ratio sampler. The validated root
ratio defaults to `1.0` for complete demo traces and can be reduced to control
ingestion cost. Remote and local parent decisions propagate through each trace;
metrics are not sampled. Logs, offline retry storage, control-plane
configuration, Statsbeat, SDK statistics, and resource metrics default to
disabled. Live Metrics and performance counters are rejected because the SDK
implements them with process-global singleton state. Exporter transport retries
are zero (`retry_total=0`).
The installed exporter sets the Azure Core pipeline to
`RedirectPolicy(permit_redirects=False)`. Its separate manual 307/308 branch
reads `client._config.redirect_policy.max_redirects`; OPTIMA supplies
`redirect_max=0`, so the branch records failure without recursive transmission.

Initialization is serialized and directly constructs local tracer and meter
providers, one trace exporter, and one metric exporter. No process environment,
global provider, SDK class, resource detector, or host logger is replaced. The
local providers receive only the validated service name, service version, and
deployment environment. OPTIMA-owned exporter subclasses suppress control-plane
setup, Statsbeat, SDK statistics, resource metrics, and raw dependency logs
through internal hooks of the pinned pre-release exporter (`1.0.0b56`), so the
exact pin is deliberate and offline real-exporter tests guard those hooks.
The Application Insights connection string requires
an explicit credential-free HTTPS Azure ingestion endpoint; suffix-derived,
plaintext, credential-bearing, queried, fragmented, unknown, or non-Azure
endpoints fail before exporter creation.

One process-wide registry initializes one exact OPTIMA configuration. Equivalent
application compositions hold close-once leases; the final lease shuts down the
locally owned providers and permanently closes the registry. A conflicting
configuration or reconstruction after close fails before construction. Existing
external OpenTelemetry providers remain installed and are never claimed,
replaced, or shut down. Partially created owned exporters, processors, readers,
and providers are shut down best effort. Runtime initialization failures are
cached as an unavailable observer and are not retried; one redacted warning and
`force_flush() == false` make the failure detectable without changing a run
result. The production FastAPI lifespan closes the telemetry lease after all
other owned resources, allowing cleanup failures to remain observable.

### Production runtime composition

`create_production_app()` validates complete Foundry, Cosmos, Redis, managed
identity, and Application Insights settings before resource construction. It
creates one app-local dependency graph with no fake fallback. The graph contains
Foundry SMALL and STRONG providers, the Foundry embedding provider, semantic
cache, Cosmos run history, deterministic context reduction, exact-reference
evaluation, centralized pricing, and observability.

Construction order is telemetry, Foundry, embedding, Redis, Redis index
bootstrap, and Cosmos. Shutdown closes Cosmos, Redis, embedding, Foundry, and
telemetry. Partial startup uses the same reverse cleanup, suppresses cleanup
errors after recording their type, and re-raises the original startup error.
Uvicorn does not serve health or run routes until the lifespan yields.

## Module configuration

Optional optimizer capabilities such as semantic cache, context reduction, and historical policy are controlled through typed configuration as defined in `docs/MODULE_CONFIGURATION.md`.

Quality evaluation is not a normal optional optimizer flag for production/hackathon execution.

## Local development

Use in-memory/fake implementations for cache, run history, historical statistics, evaluator behavior, and model clients so core planner/executor behavior can be developed and tested without Azure resources or paid model calls.

Azure adapters should implement the same interfaces.
