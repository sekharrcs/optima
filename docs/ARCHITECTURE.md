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
failures (stopping immediately on authentication or authorization errors),
never sleeps a retry past a safe margin before the current token expires,
retries pool reauthentication within the same bounds, and publishes a renewed
token only after the pool accepts it, so one transient failure does not
permanently disable renewal; one resource owner stops renewal before closing the
Redis client and selected Azure credential.

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
Production index provisioning, role assignment, cache population, and FastAPI
lifespan ownership of the Redis and embedding resources remain Slice 11
responsibilities.

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
Cosmos client and any selected async Azure Identity credential. Slice 11 retains
responsibility for production FastAPI lifespan wiring, Cosmos provisioning,
indexing policy, and role assignments.

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
