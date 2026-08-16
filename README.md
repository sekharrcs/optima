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

Use HVE Core's Research -> Plan -> Implement workflow with GitHub Copilot.

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

## Guiding rule

Do not add features because they sound intelligent. Every feature must improve or protect at least one of:
- measured cost
- token usage
- measured quality
- latency
- explainability
- experimental credibility
