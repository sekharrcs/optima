"""Explicit local demo API composition with deterministic fake dependencies."""

from decimal import Decimal

from fastapi import FastAPI

from optima.api.app import create_app
from optima.api.dependencies import ExecutionDependencies
from optima.api.models import RunRequest
from optima.cache import InMemoryCacheEntry, InMemorySemanticCache
from optima.config import AppSettings
from optima.context import DeterministicExtractiveReducer, RegexTokenCounter
from optima.context.safety import DeterministicExtractiveSafetyPolicy
from optima.cost import CostCalculator, PriceCatalog, PriceCatalogEntry
from optima.domain.cache import CacheCandidate
from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    ExecutionPlan,
    ModelPolicy,
    ModelRole,
    PlannerDecisionEvidence,
    PlannerModuleStates,
    PlannerReasonCode,
    SemanticCacheEvidence,
    SemanticCacheOutcome,
)
from optima.domain.quality_contract import (
    OptimizationMode,
    QualityProfile,
    build_quality_contract,
)
from optima.domain.request_binding import build_request_binding
from optima.domain.request_profile import Complexity, TaskType
from optima.domain.run import RunResult
from optima.evaluation import EvaluationEvidence, FakeEvaluator
from optima.execution import ExecutionRequest, PlanExecutor
from optima.planner.policies import effective_risk_tier
from optima.providers import (
    FakeProviderResponse,
    build_fake_small_provider,
    build_fake_strong_provider,
)

DEMO_PROVIDER = "local-demo"
DEMO_CATALOG_VERSION = "local-demo-v1"
DEMO_CURRENCY = "USD"
DEMO_CACHE_INPUT = "Summarize the resolved OPTIMA cache incident."
DEMO_CACHE_CONTEXT = "Incident OPT-9 was resolved after validation."
DEMO_CACHE_OUTPUT = "Incident OPT-9 was resolved after validation."
DEMO_REQUEST_METADATA = {"request_profile_source": "user_supplied_demo_input"}
DEMO_CACHE_REQUEST_BINDING = build_request_binding(
    input_text=DEMO_CACHE_INPUT,
    context=DEMO_CACHE_CONTEXT,
    reference_output=None,
    criteria=(),
    metadata=DEMO_REQUEST_METADATA,
    task_type=TaskType.SUMMARIZATION,
    complexity=Complexity.LOW,
)

_MODE_REASONS = {
    OptimizationMode.COST: PlannerReasonCode.OPTIMIZATION_MODE_COST,
    OptimizationMode.BALANCED: PlannerReasonCode.OPTIMIZATION_MODE_BALANCED,
    OptimizationMode.QUALITY: PlannerReasonCode.OPTIMIZATION_MODE_QUALITY,
}
_PROFILE_REASONS = {
    QualityProfile.STANDARD: PlannerReasonCode.STANDARD_QUALITY_CONTRACT,
    QualityProfile.HIGH: PlannerReasonCode.HIGH_QUALITY_CONTRACT,
    QualityProfile.CRITICAL: PlannerReasonCode.CRITICAL_QUALITY_CONTRACT,
}
_COMPLEXITY_REASONS = {
    Complexity.LOW: PlannerReasonCode.LOW_COMPLEXITY,
    Complexity.MEDIUM: PlannerReasonCode.MEDIUM_COMPLEXITY,
    Complexity.HIGH: PlannerReasonCode.HIGH_COMPLEXITY,
}


