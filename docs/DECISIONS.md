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
canonical UTC `created_at` metadata, and the authoritative
`RunResult.model_dump_json()` representation stored as a string. The string
preserves exact Decimal costs that Cosmos binary64 JSON numbers cannot represent
reliably. Every read validates the strict current model and rejects identity or
timestamp metadata that contradicts the payload.

The `/id` partition key provides high cardinality and efficient point reads by
opaque run ID. Its accepted tradeoff is that bounded recent-history listing is a
cross-partition query. Deterministic ordering uses `created_at DESC, id ASC` and
requires a matching composite index.

Cosmos authentication is explicit: account key, Azure CLI credential, or
managed identity. There is no implicit credential chain. One closeable resource
owner retains the async client and any owned credential for the application
lifetime. Production lifespan wiring and Cosmos infrastructure remain Slice 11.