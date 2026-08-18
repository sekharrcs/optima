---
title: OPTIMA Module Configuration
description: Typed configuration requirements for independently controlled OPTIMA modules
---

# OPTIMA Module Configuration

## Decision

OPTIMA optimization modules must be independently configurable, but the hackathon MVP will not include a full configuration UI or dynamic policy-management service.

The goal is architectural flexibility with minimal MVP complexity.

## Why

Optimization techniques can introduce tradeoffs. For example, context reduction may lower token cost but could remove information required for a high-quality answer.

A technique that is harmful for one benchmark or workload must be bypassable without rewriting planner logic.

## MVP module flags

Support configuration values equivalent to:

```text
semantic_cache_enabled: true
context_reduction_enabled: true
historical_policy_enabled: true
foundry_router_comparator_enabled: false
```

Quality evaluation is **not** a normal optional optimizer module.

A normal OPTIMA run that claims Quality Contract compliance must always use a valid evaluator.

For tests/local development:
- fake evaluators may be injected
- evaluation behavior may be stubbed
- test-only bypasses must never become the normal production/hackathon path

## Configuration source

For MVP:
- defaults live in a typed application settings/configuration component
- environment variables may override defaults
- tests can inject explicit settings

Do not scatter environment-variable reads throughout planner/strategy code.
Do not hard-code flags inside planner conditionals.

## Planner behavior

The planner receives module capabilities/configuration as input.

### Context reduction disabled

If `context_reduction_enabled == false`:
- planner must never include a context-reduction step
- request proceeds with original context
- reason code includes `CONTEXT_REDUCTION_DISABLED`

### Semantic cache disabled

If `semantic_cache_enabled == false`:
- planner skips cache lookup entirely
- reason code may include `SEMANTIC_CACHE_DISABLED` in debug details

### Historical policy disabled

If `historical_policy_enabled == false`:
- planner uses only deterministic V1 policy
- historical statistics cannot alter the base plan

### Foundry comparator disabled

If `foundry_router_comparator_enabled == false`:
- comparator runs are not offered/executed
- this must not affect normal OPTIMA planning

## Quality safeguard

Context reduction is an optimization, not a requirement.

If benchmark evidence shows that context reduction materially harms Quality Contract pass rate for a task class, it must be possible to:
1. disable context reduction globally, or
2. later disable it selectively by task/risk class

Selective per-task configuration is roadmap scope unless needed during the hackathon.

## Future evolution

Later versions may add:
- per-task module policies
- per-risk-tier policies
- per-tenant configuration
- dynamic feature flags
- experimentation/A-B policies
- admin UI
- automatic module disabling based on observed quality regression

These are not required for the MVP.
