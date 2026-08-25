---
title: Architecture Decision Log
description: Accepted architecture and engineering decisions for OPTIMA
---

# Architecture Decision Log

## ADR-001: Use HVE as an engineering workflow, not a runtime dependency
Status: Accepted

Use HVE Core Research -> Plan -> Implement patterns with GitHub Copilot during development.
OPTIMA runtime must not depend on HVE.

## ADR-002: Repo-owned specifications are the source of truth
Status: Accepted

Keep product scope, architecture, and engineering instructions in version control so Copilot sessions inherit stable context.

## ADR-003: Python + FastAPI backend
Status: Accepted

Reason: rapid hackathon development, strong AI/Azure SDK ecosystem, testability.

## ADR-004: Streamlit demo UI
Status: Accepted for MVP

Reason: optimize development speed and judge-facing visualization.
A production web frontend is roadmap work.

## ADR-005: Deterministic/explainable planner first
Status: Accepted

Do not build RL/ML planning during the hackathon.
Use rules plus historical strategy statistics.

## ADR-006: Foundry Model Router is a comparator/candidate strategy
Status: Accepted

OPTIMA must differentiate at execution-plan level and quality verification, not claim model routing as novel.

## ADR-007: Azure-native target with local fakes
Status: Accepted

Core engine must run in tests without paid cloud calls.
Azure implementations plug into interfaces.

## ADR-008: Bicep + azd for infrastructure/deployment
Status: Accepted

Keep Azure deployment reproducible and Copilot-friendly.

## ADR-009: Feature-branch-only development
Status: Accepted

`main` is the integration branch.
GitHub Copilot/HVE implementation work must occur on a task branch and merge through a pull request.
If a feature branch cannot be created, implementation must stop rather than fall back to editing `main`.

## ADR-010: Composable execution plans
Status: Accepted

Planner V1 selects cache, context, model, verification, and escalation policies as composable plan components rather than permanently encoding every combination as a monolithic strategy.

Friendly combined strategy names may still be shown in the UI.

## ADR-011: Configurable optimizer modules
Status: Accepted

Optional optimization modules are controlled through typed application configuration.

The MVP does not include a settings/admin UI for these flags.
This permits semantic cache, context reduction, and historical policy to be bypassed safely without architecture changes when benchmark evidence shows a quality or latency regression.

## ADR-012: HIGH-complexity requests use strong-direct in Planner V1
Status: Accepted

Planner V1 does not attempt the small model first for requests classified as `HIGH` complexity.

This applies across Standard, High, and Critical Quality Profiles and across Cost, Balanced, and Quality Optimization Modes.

Reason:
- avoid predictable small-model failures
- avoid unnecessary evaluator/model calls
- reduce expected latency and wasted spend
- keep V1 behavior simple and defensible

Future versions may permit task-specific exceptions only after benchmark evidence demonstrates that a lower-cost path is reliably effective.

## ADR-013: Every V1 small-first plan has strong fallback
Status: Accepted

Planner V1 has no normal `small_direct_without_fallback` execution policy.

When `SMALL` is selected first:
1. execute small
2. evaluate
3. return if the Quality Contract is met
4. otherwise execute `STRONG` exactly once
5. evaluate and return the final result

Reason:

OPTIMA should not knowingly stop at a failed small-model result when its selected execution plan has an available stronger fallback.

## ADR-014: Planner V1 uses the highest supplied risk tier

Status: Accepted

Planner V1 calculates effective risk as the more severe of the profiled request
risk and Quality Contract risk, using `LOW < MEDIUM < HIGH`. Safeguards use the
effective value, while typed decision evidence preserves all three values.

## ADR-015: Planner decisions carry typed evidence

Status: Accepted

Pre-execution plans contain immutable typed evidence for risk, module state,
cache assessment, historical statistics, and base/final model policy. Core
planner evidence must not use an arbitrary dictionary or include runtime
measurements.

## ADR-016: Historical adjustment is deterministic and bounded

Status: Accepted

With sufficient comparable evidence, poor small-first performance below the
configured avoid threshold moves an eligible COST or BALANCED small-first plan
to strong-direct. Positive evidence only strengthens an existing small-first
decision. History applies at most one adjustment and never downgrades a
strong-direct decision.

## ADR-017: Structurally invalid plans return typed failure

Status: Accepted

When configured conceptual capabilities cannot satisfy mandatory plan
constraints, Planner V1 returns a typed planning failure instead of selecting a
knowingly invalid plan. Provider calls and runtime quality failure handling
remain outside the planner.

## ADR-018: Semantic-cache reuse binds one pre-planning lookup

Status: Accepted

The application performs at most one provider-independent semantic-cache lookup
before Planner V1. A resolved value contains the exact cached output and its
source-run, complete request binding, similarity, prior-evaluation,
contract-compatibility, and reuse-safety evidence. The cache abstraction
retrieves evidence but makes no reuse decision.

