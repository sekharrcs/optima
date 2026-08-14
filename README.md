# OPTIMA

OPTIMA is a quality-constrained AI execution optimizer.

Instead of asking only "Which model should answer this request?", OPTIMA asks:

> What is the lowest-cost execution strategy that can satisfy the required quality contract?

This repository is intentionally bootstrapped with specifications and GitHub Copilot instructions before application code is written. The implementation should be created incrementally with GitHub Copilot using the repository as the source of truth.

## MVP strategies

1. Semantic cache
2. Direct small model
3. Context reduction -> small model
4. Small model -> quality evaluation -> escalate to strong model if needed
5. Direct strong model (baseline/control)
6. Microsoft Foundry Model Router (comparison strategy, not the OPTIMA differentiator)

## Development method

Use HVE Core's Research -> Plan -> Implement workflow with GitHub Copilot.

Before implementing any feature:
1. Read `docs/PRODUCT_SPEC.md`
2. Read `docs/MVP_SCOPE.md`
3. Read `docs/ARCHITECTURE.md`
4. Read the relevant domain spec
5. Research the current code
6. Produce a concrete implementation plan
7. Implement the smallest vertical slice
8. Run tests
9. Review against acceptance criteria

## Guiding rule

Do not add features because they sound intelligent. Every feature must improve at least one of:
- measured cost,
- token usage,
- measured quality,
- latency,
- explainability,
- experimental credibility.
