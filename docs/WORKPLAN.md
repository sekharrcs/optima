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

## Slice 10 - Azure adapters
Implement:
- Foundry/APIM model provider
- Cosmos run-history adapter
- Redis semantic cache adapter
- App Insights tracing

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
