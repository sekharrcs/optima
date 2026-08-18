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

Example friendly plan name:

`Semantic Cache Hit`

## Capability 2: Context reduction

Context reduction is an optional, configurable pre-inference capability.

It may be combined with either small-first or strong-direct execution.

Record:
- original token estimate/count
- reduced token estimate/count
- reduction ratio
- reduction method
- preservation/safety metadata when available

The reducer must preserve information required to answer the request.
Context reduction may be disabled through typed module configuration.

Example friendly plan names:
- `Context Reduce -> Small -> Verify -> Escalate if needed`
- `Context Reduce -> Strong -> Verify`

## Capability 3: Small-model first attempt

Use the configured lower-cost model role only when Planner V1 says the request is eligible.

The planner refers only to the conceptual role `SMALL`.
Actual deployment/model names belong in provider configuration.

In V1, a small-model first attempt always includes:
- quality verification
- configured `STRONG` fallback when quality is not met

There is no normal small-model execution path that knowingly returns a failed Quality Contract without attempting the available strong fallback.

## Capability 4: Quality verification

Measure output against the current Quality Contract.

Use deterministic evaluators when possible.
Use LLM-as-judge only where deterministic evaluation is insufficient.

Quality verification is mandatory for normal OPTIMA runs that claim contract compliance.

## Capability 5: Strong-model execution / escalation

Use the `STRONG` role:
- directly for every HIGH-complexity request in Planner V1
- directly when Quality Profile / Optimization Mode policy requires it
- after a failed eligible small-model first attempt

Strong-model escalation occurs at most once in V1.

## Capability 6: Foundry Model Router comparator

Microsoft Foundry Model Router is a comparison baseline/candidate execution path.

It is not OPTIMA's planner and must not be presented as OPTIMA's own innovation.

The hackathon comparison may include:
- fixed strong model baseline
- Foundry Model Router
- OPTIMA

## Common friendly plan patterns

These names are presentation conveniences, not separate architectural engines.

### Small -> Verify -> Escalate if needed

```text
Small
  |
Verify
  | pass -> Return
  | fail -> Strong -> Verify -> Return
```

### Context Reduce -> Small -> Verify -> Escalate if needed

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

Planner V1 uses this pattern for all HIGH-complexity requests.

### Context Reduce -> Strong

```text
Reduce Context
  |
Strong
  |
Verify
  |
Return
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
Do not introduce `small_direct_without_fallback` in Planner V1.
