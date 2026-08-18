---
description: "Test conventions for proving OPTIMA optimizer behavior"
applyTo: "tests/**/*.py"
---
# Test instructions

Tests should prove optimizer behavior, not merely line coverage.

Use fake model providers to simulate:
- small model passes quality
- small model fails then strong model passes
- both models fail
- semantic cache hit/miss
- long-context reduction
- provider error and timeout

Assert telemetry/cost accounting as well as returned answers.
Paid live model tests must use an explicit marker and be excluded from default test runs.
