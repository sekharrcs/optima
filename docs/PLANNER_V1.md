# OPTIMA Planner V1 Specification

## Purpose

Planner V1 selects an explainable execution plan expected to satisfy the Quality Contract at the lowest reasonable cost.

V1 is deterministic plus bounded historical evidence. It is not reinforcement learning and does not generate arbitrary agent graphs.

## Core design

The planner builds a composable plan rather than choosing only a single monolithic strategy label.

```text
Request
  |
Request Profile
  |
Module/Capability Gates
  |
Cache Policy
  |
Context Policy
  |
Model Policy
  |
Verification / Escalation Policy
  |
Execution Plan
```

A friendly strategy name may still be derived for the UI, such as:

`Context Reduce -> Small -> Verify -> Escalate if needed`

## Separation of responsibilities

- Request Profiler: describes the request.
- Planner: selects the execution plan.
- Executor: executes the plan.
- Evaluator: measures quality.
- Cost Calculator: calculates measured cost from usage.
- Telemetry: records what actually happened.

The evaluator never selects the next model by itself.
The planner contains no provider-specific SDK calls.
The planner contains no Azure model deployment names.

## Inputs

Planner input includes:
- Request Profile
- Quality Contract
- Module Configuration
- Cache candidate metadata, if cache is enabled
- Historical Policy Statistics, if enabled
- Planner thresholds/configuration

## Request Profile

### Task types

V1 supports:
- `SUMMARIZATION`
- `EXTRACTION`
- `CLASSIFICATION`
- `Q_AND_A`
- `CODE_GENERATION`
- `LOG_ANALYSIS`
- `GENERAL_REASONING`
- `UNKNOWN`

Do not add additional task types without an explicit product decision.

### Complexity

V1 complexity values:
- `LOW`
- `MEDIUM`
- `HIGH`

Examples are guidance, not hard-coded keyword rules.

LOW examples:
- short summarization
- structured field extraction
- text classification
- simple rewriting
- straightforward Q&A using supplied context

MEDIUM examples:
- log analysis
- comparison across multiple inputs
- moderate code generation
- multi-step summarization
- reasoning across multiple supplied context fragments

HIGH examples:
- complex code generation
- architecture/design reasoning
- ambiguous multi-step reasoning
- high-risk or highly complex analysis

The profiler may use deterministic signals plus a lightweight structured classifier. A classifier produces profile attributes; it does not select a model.

### Other profile fields

Suggested V1 fields:
- `task_type`
- `complexity`
- `input_tokens`
- `risk_tier`
- `cache_eligible`
- `has_large_context`

## Configurable thresholds

Initial hackathon values are configuration defaults, not universal truths:

```text
cache_similarity_threshold = 0.95
context_reduction_required_tokens = 8000
context_reduction_consider_tokens = 4000
history_minimum_samples = 20
history_small_prefer_pass_rate = 0.95
history_small_avoid_pass_rate = 0.70
```

All thresholds must live in typed configuration.

## Step 0: Module capability gates

Before selecting optimizations, honor `docs/MODULE_CONFIGURATION.md`.

If a module is disabled, the planner must not include it in the plan.

Module state must be represented in reason/debug metadata so a run is explainable.

## Step 1: Semantic cache gate

Only evaluate cache if:
- semantic cache module is enabled
- request profile says cache is eligible
- a cache candidate exists

Select cache only when all are true:
- candidate similarity >= configured similarity threshold
- cached result previously passed a valid evaluator
- cached quality score >= current contract threshold
- cached result is contract-compatible
- request is safe for reuse

Conceptual logic:

```python
if (
    modules.semantic_cache_enabled
    and profile.cache_eligible
    and cache_candidate is not None
    and cache_candidate.similarity >= config.cache_similarity_threshold
    and cache_candidate.quality_score >= contract.minimum_quality_score
    and cache_candidate.contract_compatible
    and cache_candidate.safe_to_reuse
):
    return semantic_cache_plan
```

Primary reason code:
- `CACHE_HIGH_CONFIDENCE_MATCH`

## Step 2: Context policy

Context reduction is an optional plan component, not a mandatory monolithic strategy.

If context reduction module is disabled:
- do not reduce context
- reason: `CONTEXT_REDUCTION_DISABLED`

If enabled, initial policy is:

```text
input tokens < 4,000
    -> no reduction

4,000 <= input tokens < 8,000
    -> consider reduction

input tokens >= 8,000
    -> reduction preferred
```

Risk/quality safeguard:
- for Critical quality with high-risk content, V1 should conservatively skip aggressive context reduction unless a task-specific safe reducer is available

Potential reason codes:
- `CONTEXT_WITHIN_NORMAL_RANGE`
- `CONTEXT_REDUCTION_CONSIDERED`
- `CONTEXT_ABOVE_REDUCTION_THRESHOLD`
- `CONTEXT_REDUCTION_SELECTED`
- `CONTEXT_REDUCTION_SKIPPED_HIGH_RISK`
- `CONTEXT_REDUCTION_DISABLED`

The context reducer must emit before/after token counts and preserve evidence needed by evaluation.

## Step 3: Base model policy

