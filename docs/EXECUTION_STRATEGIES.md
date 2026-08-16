# OPTIMA Execution Capabilities and Plan Patterns

## Purpose

OPTIMA does not permanently encode every possible optimization combination as a separate strategy.

Planner V1 composes execution plans from capabilities:

```text
Cache Policy
+
Context Policy
+
Model Policy
+
Verification / Escalation Policy
```

See `docs/PLANNER_V1.md` for authoritative planner behavior.

Friendly combined plan names may still be shown in the UI.

## Capability 1: Semantic cache

Use when a sufficiently similar previously accepted result exists and cache policy allows reuse.

Record:
- similarity
- source run
- cached quality score
- model call avoided
- estimated/known avoided cost

Do not reuse answers for requests with unsafe context-dependent differences.

Example friendly plan name: `Semantic Cache Hit`

## Capability 2: Context reduction

Context reduction is an optional, configurable pre-inference capability.

It may be combined with either small- or strong-model execution.

Record:
- original token estimate/count
- reduced token estimate/count
- reduction ratio
- reduction method
- preservation/safety metadata when available

The reducer must preserve information required to answer the request.
Context reduction may be disabled through typed module configuration.

Example friendly plan names:
- `Context Reduce -> Small`
- `Context Reduce -> Small -> Verify -> Escalate if needed`
- `Context Reduce -> Strong`

## Capability 3: Small model

Use a configured lower-cost model role when Planner V1 says the request is eligible.

The planner refers only to the conceptual role `SMALL`.
Actual deployment/model names belong in provider configuration.

## Capability 4: Quality verification

Measure output against the current Quality Contract.

Use deterministic evaluators when possible.
Use LLM-as-judge only where deterministic evaluation is insufficient.

Quality verification is mandatory for normal OPTIMA runs that claim contract compliance.

## Capability 5: Strong-model execution / escalation

Use the `STRONG` role:
- directly when Planner V1 determines that risk/complexity/Optimization Mode warrants it
- after a failed small-model quality check

Strong-model escalation must occur at most once in the MVP small-verify-escalate pattern.

## Capability 6: Foundry Model Router comparator

Microsoft Foundry Model Router is a comparison baseline/candidate execution path.

It is not OPTIMA's planner and must not be presented as OPTIMA's own innovation.

The hackathon comparison may include:
- fixed strong model baseline
- Foundry Model Router
- OPTIMA

## Common friendly plan patterns

These names are presentation conveniences, not separate architectural engines.

### Small Direct

```text
Small -> Verify -> Return
```

Even when the model policy is called `small_direct`, the final result must still be evaluated before OPTIMA claims Quality Contract compliance.

### Small -> Verify -> Escalate

```text
Small
  |
Verify
  | pass -> Return
  | fail -> Strong -> Verify -> Return
```

### Context Reduce -> Small -> Verify -> Escalate

```text
Reduce Context
  |
Small
  |
Verify
  | pass -> Return
  | fail -> Strong -> Verify -> Return
```

### Strong Direct

```text
Strong -> Verify -> Return
```

### Semantic Cache

```text
Safe Accepted Cache Result -> Return
```

The cache result must already have valid quality evidence compatible with the current contract.

## Planner authority

The authoritative selection logic is in `docs/PLANNER_V1.md`.

Do not implement routing rules from friendly plan labels in this document.
Do not treat context reduction as a model-routing strategy.
Do not hard-code provider/model names in plan-selection logic.
