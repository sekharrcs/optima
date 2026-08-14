---
applyTo: "src/ui/**/*.py"
---
# Demo UI instructions

Build for judge comprehension, not feature density.

The primary screen should make three things obvious:
1. What strategy OPTIMA selected and why.
2. Whether the Quality Contract was met.
3. How OPTIMA compares with the baseline on tokens, cost, latency, and quality.

Required UI concepts:
- request input
- Quality Contract controls
- optimization mode
- execution trace/decision explanation
- result
- baseline vs OPTIMA comparison
- aggregate dashboard

Never display invented metrics.
Keep raw debug payloads behind an expandable section.