Models are represented conceptually as `SMALL` and `STRONG`.
Provider/deployment configuration maps these roles to actual Azure models.

### Standard quality contract

Initial threshold: 0.80

| Complexity | Base policy |
|---|---|
| LOW | Small direct |
| MEDIUM | Small -> Verify -> Escalate |
| HIGH | Strong direct |

### High quality contract

Initial threshold: 0.90

| Complexity | Base policy |
|---|---|
| LOW | Small -> Verify -> Escalate |
| MEDIUM | Small -> Verify -> Escalate |
| HIGH | Strong direct |

### Critical quality contract

Initial threshold: 0.95

| Complexity | Base policy |
|---|---|
| LOW | Small -> Verify -> Escalate |
| MEDIUM | Strong direct |
| HIGH | Strong direct |

Reason codes include:
- `LOW_COMPLEXITY`
- `MEDIUM_COMPLEXITY`
- `HIGH_COMPLEXITY`
- `STANDARD_QUALITY_CONTRACT`
- `HIGH_QUALITY_CONTRACT`
- `CRITICAL_QUALITY_CONTRACT`
- `SMALL_MODEL_ELIGIBLE`
- `STRONG_MODEL_REQUIRED`

## Step 4: Historical policy adjustment

Historical learning is disabled unless:
- historical policy module is enabled
- sufficient comparable samples exist

Minimum initial evidence:
- at least 20 comparable runs for the relevant task/profile bucket

Historical policy may make the base plan more efficient but must not bypass explicit safety/risk rules.

Initial guidance:

```text
If small-first pass-without-escalation rate >= 0.95
and average quality satisfies the current contract
and sample count >= 20:
    planner may prefer small-first where otherwise optional

If small-first pass-without-escalation rate < 0.70
and sample count >= 20:
    planner may skip an expected-waste small call and go strong directly
```

Reason codes:
- `HISTORICAL_SMALL_SUCCESS_HIGH`
- `HISTORICAL_SMALL_SUCCESS_LOW`
- `HISTORICAL_EVIDENCE_INSUFFICIENT`
- `HISTORICAL_POLICY_DISABLED`

All historical adjustments must record the statistics that supported the change.

## Step 5: Verification and escalation policy

For `small_verify_escalate`:

1. Execute small model.
2. Evaluate its answer.
3. If score >= Quality Contract threshold and mandatory checks pass, return it.
4. Otherwise record quality failure and execute strong model exactly once.
5. Evaluate strong-model result.
6. Return the strong result with final contract status.

Reason/event codes:
- `QUALITY_CONTRACT_MET`
- `QUALITY_THRESHOLD_NOT_MET`
- `ESCALATION_REQUIRED`
- `ESCALATED_TO_STRONG`
- `FINAL_QUALITY_CONTRACT_NOT_MET`

Escalation must record both model calls in total token/cost accounting.

## Plan data model

The concrete implementation may refine names, but must represent at least:

```text
ExecutionPlan
- cache_policy
- context_policy
- initial_model_role
- verification_required
- escalation_model_role (optional)
- reason_codes[]
- human_readable_name
- expected_quality (optional estimate)
- expected_cost (optional estimate)
```

Actual measured quality/cost do not belong in the pre-execution plan.

## Reference pseudocode

```python
def select_plan(request, contract, modules, cache_candidate, history, config):
    profile = profile_request(request)

    if safe_cache_match(
        enabled=modules.semantic_cache_enabled,
        profile=profile,
        candidate=cache_candidate,
        similarity_threshold=config.cache_similarity_threshold,
        minimum_quality=contract.minimum_quality_score,
    ):
        return build_cache_plan(...)

    context_policy = select_context_policy(
        enabled=modules.context_reduction_enabled,
        token_count=profile.input_tokens,
        risk=profile.risk_tier,
        contract=contract,
        config=config,
    )

    model_policy = select_base_model_policy(
        quality_profile=contract.quality_profile,
        complexity=profile.complexity,
    )

    if modules.historical_policy_enabled:
        model_policy = apply_historical_policy(
            base_policy=model_policy,
            profile=profile,
            contract=contract,
            history=history,
            config=config,
        )

    return build_execution_plan(
        profile=profile,
        context_policy=context_policy,
        model_policy=model_policy,
        reasons=collect_reason_codes(...),
    )
```

## Important non-goals

Planner V1 must not contain:
- actual model names
- model SDK/API calls
- Azure deployment identifiers
- evaluator prompts
- Redis/Cosmos calls
- pricing formulas
- reinforcement learning
- arbitrary plan generation

## Required unit-test matrix

Tests must include at least:
- cache hit wins when safe and contract-compatible
- cache disabled skips cache
- low/standard selects small direct
- medium/standard selects small-verify-escalate
- high/high selects strong direct
- low/critical selects small-verify-escalate
- medium/critical selects strong direct
- context reduction disabled never includes reduction
- long context selects reduction when allowed
- critical/high-risk skips unsafe reduction
- insufficient history cannot alter policy
- strong historical evidence can alter eligible policy
- poor small-model history can skip expected-waste small call
- planner reason codes explain every selected component
