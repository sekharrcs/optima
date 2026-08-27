"""Explicit local demo API composition with deterministic fake dependencies."""

from decimal import Decimal

from fastapi import FastAPI

from optima.api.app import create_app
from optima.api.dependencies import ExecutionDependencies
from optima.cache import InMemoryCacheEntry, InMemorySemanticCache
from optima.config import AppSettings
from optima.context import DeterministicExtractiveReducer, RegexTokenCounter
from optima.context.safety import DeterministicExtractiveSafetyPolicy
from optima.cost import CostCalculator, PriceCatalog, PriceCatalogEntry
from optima.domain.cache import CacheCandidate
from optima.domain.evaluation import EvaluationResult
from optima.domain.request_binding import build_request_binding
from optima.domain.request_profile import Complexity, TaskType
from optima.evaluation import EvaluationEvidence, FakeEvaluator
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
    return create_app(execution_dependencies=dependencies)


app = create_demo_app()