def create_demo_app() -> FastAPI:
    """Create an API that executes the real planner/executor flow with local fakes."""
    token_counter = RegexTokenCounter()
    calculator = CostCalculator(
        PriceCatalog(
            version=DEMO_CATALOG_VERSION,
            currency=DEMO_CURRENCY,
            entries=(
                PriceCatalogEntry(
                    provider=DEMO_PROVIDER,
                    deployment="small-demo",
                    input_rate_per_million_tokens=Decimal("0.15"),
                    output_rate_per_million_tokens=Decimal("0.60"),
                ),
                PriceCatalogEntry(
                    provider=DEMO_PROVIDER,
                    deployment="strong-demo",
                    input_rate_per_million_tokens=Decimal("2.50"),
                    output_rate_per_million_tokens=Decimal("10.00"),
                ),
            ),
        )
    )
    dependencies = ExecutionDependencies(
        settings=AppSettings(
            semantic_cache_enabled=True,
            context_reduction_enabled=True,
            historical_policy_enabled=False,
            foundry_router_comparator_enabled=False,
        ),
        small_provider=build_fake_small_provider(
            provider_name=DEMO_PROVIDER,
            deployment_name="small-demo",
            responses=(
                FakeProviderResponse(
                    output_text=(
                        "Local demo response from the configured SMALL model role."
                    ),
                    input_tokens=640,
                    output_tokens=96,
                ),
            ),
        ),
        strong_provider=build_fake_strong_provider(
            provider_name=DEMO_PROVIDER,
            deployment_name="strong-demo",
            responses=(
                FakeProviderResponse(
                    output_text=(
                        "Local demo response from the configured STRONG model role."
                    ),
                    input_tokens=660,
                    output_tokens=112,
                ),
            ),
        ),
        evaluator=FakeEvaluator(
            responses=(
                EvaluationEvidence(
                    evaluator_type="local-demo-deterministic",
                    evaluator_valid=True,
                    score=0.92,
                    metadata={"composition": "local-demo"},
                ),
            )
        ),
        cost_calculator=calculator,
        semantic_cache=InMemorySemanticCache(
            (
                InMemoryCacheEntry(
                    request_binding=DEMO_CACHE_REQUEST_BINDING,
                    candidate=CacheCandidate(
                        source_run_id="run-local-cache-source-1",
                        output_text=DEMO_CACHE_OUTPUT,
                        request_binding=DEMO_CACHE_REQUEST_BINDING,
                        similarity=1.0,
                        prior_evaluation=EvaluationResult(
                            evaluator_type="local-demo-deterministic",
                            evaluator_valid=True,
                            score=0.96,
                            threshold=0.80,
                            mandatory_checks_passed=True,
                            passed=True,
                            reasons=("Source demo contract passed",),
                            metadata={
                                "composition": "local-demo-exact-match",
                            },
                        ),
                        contract_compatible=True,
                        safe_to_reuse=True,
                    ),
                ),
            )
        ),
        context_reducer=DeterministicExtractiveReducer(token_counter),
        token_counter=token_counter,
        context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
    )
    application = create_app(execution_dependencies=dependencies)

    @application.post(
        "/api/v1/demo/fixed-strong-baseline",
        response_model=RunResult,
    )
    async def execute_fixed_strong_baseline(run_request: RunRequest) -> RunResult:
        """Execute one measured fixed-strong benchmark arm for the local demo."""
        quality_contract = build_quality_contract(
            quality_profile=run_request.quality_profile,
            optimization_mode=run_request.optimization_mode,
            risk_tier=run_request.risk_tier,
            grounding_required=run_request.grounding_required,
            max_latency_ms=run_request.max_latency_ms,
            thresholds=dependencies.settings.quality_thresholds(),
        )
        request_binding = build_request_binding(
            input_text=run_request.input_text,
            context=run_request.context,
            reference_output=run_request.reference_output,
            criteria=run_request.criteria,
            metadata=run_request.metadata,
            task_type=run_request.request_profile.task_type,
            complexity=run_request.request_profile.complexity,
        )
        plan = ExecutionPlan(
            cache_policy=CachePolicy.SKIP,
            context_policy=ContextPolicy.KEEP_ORIGINAL,
            model_policy=ModelPolicy.STRONG_DIRECT,
            initial_model_role=ModelRole.STRONG,
            verification_required=True,
            escalation_model_role=None,
            optimization_mode=run_request.optimization_mode,
            quality_profile=run_request.quality_profile,
            reason_codes=(
                PlannerReasonCode.SEMANTIC_CACHE_DISABLED,
                PlannerReasonCode.CONTEXT_REDUCTION_DISABLED,
                _COMPLEXITY_REASONS[run_request.request_profile.complexity],
                _PROFILE_REASONS[run_request.quality_profile],
                _MODE_REASONS[run_request.optimization_mode],
                PlannerReasonCode.STRONG_MODEL_REQUIRED,
            ),
            human_readable_name="Fixed Strong Baseline -> Verify",
            decision_evidence=PlannerDecisionEvidence(
                profile_risk_tier=run_request.request_profile.risk_tier,
                contract_risk_tier=run_request.risk_tier,
                effective_risk_tier=effective_risk_tier(
                    run_request.request_profile.risk_tier,
                    run_request.risk_tier,
                ),
                module_states=PlannerModuleStates(
                    semantic_cache_enabled=False,
                    context_reduction_enabled=False,
                    historical_policy_enabled=False,
                    foundry_router_comparator_enabled=False,
                ),
                cache_candidate_assessed=False,
                base_model_policy=ModelPolicy.STRONG_DIRECT,
                final_model_policy=ModelPolicy.STRONG_DIRECT,
            ),
            request_binding=request_binding,
        )
        executor = PlanExecutor(
            small_provider=dependencies.small_provider,
            strong_provider=dependencies.strong_provider,
            evaluator=dependencies.evaluator,
            cost_calculator=dependencies.cost_calculator,
            context_reducer=dependencies.context_reducer,
            token_counter=dependencies.token_counter,
            monotonic_clock=dependencies.monotonic_clock,
            utc_now=dependencies.utc_now,
        )
        return await executor.execute(
            ExecutionRequest(
                run_id=dependencies.run_id_factory(),
                correlation_id=dependencies.correlation_id_factory(),
                input_text=run_request.input_text,
                context=run_request.context,
                reference_output=run_request.reference_output,
                criteria=run_request.criteria,
                metadata=run_request.metadata,
                quality_contract=quality_contract,
                request_profile=run_request.request_profile,
                execution_plan=plan,
                semantic_cache=SemanticCacheEvidence(
                    outcome=SemanticCacheOutcome.DISABLED_BYPASSED,
                    lookup_latency_ms=0,
                    planner_reason_code=PlannerReasonCode.SEMANTIC_CACHE_DISABLED,
                ),
            )
        )

    return application


app = create_demo_app()
