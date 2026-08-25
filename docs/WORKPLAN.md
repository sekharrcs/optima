---
title: Build Workplan
description: Incremental implementation slices and acceptance criteria for OPTIMA
---

# Build Workplan

## Slice 0 - Repository engineering setup
Acceptance:
- Copilot instructions load
- HVE Core available
- Python project initializes
- lint/type/test commands run
- no product code beyond health endpoint
- branch workflow is documented and implementation does not occur directly on `main`
- typed application settings skeleton supports MVP module flags

## Slice 1 - Domain contracts
Implement:
- QualityContract
- RequestProfile
- ExecutionPlan
- ExecutionStep
- EvaluationResult
- ModelUsage
- RunResult

Acceptance:
- Pydantic validation tests
- profile -> threshold translation tests
- ExecutionPlan can represent `SMALL_FIRST_WITH_FALLBACK` and `STRONG_DIRECT`

## Slice 2 - Planner with fake dependencies
Implement:
- deterministic Planner V1
- composable plan policies
- module configuration gates
- reason codes
- plan explanation

Acceptance:
- planner behavior conforms to `docs/PLANNER_V1.md`
- planner unit test matrix covers all V1 paths
- every HIGH-complexity case selects strong-direct
- every small-first plan contains strong fallback
- module-disabled cases are covered by unit tests

## Slice 3 - Model provider abstraction + fake providers
Implement:
- small/strong provider interface
- fake provider
- usage/latency capture

Acceptance:
- no live cloud required

## Slice 4 - Quality evaluator
Implement:
- evaluator interface
- deterministic evaluator
- fake evaluator
- threshold engine

Acceptance:
- pass/fail and reasons tested

## Slice 5 - small-first-with-fallback vertical slice
Implement end-to-end through API.

Acceptance:
- small pass avoids strong call
- small fail escalates exactly once
- no normal small-first path returns a failed Quality Contract without attempting configured strong fallback
- total tokens/cost includes all executed calls
- explanation contains escalation reason

## Slice 6 - Cost calculator and baseline comparison
Acceptance:
- centralized price catalog
- actual usage -> cost calculation
- baseline vs OPTIMA metrics

## Slice 7 - Streamlit decision demo
Acceptance:
- UI conforms to `docs/UI_SPEC.md`
- Execute, Dashboard, and Run History views exist
- submit request and optional context
- select Quality Contract
- see result, chosen plan, execution steps, quality, token/cost comparison
- UI renders backend trace/reason codes rather than inventing execution facts

## Slice 8 - Context reduction
Acceptance:
- module can be enabled/disabled through typed configuration
- disabled state bypasses context reduction without changing planner code
- before/after token metrics
- tests verify required facts preserved on benchmark fixtures

## Corrective Slice 8A - Strong-direct execution
Acceptance:
- existing Planner V1 `STRONG_DIRECT` decisions execute without new routing
- optional context reduction retains measured fallback-to-original behavior
- STRONG executes exactly once and quality evaluation executes exactly once
- no SMALL call or escalation evidence appears in strong-direct traces
- valid final evaluation yields measured `true` or `false` contract status
- unavailable final evaluation fails closed without fabricated output or status
- actual one-call tokens, cost, latency, and pricing provenance are exposed
- FastAPI and Streamlit render backend execution evidence

## Slice 9 - Semantic cache
Acceptance:
- module can be enabled/disabled through typed configuration
- one provider-independent lookup occurs before planning without a second lookup
- hit, miss, rejected-match, failure, and timeout flows carry typed runtime evidence
- similarity, source run, lookup latency, and cached quality evidence are surfaced
- accepted plans bind the exact resolved output and source evidence
- source evaluation identity, threshold, pass result, checks, and metadata remain unchanged
- cache results must have valid prior passing evidence and satisfy the current contract
- cache hits perform no model, context-reduction, or current evaluator calls
- cache failures and timeouts fall back to unchanged model execution
- the local exact-match demo proves integration only; Redis remains Slice 10

## Corrective Slice 10A - Foundry and APIM model provider

Implement:

- Azure OpenAI v1 chat-completion adapter behind the existing provider contract
- configured `SMALL` and `STRONG` deployment mapping
- explicit API-key, Azure CLI, and managed-identity authentication
- provider output, request identity, latency, and optional usage mapping

Acceptance:

- each logical provider call performs one outbound request with no implicit retry
- missing provider usage remains unavailable rather than becoming zero
- provider-reported total and cached token facts remain unchanged
- planner, evaluator, escalation, and cost ownership remain unchanged
- default API, local demo, and unit tests require no Azure credentials or paid calls

