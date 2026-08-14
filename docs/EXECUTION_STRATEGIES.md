# Execution Strategies

## 1. semantic_cache

Use when a sufficiently similar accepted result exists and cache policy allows reuse.

Record:
- similarity
- source run
- cached quality score
- model call avoided
- estimated/known avoided cost

Do not reuse answers for requests with unsafe context-dependent differences.

## 2. small_direct

Use a configured lower-cost model.

Best initial candidate:
- low/medium complexity
- short context
- Standard/High contracts where history shows a good pass rate

## 3. reduce_context_small

Reduce context before inference.

Record:
- original token estimate
- reduced token estimate
- reduction ratio
- reduction method
- model-call tokens

The reducer must preserve information required to answer the user request.

## 4. small_verify_escalate

1. Call small model.
2. Evaluate answer.
3. If pass, return it.
4. If fail, call strong model.
5. Evaluate final answer.
6. Record escalation and both model calls.

This is the primary OPTIMA demo strategy.

## 5. strong_direct

Use as:
- baseline/control
- high-risk fallback
- strategy for requests planner considers difficult

## 6. foundry_model_router

Adapter used to compare OPTIMA with Microsoft Foundry Model Router.

It is not an OPTIMA-owned innovation and should be labeled clearly in demos.

## Planner V1

Start with deterministic rules plus historical evidence.

Example conceptual policy:

```text
if safe semantic cache hit:
    semantic_cache
elif very long context:
    reduce_context_small
elif low complexity and historical pass rate is strong:
    small_direct
elif medium/uncertain complexity:
    small_verify_escalate
else:
    strong_direct
```

Every branch must generate reason codes for explainability.
