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