## Slice 10B - Cosmos run-history adapter

Implement:

- provider-independent run-history contract and deterministic in-memory store
- Azure Cosmos DB for NoSQL adapter using `azure.cosmos.aio`
- immutable versioned documents and bounded read APIs

Acceptance:

- save, point-read, and bounded newest-first list operate on validated
	`RunResult` evidence
- the container uses `/id`, and point reads supply the exact run ID as both item
	and partition key
- writes use create-only semantics; identical duplicates are idempotent and
	contradictory duplicates never overwrite evidence
- authoritative JSON-string payloads preserve exact Decimal costs and unavailable
	optional measurements
- every read rejects unsupported, malformed, or contradictory documents
- recent listing is a parameterized bounded cross-partition query with
	deterministic run-ID tie ordering
- items over Cosmos DB's 2-MB UTF-8 JSON limit fail explicitly without truncation
- account-key, Azure CLI, and managed-identity modes are explicit and mutually
	exclusive, with no default credential chain
- one application-lifetime async client and any owned credential are explicitly
	closeable
- completed, failed, and timed-out terminal results are returned once and persist
	exactly once as a best-effort side effect; a persistence failure is reported by
	response header and never converts a completed execution into an HTTP failure
- history reads return structured `404`, `503`, or fail-closed invalid-document
	responses as appropriate
- default tests use deterministic fakes without Azure credentials, network
	access, an emulator, or paid resources
- planner routing, execution order, provider/evaluator calls, semantic cache,
	cost calculation, and existing result evidence remain unchanged

## Slice 10C - Redis semantic-cache adapter

Implemented:

- read-only Azure Managed Redis implementation of the existing semantic-cache
	contract
- one bounded pre-planning `KNN 1` lookup with strict schema and domain
	validation
- explicit access-key, Azure CLI, or managed-identity authentication without a
	default credential chain
- application-lifetime Redis, token-renewal, and Azure Identity ownership
- offline tests for query shape, malformed evidence, Planner V1 authority,
	authentication selection, client security settings, and cleanup

Deferred to Slice 11 or a separately approved cache-population slice:

- Redis provisioning, RediSearch enablement, index creation, and role assignment
- cache writes, write-back, invalidation, and population policy
- production FastAPI lifespan composition

## Slice 10D - Application Insights tracing

Implemented:

- provider-independent run and stage observation contracts
- inert and deterministic in-memory implementations
- direct Azure Monitor OpenTelemetry exporter adapter with local providers
- explicit privacy-safe FastAPI server spans
- planner, semantic-cache, context-reduction, model, evaluation, persistence,
  and terminal-outcome spans
- bounded operational metrics projected from validated domain evidence
- parent-based trace-ID ratio sampling with unsampled metrics
- process-wide idempotent Azure Monitor initialization and explicit lifecycle
  operations

Acceptance:

- disabled observability performs no Azure initialization, network access,
  credential acquisition, background work, or telemetry persistence
- every logical operation is represented once and only when attempted
- async context keeps each `optima.*` operation beneath one `optima.run` span
  and the explicit FastAPI server span
- terminal spans and metrics are projected once from validated `RunResult`
  evidence without inferred zero values
- completed contract misses remain successful system operations, while timeout
  and failed operations use error status
- cache and history failures remain child-operation failures and cannot rewrite
  a successful terminal run
- no prompt, context, criterion, reference, output, metadata payload, vector,
  secret, endpoint, body, header, raw URL, query string, raw exception message,
  or stack content is exported
- metric dimensions contain only bounded values and never run, correlation, or
  provider request IDs
- exact `Decimal` cost is omitted from metrics and retained as domain evidence
  plus an optional decimal-string trace attribute
- repeated app composition does not duplicate providers, exporters, or HTTP
  instrumentation
- ambient OpenTelemetry resource attributes, exporter retries, and exporter
	control-plane/statistics background components are disabled without global
	environment or provider mutation
- runtime initialization failures degrade once to inert observation, while
  invalid or conflicting typed configuration fails before initialization
- all automated tests use fakes or local in-memory OpenTelemetry providers

Deferred to Slice 11:

- production FastAPI lifespan ownership of telemetry flush and shutdown
- Azure resource provisioning, connection-string injection, and RBAC
- opt-in live-Azure smoke validation

## Slice 11 - Azure infrastructure and deployment
Implement:
- Bicep
- `azure.yaml`
- GitHub Actions
- managed identity/RBAC

## Slice 12 - Benchmark + hackathon dashboard
Acceptance:
- baseline and OPTIMA run on same dataset
- measured aggregate savings
- quality pass-rate comparison
- strategy distribution
- individual decision inspection
