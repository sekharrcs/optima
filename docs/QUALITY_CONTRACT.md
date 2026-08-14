# Quality Contract

## Purpose

A Quality Contract expresses the minimum acceptable outcome for one request while also describing optimization preferences.

## Domain model

Suggested fields:

```text
quality_profile: STANDARD | HIGH | CRITICAL
minimum_quality_score: float
optimization_mode: COST | BALANCED | QUALITY
max_latency_ms: optional integer
risk_tier: LOW | MEDIUM | HIGH
```

The UI should normally expose the profile and optimization mode, not raw scores.

## Initial hackathon defaults

These values are configuration, not universal truths:

| Profile | Initial threshold |
|---|---:|
| Standard | 0.80 |
| High | 0.90 |
| Critical | 0.95 |

They must be easy to change after benchmark calibration.

## Pass condition

A final answer passes when:
- the selected evaluator is valid for the task, and
- its measured score is at or above the threshold, and
- any mandatory deterministic checks pass.

## Important limitation

An LLM judge score is an estimate, not ground truth. Store the evaluator type and explanation with every score.
