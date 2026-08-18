---
description: "Streamlit UI conventions for the OPTIMA hackathon experience"
applyTo: "src/ui/**/*.py"
---
# Demo UI instructions

`docs/UI_SPEC.md` is authoritative for hackathon UI behavior.

Build for judge comprehension, not feature density.

The MVP has exactly three primary views:
1. Execute
2. Dashboard
3. Run History

The primary Execute screen should make three things obvious:
1. What execution plan OPTIMA selected and why.
2. Whether the Quality Contract was met.
3. How OPTIMA compares with the baseline on tokens, cost, latency, and quality.

Required UI concepts:
- request input
- optional context input
- Quality Contract controls
- optimization mode
- execution trace/decision explanation
- result
- baseline vs OPTIMA comparison
- aggregate dashboard
- run history/detail inspection

Render execution trace and explanation from backend execution data and planner reason codes.
The UI must not invent planner steps, model calls, quality scores, or savings.

Module enable/disable controls are not an MVP UI feature. See `docs/MODULE_CONFIGURATION.md`.

Never display invented metrics.
Keep raw debug payloads behind an expandable section.
