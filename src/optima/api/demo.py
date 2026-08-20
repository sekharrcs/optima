"""Explicit local demo API composition with deterministic fake dependencies."""

from decimal import Decimal

from fastapi import FastAPI

from optima.api.app import create_app
from optima.api.dependencies import ExecutionDependencies
from optima.config import AppSettings
from optima.context import DeterministicExtractiveReducer, RegexTokenCounter
from optima.context.safety import DeterministicExtractiveSafetyPolicy
from optima.cost import CostCalculator, PriceCatalog, PriceCatalogEntry
from optima.evaluation import EvaluationEvidence, FakeEvaluator
from optima.providers import (
    FakeProviderResponse,
    build_fake_small_provider,
    build_fake_strong_provider,
)

DEMO_PROVIDER = "local-demo"
DEMO_CATALOG_VERSION = "local-demo-v1"
DEMO_CURRENCY = "USD"


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
            semantic_cache_enabled=False,
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
        context_reducer=DeterministicExtractiveReducer(token_counter),
        token_counter=token_counter,
        context_reducer_safety_policy=DeterministicExtractiveSafetyPolicy(),
    )
    return create_app(execution_dependencies=dependencies)


app = create_demo_app()
