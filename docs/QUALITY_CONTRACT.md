---
title: Quality Contract
description: Quality profiles, optimization modes, and pass conditions for OPTIMA requests
---

# Quality Contract

## Purpose

A Quality Contract expresses the minimum acceptable quality for one request and the preference OPTIMA should use when choosing among execution plans that can satisfy that minimum.

The two primary concepts are deliberately separate:

- **Quality Profile** answers: "What minimum quality must the final result satisfy?"
- **Optimization Mode** answers: "How aggressively should OPTIMA pursue savings while trying to satisfy that quality requirement?"

Changing Optimization Mode must never lower the minimum quality threshold.

## Domain model

Suggested fields:

```text
quality_profile: STANDARD | HIGH | CRITICAL
minimum_quality_score: float
optimization_mode: COST | BALANCED | QUALITY
max_latency_ms: optional integer
risk_tier: LOW | MEDIUM | HIGH
```

The UI should normally expose the Quality Profile and Optimization Mode, not raw thresholds.

## Initial hackathon quality defaults

These values are configuration defaults, not universal truths:

| Quality Profile | Initial threshold |
|---|---:|
| Standard | 0.80 |
| High | 0.90 |
| Critical | 0.95 |

They must be easy to change after benchmark calibration.

## Optimization Mode semantics

### COST

Use the most cost-aggressive eligible plan while preserving the Quality Contract.

Typical behavior:
- prefer small-first execution where safety/risk rules allow it
- prefer context reduction when enabled and safe
- allow verification and escalation to recover quality
- accept the possibility of an additional model call when small-first fails

### BALANCED

Use the default conservative Planner V1 policy.

Typical behavior:
- small-first for low/medium complexity when appropriate
- strong-direct for difficult or high-risk work
- use context reduction only when policy says the token benefit is worthwhile

### QUALITY

Reduce quality risk even if expected cost is higher.

Typical behavior:
- move to strong-direct earlier
- avoid aggressive context reduction for uncertain/high-risk requests
- use small-first only when the request is clearly low complexity or historical evidence is strong

`QUALITY` does not mean "ignore cost." It means quality-risk is weighted more heavily when several plans can satisfy the contract.

## Pass condition

A final answer passes when:
- the selected evaluator is valid for the task
- its measured score is at or above the threshold
- any mandatory deterministic checks pass
- required grounding is supported by supplied context

Contract status has three states:
- `true` when valid final evidence passes
- `false` when valid final evidence does not pass
- unavailable when valid final evidence does not exist

Unavailable evidence must fail closed and must not be reported as a measured
contract failure.

Quality scores use a finite inclusive scale from `0.0` to `1.0`. Invalid,
negative, out-of-range, `NaN`, or infinite judge values are rejected rather than
clamped. An evaluator timeout, provider failure, malformed response, unsupported
schema, or missing grounding context produces no score and therefore leaves
contract status unavailable.

## Quality evaluation is mandatory for normal OPTIMA runs

A normal run that claims Quality Contract compliance must produce a valid evaluation result.

Tests and local development may inject fake evaluators, but production/hackathon execution must not silently bypass quality evaluation.

## Cached source evaluation

A semantic-cache hit reuses previously accepted evidence rather than executing a
new evaluator. The source evaluation retains its original evaluator identity,
score, threshold, mandatory-check result, pass result, reasons, and metadata.
OPTIMA must not copy the score into a synthetic current-run evaluation or replace
the source threshold.

Planner V1 may accept the evidence only when the prior evaluator was valid, the
source evaluation passed, mandatory checks passed, the score is at or above the
current contract threshold, the contracts are compatible, reuse is safe, and the
candidate carries the same complete request binding as the current request. The
current run can then report the contract as met while keeping current-run
evaluation results empty and exposing the source evaluation separately as cache
evidence.

The current Quality Contract remains a separate Planner V1 gate rather than part
of exact request identity. This preserves compatible reuse across current
thresholds while requiring the source score to satisfy the current threshold.
The source evaluator identity, original threshold, score, checks, pass state,
reasons, and recursively immutable metadata remain unchanged.

## Important limitation

`EXACT_REFERENCE` is deterministic benchmark measurement when an expected output
exists. `LLM_JUDGE` is reference-free model-generated measurement. An LLM judge
score is an estimate, not ground truth.

LLM-judge evidence can reflect judge bias, model self-preference, prompt
sensitivity, stochastic behavior, model-version changes, and correlation between
related generator and judge models. Benchmarks must identify the evaluator mode
and must not compare exact-reference and LLM-judge scores as though they were the
same measurement method.

LLM-judge evaluation sends the original task, candidate output, explicit criteria,
and required supplied context to the configured judge model. It sends no unrelated
caller metadata and performs no external factual lookup. Raw judge prompts,
responses, task text, candidate text, and context are excluded from telemetry.

Store with every evaluation:
- evaluator type
- score
- threshold
- pass/fail
- explanation/reasons
- evaluator metadata where safe
- JUDGE model usage and pricing provenance where applicable
