# OPTIMA Planner V1 Specification

## Purpose

Planner V1 selects an explainable execution plan expected to satisfy the Quality Contract efficiently under the selected Optimization Mode.

V1 is deterministic plus bounded historical evidence. It is not reinforcement learning and does not generate arbitrary agent graphs.

## Core principles

1. **Quality Contract is a constraint, not a suggestion.**
2. **Optimization Mode changes aggressiveness, not the minimum quality threshold.**
3. **OPTIMA does not always start with the small model.**
4. **HIGH-complexity requests start with the strong model in V1.**
5. **Whenever the planner chooses a small-model first attempt, strong-model fallback is available if the small result fails quality.**
6. **Context reduction is independent and configurable.**
7. **Historical evidence may optimize eligible choices but may not bypass explicit safety or HIGH-complexity rules in V1.**

## Core design

The planner builds a composable plan:

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

A friendly plan name may be derived for the UI, such as:

`Context Reduce -> Small -> Verify -> Escalate if needed`

or

`Strong -> Verify`

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
- Quality Contract, including Optimization Mode
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

The profiler may use deterministic signals plus a lightweight structured classifier.
A classifier produces profile attributes; it does not select a model.

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

## Quality Profile vs Optimization Mode

These are separate inputs:

- Quality Profile sets the minimum acceptable quality score.
- Optimization Mode changes how aggressively OPTIMA pursues lower-cost plans.

Optimization Mode must never lower the Quality Contract threshold.

Reason codes must always include exactly one of:
- `OPTIMIZATION_MODE_COST`
- `OPTIMIZATION_MODE_BALANCED`
- `OPTIMIZATION_MODE_QUALITY`

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

Optimization Mode does not override cache safety or contract compatibility.

## Step 2: Context policy

Context reduction is an optional plan component, not a monolithic strategy.

If `context_reduction_enabled == false`:
- do not reduce context
- reason: `CONTEXT_REDUCTION_DISABLED`

V1 must make a deterministic decision. There is no unresolved `CONSIDER` state in the final Execution Plan.

### COST mode

If a safe reducer is available and the request is not blocked by risk safeguards:

```text
input tokens < 4,000       -> KEEP_ORIGINAL
input tokens >= 4,000      -> REDUCE
```

### BALANCED mode

If a safe reducer is available and the request is not blocked by risk safeguards:

```text
input tokens < 8,000       -> KEEP_ORIGINAL
input tokens >= 8,000      -> REDUCE
```

### QUALITY mode

```text
input tokens < 8,000       -> KEEP_ORIGINAL
input tokens >= 8,000      -> REDUCE only when:
                               - a task-safe reducer is available
                               - risk tier is LOW or MEDIUM
                               - contract is not Critical + HIGH risk
                             otherwise KEEP_ORIGINAL
```

### Hard safeguard

For `CRITICAL` quality with `HIGH` risk, V1 must keep original context unless an explicitly task-safe reducer is configured and approved for that task class.

Potential reason codes:
- `CONTEXT_WITHIN_NORMAL_RANGE`
- `CONTEXT_ABOVE_REDUCTION_THRESHOLD`
- `CONTEXT_REDUCTION_SELECTED`
- `CONTEXT_REDUCTION_SKIPPED_HIGH_RISK`
- `CONTEXT_REDUCTION_SKIPPED_QUALITY_MODE`
- `CONTEXT_REDUCTION_DISABLED`
- `SAFE_REDUCER_UNAVAILABLE`

The context reducer must emit before/after token counts and preserve evidence needed by evaluation.

## Step 3: Base model policy

Models are represented conceptually as `SMALL` and `STRONG`.
Provider/deployment configuration maps these roles to actual Azure models.

The model policy has only two normal V1 choices:

- `SMALL_FIRST_WITH_FALLBACK`
- `STRONG_DIRECT`

A semantic cache hit is handled earlier and bypasses model execution.

### V1 invariant: HIGH complexity starts strong

For every Quality Profile and every Optimization Mode:

```text
complexity == HIGH -> STRONG_DIRECT
```

V1 does not try the small model first for HIGH-complexity work.

Future versions may relax this only if benchmark evidence supports a task-specific policy change.

### COST mode

| Quality Profile | LOW | MEDIUM | HIGH |
|---|---|---|---|
| Standard | Small first + fallback | Small first + fallback | Strong direct |
| High | Small first + fallback | Small first + fallback | Strong direct |
| Critical | Small first + fallback | Strong direct | Strong direct |

### BALANCED mode

| Quality Profile | LOW | MEDIUM | HIGH |
|---|---|---|---|
| Standard | Small first + fallback | Small first + fallback | Strong direct |
| High | Small first + fallback | Small first + fallback | Strong direct |
| Critical | Small first + fallback | Strong direct | Strong direct |

### QUALITY mode

| Quality Profile | LOW | MEDIUM | HIGH |
|---|---|---|---|
| Standard | Small first + fallback | Strong direct | Strong direct |
| High | Small first + fallback | Strong direct | Strong direct |
| Critical | Strong direct | Strong direct | Strong direct |