Planner V1 applies all cache gates. An accepted plan carries a detached snapshot
of the exact resolved value, and the executor consumes that snapshot without a
second lookup. This prevents time-of-check/time-of-use substitution.

The versioned request binding uses deterministic canonical JSON over input text,
original context, reference output, ordered criteria, caller metadata, task
type, and complexity. Planner V1 rejects a binding mismatch before candidate
similarity or quality gates. The binding exposes task type and complexity but no
raw request content. The execution request recomputes the digest, and the run
result verifies profile identity plus equality across planner and runtime
snapshots. Every assessed candidate also produces a detached assessment without
the cached output, including its binding, source identity, similarity, prior
evaluation, compatibility, and safety facts. Semantic-cache outcome requirements
and model trace ordering are centralized so contradictory evidence, event sets,
execution steps, or terminal cache results fail at domain boundaries.

Source evaluation evidence remains unchanged and is exposed separately from
current-run evaluations. Cache failures and timeouts fall back to normal model
execution with typed runtime evidence. Redis persistence, cache writes,
invalidation, embeddings, and cloud adapters remain Slice 10 or later.

## ADR-019: Cosmos run history uses immutable versioned payloads

Status: Accepted

Completed `RunResult` values are immutable execution evidence. The Cosmos
adapter uses create-only writes and never unconditional upsert. A duplicate run
ID succeeds only when the existing versioned document validates to the same
complete result; otherwise the adapter raises a conflict.

Schema version 1 uses `id == RunResult.run_id`, `/id` as the partition-key path,
canonical UTC `created_at` metadata, a descending `sort_key` ordering property,
and the authoritative `RunResult.model_dump_json()` representation stored as a
string. The string
preserves exact Decimal costs that Cosmos binary64 JSON numbers cannot represent
reliably. Every read validates the strict current model and rejects identity,
timestamp, or sort-key metadata that contradicts the payload.

The `/id` partition key provides high cardinality and efficient point reads by
opaque run ID. Its accepted tradeoff is that bounded recent-history listing is a
cross-partition query. Deterministic newest-first ordering uses one descending
`sort_key` property (canonical `created_at` plus `run_id`), so it runs on the
default container index without a composite index.

Execution and persistence cannot be atomic because external model execution
precedes storage. `POST /api/v1/runs` returns the authoritative completed result
once constructed and reports persistence as a best-effort side effect through the
`X-OPTIMA-Run-History` and `X-OPTIMA-Run-History-Error` response headers rather
than converting a completed paid execution into a retryable HTTP failure.

Cosmos authentication is explicit: account key, Azure CLI credential, or
managed identity. There is no implicit credential chain. One closeable resource
owner retains the async client and any owned credential for the application
lifetime. Production lifespan wiring and Cosmos infrastructure remain Slice 11.

## ADR-020: Redis retrieves candidate evidence and Planner V1 decides reuse

Status: Accepted

The Azure Managed Redis semantic-cache adapter performs one read-only vector
lookup before Planner V1. It returns at most one complete `CacheCandidate` and
does not apply similarity thresholds, Quality Contract thresholds, exact request
binding, contract compatibility, or reuse safety. Planner V1 remains the single
authority for all reuse gates, and the executor consumes the planner-bound
snapshot without another Redis lookup.

Schema version 1 uses a HASH index with task-type and complexity TAG fields and
one `FLAT`, `FLOAT32`, `COSINE` vector field. Stored JSON payloads contain the
complete request binding and prior evaluation. The adapter validates exact
dimensions, finite nonzero vectors, canonical booleans, supported schema
version, bounded cosine distance, and complete immutable domain evidence.
Malformed evidence fails closed into the existing typed lookup-failure path.

Azure Managed Redis uses TLS with hostname verification on port `10000`, RESP2
raw responses, bounded timeouts and connection count, and zero command retries.
Authentication is explicitly one of access key, Azure CLI, or managed identity.
Microsoft Entra modes use the configured identity object ID as the Redis AUTH
username and request tokens only for `https://redis.azure.com/.default`.
`DefaultAzureCredential` and credential fallback are prohibited.

One application-lifetime resource owner controls the Redis client, background
token renewal, and selected Azure Identity credential. Renewal is cancelled
before transport and credential shutdown. Cache write-back, invalidation,
population, infrastructure provisioning, role assignment, and production
lifespan wiring remain outside Slice 10C.

## ADR-021: Enforce embedding-profile identity, embedding cost, and renewal resilience

Status: Accepted

