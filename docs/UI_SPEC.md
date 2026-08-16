# OPTIMA Hackathon UI Specification

## Purpose

The UI exists to make OPTIMA's value understandable within seconds:

> OPTIMA selected an execution plan, satisfied the Quality Contract, and used fewer resources than the baseline.

This is not a chatbot-first UI. The answer matters, but the product being demonstrated is the optimization decision and its measured outcome.

## MVP technology

Use Streamlit for the hackathon UI.

Do not add authentication, user administration, tenant settings, or a general-purpose configuration console to the MVP.

## Navigation

The MVP has exactly three primary views:

1. Execute
2. Dashboard
3. Run History

## Screen 1: Execute

### Header

Display:
- Product name: OPTIMA
- Subtitle: Quality-Constrained AI Execution Optimizer
- Short value statement: Find the lowest-cost execution plan that satisfies the required quality.

Avoid generic chatbot branding or a chat-style conversation layout.

### Request panel

Required controls:
- Task/request text area
- Optional context/supporting-data text area
- Quality profile selector: Standard / High / Critical
- Optimization mode selector: Cost / Balanced / Quality
- Primary action: Run with OPTIMA

Hackathon demo defaults:
- Quality profile: High
- Optimization mode: Cost

Do not expose raw quality thresholds by default. They may appear in an advanced/debug expander.

### Execution trace

After execution begins, show the actual plan steps in order.

Example without escalation:

```text
Request Profiled
      |
Context Reduction Selected
      |
Context: 14,820 -> 4,120 tokens
      |
Small Model
      |
Quality Evaluation: 0.93
Required: 0.90
      |
QUALITY CONTRACT MET
```

Example with escalation:

```text
Small Model
    |
Quality = 0.82
Required = 0.90
    |
FAILED
    |
Escalate
    |
Strong Model
    |
Quality = 0.95
    |
PASSED
```

The UI must render trace data returned by the backend. It must not invent execution steps or planner decisions.

### OPTIMA Decision card

Required fields:
- Human-readable plan name
- Plan components
- Reason explanation derived from structured planner reason codes
- Required quality threshold
- Final measured quality
- Contract result: Met / Not Met
- Whether escalation occurred

Example plan label:

`Context Reduce -> Small -> Verify -> Escalate if needed`

Reason text must be deterministic from planner reason codes. Do not call an LLM only to explain the planner.

### Answer/result section

Display the final answer after the decision card.

The answer should be readable but should not visually dominate the optimizer metrics.

### Baseline vs OPTIMA comparison

Show measured values side-by-side.

Required metrics when available:
- Model calls
- Input tokens
- Output tokens
- Total tokens
- Cost
- Latency
- Quality score
- Quality Contract pass/fail

Show savings only when both a baseline and OPTIMA measurement exist.

Preferred emphasis:
- Token reduction percentage
- Cost reduction percentage
- Both executions' Quality Contract status

Do not present a lower-but-passing OPTIMA quality score as a quality improvement. The correct message is that the required quality was satisfied at lower resource cost.

Clearly distinguish:
- Measured actuals
- Estimates
- Unavailable metrics

## Screen 2: Dashboard

### KPI cards

Show:
- Cost saved vs baseline
- Tokens saved vs baseline
- Quality Contract pass rate
- Latency change vs baseline

All values must be computed from stored runs. Never use fabricated demo numbers in production code.

### Strategy / plan usage

Show the distribution of plan types/components, for example:
- Small direct
- Small -> verify
- Context reduce -> small
- Semantic cache
- Strong direct
- Escalated runs

### Savings attribution

When attribution can be measured, show savings associated with:
- Context reduction
- Smaller model usage
- Semantic cache
- Avoided strong-model escalation

If attribution is not reliable, omit it rather than inventing it.

### Aggregate comparison

Show baseline vs OPTIMA for the same benchmark set:
- Requests
- Tokens
- Cost
- Quality Contract pass rate
- Average latency

## Screen 3: Run History

Display a compact table with:
- Run ID
- Timestamp
- Task type
- Plan name
- Final quality
- Cost
- Savings vs baseline
- Contract status

Selecting a run shows:
- Request profile
- Quality Contract
- Planner reason codes
- Execution steps
- Model usage
- Evaluations
- Baseline comparison
- Errors/timeouts if any

## Advanced/debug information

Raw JSON, provider metadata, evaluator metadata, and correlation IDs belong in expandable debug sections.

Do not expose secrets, API keys, connection strings, or full sensitive prompts by default.

## Module configuration in the UI

Module enable/disable switches are NOT part of the MVP user interface.

The backend must support configuration flags so modules can be disabled during experiments or deployment. See `docs/MODULE_CONFIGURATION.md`.

A future admin/settings view may expose these flags after the hackathon.

## Visual design guidance

- Optimize for judge comprehension, not feature density.
- Use whitespace and clear information hierarchy.
- Make the selected execution plan visually obvious.
- Make Quality Contract status visually obvious.
- Make baseline-vs-OPTIMA savings visually obvious.
- Avoid excessive animations; a simple progressive execution trace is sufficient.
- Keep the primary Execute screen usable on a laptop without horizontal scrolling.

## Acceptance criteria

The Execute screen is complete when a user can:
1. Enter a request and optional context.
2. Select Quality and Optimization profiles.
3. Execute OPTIMA.
4. See the chosen plan and why it was chosen.
5. See actual execution steps.
6. See whether the Quality Contract was met.
7. See the final answer.
8. Compare measured baseline and OPTIMA cost/tokens/latency/quality.

The Dashboard is complete when aggregate values are derived only from stored measured runs.

The Run History is complete when a user can inspect the full evidence behind one prior decision.