These are initial hackathon policy defaults and may later be calibrated from benchmark evidence.

### Why Cost and Balanced can still differ

The base model matrix intentionally keeps COST and BALANCED similar for LOW/MEDIUM model selection in V1.

They still differ through:
- context-reduction aggressiveness
- future historical-policy eligibility
- expected-cost weighting

Do not invent extra model-routing differences merely to make the modes look different.

Reason codes include:
- `LOW_COMPLEXITY`
- `MEDIUM_COMPLEXITY`
- `HIGH_COMPLEXITY`
- `STANDARD_QUALITY_CONTRACT`
- `HIGH_QUALITY_CONTRACT`
- `CRITICAL_QUALITY_CONTRACT`
- `OPTIMIZATION_MODE_COST`
- `OPTIMIZATION_MODE_BALANCED`
- `OPTIMIZATION_MODE_QUALITY`
- `SMALL_FIRST_SELECTED`
- `STRONG_MODEL_REQUIRED`
- `HIGH_COMPLEXITY_STRONG_DIRECT`
- `QUALITY_MODE_PREFERS_STRONG`

## Step 4: Historical policy adjustment

Historical policy is applied only when:
- `historical_policy_enabled == true`
- at least `history_minimum_samples` comparable runs exist

Comparable history should initially be bucketed by:
- task type
- Quality Profile
- Optimization Mode
- relevant risk tier where practical

### Positive small-first evidence

If all are true:
- sample count >= 20
- small-first pass-without-escalation rate >= 0.95
- average final quality satisfies the current contract

then:
- COST may strengthen confidence in already-eligible small-first paths
- BALANCED may strengthen confidence in already-eligible small-first paths
- QUALITY does not downgrade a base strong-direct decision in V1

### Poor small-first evidence

If all are true:
- sample count >= 20
- small-first pass-without-escalation rate < 0.70

then COST or BALANCED may replace an eligible small-first base policy with `STRONG_DIRECT` to avoid an expected-waste small call.

### V1 historical-policy guardrail

Historical policy must **not** change a HIGH-complexity request from `STRONG_DIRECT` to small-first in V1.

Historical evidence must never bypass explicit safety/risk rules.

Reason codes:
- `HISTORICAL_SMALL_SUCCESS_HIGH`
- `HISTORICAL_SMALL_SUCCESS_LOW`
- `HISTORICAL_EVIDENCE_INSUFFICIENT`
- `HISTORICAL_POLICY_DISABLED`

All historical adjustments must record the statistics that supported the change.

## Step 5: Verification and escalation policy

Quality evaluation is mandatory for every normal model-executed plan before OPTIMA claims Quality Contract compliance.

### Small first with fallback

1. Execute small model.
2. Evaluate its answer.
3. If score >= Quality Contract threshold and mandatory checks pass, return it.
4. Otherwise record quality failure.
5. Execute strong model exactly once.
6. Evaluate strong-model result.
7. Return the strong result with final contract status.

There is no normal `small_direct_without_fallback` policy in V1.

If the small result fails the Quality Contract and a configured strong fallback is available, OPTIMA must not knowingly stop at the failed small result.

### Strong direct

1. Execute strong model.
2. Evaluate.
3. Return result with contract pass/fail status.

A strong result may still fail the Quality Contract. V1 records that final failure; it does not create arbitrary additional escalation chains.

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
- optimization_mode
- reason_codes[]
- human_readable_name
- expected_quality (optional estimate)
- expected_cost (optional estimate)
```

For `SMALL_FIRST_WITH_FALLBACK`:

```text
initial_model_role = SMALL
verification_required = true
escalation_model_role = STRONG
```

For `STRONG_DIRECT`:

```text
initial_model_role = STRONG
verification_required = true
escalation_model_role = null
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
        optimization_mode=contract.optimization_mode,
        config=config,
    )

    model_policy = select_base_model_policy(
        quality_profile=contract.quality_profile,
        complexity=profile.complexity,
        optimization_mode=contract.optimization_mode,
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
        optimization_mode=contract.optimization_mode,
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
- low/standard/cost selects small-first-with-fallback
- medium/standard/balanced selects small-first-with-fallback
- high/standard/cost selects strong-direct
- high/standard/balanced selects strong-direct
- high/standard/quality selects strong-direct
- high/high/cost selects strong-direct
- high/critical/cost selects strong-direct
- medium/high/quality selects strong-direct
- low/critical/balanced selects small-first-with-fallback
- low/critical/quality selects strong-direct
- every small-first plan has `STRONG` fallback configured
- failed small evaluation escalates exactly once
- passing small evaluation does not call strong
- context reduction disabled never includes reduction
- 4k-8k context reduces in COST when safe but not BALANCED
- >=8k context reduces in BALANCED when safe
- QUALITY mode uses conservative context-reduction rules
- critical/high-risk skips unsafe reduction
- insufficient history cannot alter policy
- positive history cannot move HIGH complexity to small-first in V1
- poor small-model history can skip expected-waste small call
- planner reason codes explain every selected component
- optimization-mode reason code is always present
