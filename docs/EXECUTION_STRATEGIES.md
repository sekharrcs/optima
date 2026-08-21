---
title: OPTIMA Execution Capabilities and Plan Patterns
description: Composable execution capabilities and presentation patterns for OPTIMA plans
---

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
- actual cache lookup latency and outcome
- zero current-run model calls and model tokens

Do not reuse answers for requests with unsafe context-dependent differences.

The runtime performs one lookup before planning, deeply revalidates the adapter
value inside the lookup failure boundary, and binds the exact resolved output
and source evidence to an accepted plan. Malformed adapter values become typed
lookup failures and continue through model execution. The runtime does not
search again during execution. Planner V1 remains the sole authority for
similarity, prior quality, current-threshold compatibility, contract
compatibility, and reuse safety.

Reuse also requires exact equality between the candidate's complete source
request binding and the current request binding. The versioned binding covers
input text, original context, reference output, ordered criteria, caller
metadata, task type, and complexity. A mismatch records a rejected match with
`CACHE_REQUEST_BINDING_MISMATCH`, then follows the selected context and model
path. The local exact-match cache uses the same complete binding as its key.

Planner V1 snapshots assessed candidate facts separately from the cached output.
The assessment contains the source request binding, source identity, similarity,
prior evaluation, contract compatibility, and reuse-safety decision. Rejected
runtime evidence must equal this planner-owned assessment, while accepted plans
also retain the exact cached output selected for return.

The source evaluation remains unchanged, including its source threshold, pass
result, checks, reasons, and metadata. A cache hit does not run or claim a new
evaluation. The current contract result is derived from Planner V1's acceptance
of that valid source evidence and its inclusive score comparison against the
current threshold.

Actual cache-run model cost is unavailable because no model usage exists. Avoided
cost is reported only when a compatible measured baseline supports it; source or
estimated cost must not be presented as current calculated cost.

Cache evidence is recursively immutable after validation. Nested JSON objects
and arrays reject mutation while API and UI serialization still emits ordinary
JSON objects and arrays. A cache hit has exactly one leading successful cache
step with reuse and quality-met events, followed by one successful return step.
Miss, rejection, failure, and timeout each have one exact leading step contract
before normal model execution. Disabled and ineligible bypasses have no cache
execution step. Model execution uses contiguous sequence numbers and exact causal
ordering: a model call must succeed before evaluation, a failed SMALL evaluation
must precede one SMALL-to-STRONG escalation, and return follows a successful
evaluation. A successful return requires a completed run and exact model-role
and contract-result facts. A failed return is valid only when invalid final
evaluator evidence causes the run to fail closed, with matching role and error
facts.

Example friendly plan name:

`Cached Result`

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

Runtime reduction evidence comes from an injected token counter applied to the
original and reduced context. `RequestProfile.input_tokens` remains a planning
input and is not treated as measured runtime proof.

If a configured reducer fails, times out, returns invalid evidence, reports token
counts that disagree with the runtime counter, or does not reduce measured tokens,
the small-first executor records the unsuccessful reduction step and continues with
the unchanged original context. The model-call trace identifies that original context
was used. A selected reduction plan without its reducer or token-counter dependency
fails structurally before any model call.

Deterministic fixture checks can prove that named benchmark facts survive one local
extractive reduction. They do not establish general semantic preservation.

The local demo therefore uses a request-aware safety policy with a narrow supported
envelope. It marks the deterministic reducer task-safe only for `LOW`-complexity
`SUMMARIZATION` requests whose context contains at least two non-empty lines, at least
one byte-for-byte duplicate non-blank line, and no unique line that the reducer would
discard. Within that envelope, runtime reduction removes duplicate lines only and
preserves retained source-line text. Task types other than
summarization, `MEDIUM` or `HIGH` complexity, single-line context, context without a
duplicate, or context containing a unique unsupported line are not established safe
and keep the original context with `SAFE_REDUCER_UNAVAILABLE`.

This local policy does not claim general semantic preservation. Quality Profile and
risk safeguards remain Planner V1 decisions. The local policy never approves the
critical/high-risk exception, so Planner V1's existing critical/high-risk and QUALITY
mode safeguards remain authoritative.

Example friendly plan names:
- `Reduce Context -> Small -> Verify -> Escalate if needed`
- `Reduce Context -> Strong -> Verify`

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

### Reduce Context -> Small -> Verify -> Escalate if needed

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

### Reduce Context -> Strong

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