Different embedding models can produce vectors of identical dimension in
incompatible vector spaces, so dimension equality alone is not truthful evidence
of comparability. Slice 10C therefore binds every stored and queried vector to a
strict, versioned embedding profile (`embedding-profile-v1`) whose identity is a
SHA-256 hash of the embedding model, deployment, and dimension. The identity is
an injection-safe RediSearch tag: model and deployment tokens are validated
against a restricted character set. The Redis adapter requires the injected
provider to declare the configured profile, filters `FT.SEARCH` by schema
version and embedding profile before KNN, and rejects any stored record whose
profile differs. Composition fails fast when the provider and index profiles
disagree. The source `RequestBinding` is still returned unchanged and Planner V1
remains the sole reuse authority.

The smallest production embedding provider is a Foundry/APIM Azure OpenAI v1
`/embeddings` adapter that reuses the Slice 10A authentication modes (API key,
Azure CLI, or managed identity, with no `DefaultAzureCredential` and no
fallback), issues exactly one non-retried HTTPS request per lookup, and strictly
validates the response: exactly one embedding at the expected index, exact
dimension, finite non-boolean values, and non-fabricated usage. Errors expose no
endpoint, token, prompt, or response body. A deterministic fake provider serves
offline tests. Production FastAPI lifespan ownership remains Slice 11.

Because a lookup against a paid embeddings deployment consumes tokens and cost
even on a hit, the lookup returns a dedicated `EmbeddingUsage` (never forced into
a model role). It is priced through the same authoritative catalog as model
calls; central pricing stays authoritative and provider-reported monetary cost is
not accepted. `RunResult` token and cost totals include embedding consumption, so
a cache hit is never reported as free, and totals return unavailable rather than
fabricate a value when embedding tokens or pricing are missing. Embedding usage
is carried through hit, miss, binding-mismatch fallback, and Redis-failure paths;
a genuine embedding failure records no usage.

Background Microsoft Entra token renewal uses bounded attempts with bounded
backoff and jitter (both configurable within strict limits). A transient token
acquisition or reauthentication failure no longer permanently disables renewal;
renewal stops only when the bound is exhausted, the owner is closing, the failure
is non-transient, or continued use would pass the safe expiry boundary. Renewal
never retries a Redis search or an embedding request and never knowingly uses an
expired token. Endpoint validation additionally rejects control characters,
whitespace, and other non-hostname input before any network activity. The custom
`azure-identity` streaming credential provider is retained over `redis-entraid`
to keep explicit `AzureCliCredential` and `ManagedIdentityCredential` support and
object-ID `AUTH` username handling under OPTIMA's own typed control.

The truthfulness, provider-identity, semantic-input, and renewal guarantees
described above are made precise and enforced by ADR-022; where the two ADRs
overlap, ADR-022 is authoritative.

## ADR-022: Truthful embedding attempts, verified identity, and safe renewal

Status: Accepted

A paid embedding request can leave the caller unable to observe what it
consumed: the request may reach the provider and be billed, yet fail before
returning measured usage. Recording "no usage" in that case understates
consumption. Slice 10C therefore replaces the raw `EmbeddingUsage` carried on
cache evidence with a typed `EmbeddingAttempt` that records whether the provider
was invoked, whether an outbound request may have been attempted, and the
measured usage when available. The embedding provider signals
`outbound_attempted=False` only when a failure provably occurred before any
outbound request (for example, authentication acquisition); every failure at or
after the outbound call is reported as possibly billed. When an attempt was
possibly billed but returned no measured usage, `RunResult` reports
`total_input_tokens`, `total_tokens`, and cost as unavailable rather than
model-only. Output-token totals stay exact because embeddings never produce
output tokens, and a proven pre-outbound failure keeps model-only totals intact.

Two embedding models can return same-dimension vectors from incompatible spaces,
so a vector is bound to a profile only when the provider identity is verified.
The provider requires the embeddings response `model` to be present and equal to
the configured profile model; a missing, blank, non-string, or mismatched value
is rejected as an invalid response, and no profile is ever attached to an
unverified vector. Because embeddings consume input only, reported
`total_tokens` must equal `prompt_tokens` whenever both are present; a
disagreement is rejected.

Semantic similarity is defined over a versioned input policy
(`semantic-input-v1`) whose version is part of the embedding-profile identity.
A single pure builder produces the embedding input as canonical JSON over the
generation request (input text and optional context) rather than delimiter
concatenation, avoiding injection ambiguity; reference output and evaluation
criteria are excluded because they are evaluation identity captured by the
authoritative `RequestBinding` that Planner V1 checks. Any external
cache-population tooling must use the same builder to remain comparable.

Background Microsoft Entra token renewal bounds each token acquisition with an
explicit timeout, classifies failures so only transient transport, throttling,
and transient server statuses are retried while authentication and authorization
failures stop immediately, and publishes a renewed token only after the pool has
accepted it. A renewal step is scheduled and retried against a conservative safe
deadline: the whole next-attempt budget — the actual jittered delay plus the
operation's bounded timeout plus a pre-expiry safety margin — must fit before the
relevant token expires, so a retry can never begin yet block past the deadline in
acquisition or reauthentication. The pre-expiry margin defaults to 180 seconds,
matching Microsoft's guidance to send a renewed `AUTH` token at least three
minutes before expiry, and reauthentication retries are measured against the
current (still-serving) token rather than the renewed token, because live
connections depend on the current token until reauthentication succeeds. The
first refresh is scheduled at the sooner of a proactive refresh ratio or the last
moment that still reserves one full acquisition, one full reauthentication, and
the margin; a short-lived token whose ratio point would already breach that
reserve is refreshed immediately. Cancellation is preserved during sleeps,
acquisition, and reauthentication; renewal never retries a Redis search or an
embedding request, never knowingly uses an expired token, and never exposes token
or SDK error text. All renewal bounds (attempts, backoff, cap, acquisition
timeout, reauthentication timeout, and expiry safety margin) are configurable
within strict limits. This section supersedes any earlier claim that these
behaviors held before Slice 10C's round-three and round-four corrections.

## ADR-023: Project authoritative evidence through a provider-independent observability boundary

Status: Accepted

Slice 10D adds a small observability contract that starts one run observation,
observes only attempted stages, projects one validated terminal `RunResult`,
records bounded pre-result failures, and exposes explicit flush and close
operations. The default implementation is inert, deterministic tests use an
in-memory recorder, and the Azure adapter is isolated from domain, planner,
executor, evaluator, cache, provider, and history contracts.

The Azure implementation uses `azure-monitor-opentelemetry-exporter` with
locally owned OpenTelemetry providers and manual `optima.*` spans. The distro
and its automatic-instrumentation packages are not installed. The exporter is
pinned to the exact pre-release build `1.0.0b56`; disabling Statsbeat, customer
SDK statistics, the control-plane worker, and resource metrics relies on that
build's internal exporter hooks, so the exact pin is deliberate and offline
real-exporter tests guard the behavior against an unreviewed upgrade.
A custom FastAPI middleware emits one server span, extracts only W3C trace
context, uses registered route templates, and never captures bodies, headers,
query strings, raw URLs, or raw exceptions. This avoids duplicate spans and
prevents endpoint, credential, or caller data from entering telemetry.

Terminal metrics are emitted once from existing `RunResult` evidence. Metric
dimensions use only bounded statuses, plan families, model roles, token
categories, cache outcomes, contract results, and persistence outcomes. Missing
measurements produce no numeric point. Run, correlation, and provider request
IDs remain trace-only. Aggregate cost is intentionally absent from metrics
because converting the exact domain `Decimal` to a floating-point metric would
weaken the evidence contract; exact cost stays in `RunResult` and a decimal
string trace attribute. That attribute is a numerical canonicalization: fixed
point, no scientific notation, zero represented as `0`, and insignificant
fractional trailing zeros removed. It preserves exact numeric equality rather
than the Decimal's original exponent representation and is absent when pricing
evidence is incomplete or when the exact value would exceed the bounded
fixed-point width that guards against unbounded rate exponents.

Application Insights is disabled by default. Enabled composition requires a
validated `SecretStr` connection string and initializes one process-wide runtime
for one exact configuration. The connection string requires an explicit
credential-free HTTPS Azure ingestion endpoint and rejects suffix-derived or
unknown endpoint configuration. Parent-based trace-ID ratio sampling defaults
to `1.0` for root traces and preserves remote and local parent decisions;
metrics remain unsampled. Offline storage, telemetry logs, control-plane
configuration, Statsbeat, SDK statistics, resource metrics, and Azure Core
transport retries default to disabled. Live Metrics and performance counters
are rejected because their SDK implementations are process-global. The exporter
hardcodes automatic pipeline redirects off. Its separate manual 307/308 branch
consumes `redirect_max`; OPTIMA sets it to zero so no recursive redirect is
attempted. Local provider resources contain only validated OPTIMA attributes.

A pre-existing process-wide OpenTelemetry provider can coexist with OPTIMA.
Initialization is serialized and directly builds local providers and exporters;
it never mutates environment values, global providers, resource detectors, SDK
classes, or host provider ownership. Temporary same-thread filters suppress raw
SDK records during OPTIMA-owned operations and preserve concurrent host
diagnostics. OPTIMA shuts down only its explicit local components.
Equivalent compositions receive close-once
leases, final close permanently closes the registry, and reconstruction is
rejected. Runtime initialization failures are cached as unavailable and are not
retried; `force_flush()` returns false and one redacted warning exposes the
failure. Typed configuration errors and a conflicting second configuration
still fail before initialization. Terminal projection is serialized and never
retries a partially emitted metric batch. Adapter failures never alter business
behavior. Slice 11 owns production lifespan calls to the provided flush and
close operations.