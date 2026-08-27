"""Execution integration tests for reference-free LLM judge evaluation."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

from optima.cost import CostCalculator, PriceCatalog, PriceCatalogEntry
from optima.domain.execution import ExecutionPlan, ModelRole
from optima.domain.quality_contract import (
    OptimizationMode,
    QualityContract,
    QualityProfile,
    RiskTier,
)
from optima.domain.request_binding import build_request_binding
from optima.domain.request_profile import Complexity, RequestProfile, TaskType
from optima.domain.run import RunStatus
from optima.evaluation import LLM_JUDGE_RESPONSE_SCHEMA_VERSION, LLMJudgeEvaluator
from optima.execution import ExecutionRequest, PlanExecutor
from optima.planner import (
    ContextReducerCapability,
    ModuleConfiguration,
    PlannerInput,
    PlanningFailure,
    select_plan,
)
from optima.providers import FakeModelProvider, FakeProviderResponse


class IncrementingClock:
    """Deterministic monotonic clock requiring no sleeps."""

    def __init__(self) -> None:
        self._value = 0.0

    def now(self) -> float:
        value = self._value
        self._value += 0.001
        return value


def contract() -> QualityContract:
    """Build the reference-free execution contract."""
    return QualityContract(
        quality_profile=QualityProfile.HIGH,
        minimum_quality_score=0.9,
        optimization_mode=OptimizationMode.COST,
        risk_tier=RiskTier.LOW,
    )


def profile() -> RequestProfile:
    """Build a request profile that selects SMALL-first."""
    return RequestProfile(
        task_type=TaskType.SUMMARIZATION,
        complexity=Complexity.LOW,
        input_tokens=100,
        risk_tier=RiskTier.LOW,
        cache_eligible=False,
        has_large_context=False,
    )


def plan() -> ExecutionPlan:
    """Select one authoritative Planner V1 small-first plan."""
    result = select_plan(
        PlannerInput(
            request_profile=profile(),
            request_binding=build_request_binding(
                input_text="Summarize the incident",
                context=None,
                reference_output=None,
                criteria=(),
                metadata={},
                task_type=TaskType.SUMMARIZATION,
                complexity=Complexity.LOW,
            ),
            quality_contract=contract(),
            modules=ModuleConfiguration(
                semantic_cache_enabled=False,
                context_reduction_enabled=False,
                historical_policy_enabled=False,
                foundry_router_comparator_enabled=False,
            ),
            reducer_capability=ContextReducerCapability(
                available=False,
                task_safe=False,
                approved_for_critical_high_risk=False,
            ),
        )
    )
    assert not isinstance(result, PlanningFailure)
    return result


def execution_request() -> ExecutionRequest:
    """Build one execution request without reference output."""
    return ExecutionRequest(
        run_id="run-judge-execution",
        correlation_id="correlation-judge-execution",
        input_text="Summarize the incident",
        quality_contract=contract(),
        request_profile=profile(),
        execution_plan=plan(),
    )


def response(score: float) -> str:
    """Build one valid judge response for the configured threshold."""
    return json.dumps(
        {
            "schema_version": LLM_JUDGE_RESPONSE_SCHEMA_VERSION,
            "score": score,
            "criteria": [],
            "grounded": None,
            "reason_code": "CORRECTNESS_OR_RELEVANCE_CONCERN",
            "explanation": "Measured candidate quality.",
        }
    )


def provider(
    role: ModelRole,
    deployment: str,
    responses: tuple[FakeProviderResponse, ...],
) -> FakeModelProvider:
    """Build a deterministic fake for one explicit role."""
    return FakeModelProvider(
        provider_name="fake-foundry",
        deployment_name=deployment,
        model_role=role,
        responses=responses,
        clock=IncrementingClock(),
    )


def provider_response(
    output_text: str,
    input_tokens: int,
    output_tokens: int,
) -> FakeProviderResponse:
    """Build one unpriced provider result template."""
    return FakeProviderResponse(
        output_text=output_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def calculator(*, include_judge: bool = True) -> CostCalculator:
    """Build a complete artificial catalog, optionally omitting JUDGE pricing."""
    entries = [
        PriceCatalogEntry(
            provider="fake-foundry",
            deployment="small-deployment",
            input_rate_per_million_tokens=Decimal("2"),
            output_rate_per_million_tokens=Decimal("40"),
        ),
        PriceCatalogEntry(
            provider="fake-foundry",
            deployment="strong-deployment",
            input_rate_per_million_tokens=Decimal("30"),
            output_rate_per_million_tokens=Decimal("190"),
        ),
    ]
    if include_judge:
        entries.append(
            PriceCatalogEntry(
                provider="fake-foundry",
                deployment="judge-deployment",
                input_rate_per_million_tokens=Decimal("5"),
                output_rate_per_million_tokens=Decimal("25"),
            )
        )
    return CostCalculator(
        PriceCatalog(
            version="judge-pricing-v1",
            currency="TEST",
            entries=tuple(entries),
        )
    )


def build_executor(
    judge_outputs: tuple[str, ...],
    *,
    include_judge_price: bool = True,
) -> tuple[PlanExecutor, FakeModelProvider, FakeModelProvider, FakeModelProvider]:
    """Build generator roles, judge role, and one executor."""
    small = provider(
        ModelRole.SMALL,
        "small-deployment",
        (provider_response("small answer", 100, 20),),
    )
    strong = provider(
        ModelRole.STRONG,
        "strong-deployment",
        (provider_response("strong answer", 110, 30),),
    )
    judge = provider(
        ModelRole.JUDGE,
        "judge-deployment",
        tuple(provider_response(output, 50, 10) for output in judge_outputs),
    )
    executor = PlanExecutor(
        small_provider=small,
        strong_provider=strong,
        evaluator=LLMJudgeEvaluator(
            provider=judge,
            judge_model="judge-model-v1",
        ),
        cost_calculator=calculator(include_judge=include_judge_price),
        monotonic_clock=IncrementingClock(),
        utc_now=lambda: datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
    )
    return executor, small, strong, judge


def test_passing_small_judge_avoids_strong_and_includes_judge_economics() -> None:
    """Accept SMALL only after one passing judge call with visible cost and tokens."""
    executor, small, strong, judge = build_executor((response(0.95),))

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.COMPLETED
    assert result.final_output == "small answer"
    assert result.contract_met is True
    assert result.escalated is False
    assert len(small.calls) == 1
    assert len(strong.calls) == 0
    assert len(judge.calls) == 1
    assert tuple(usage.model_role for usage in result.model_usages) == (
        ModelRole.SMALL,
        ModelRole.JUDGE,
    )
    assert result.total_input_tokens == 150
    assert result.total_output_tokens == 30
    assert result.total_tokens == 180
    assert result.total_calculated_cost == Decimal("0.0015")
    assert result.total_cost_provenance is not None
    assert result.total_cost_provenance.catalog_version == "judge-pricing-v1"


def test_failed_small_judge_escalates_once_and_counts_both_judgments() -> None:
    """Escalate exactly once and include both judge calls in final economics."""
    executor, small, strong, judge = build_executor((response(0.5), response(0.95)))

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.COMPLETED
    assert result.final_output == "strong answer"
    assert result.contract_met is True
    assert result.escalated is True
    assert len(small.calls) == 1
    assert len(strong.calls) == 1
    assert len(judge.calls) == 2
    assert tuple(usage.model_role for usage in result.model_usages) == (
        ModelRole.SMALL,
        ModelRole.JUDGE,
        ModelRole.STRONG,
        ModelRole.JUDGE,
    )
    assert result.total_tokens == 380
    assert result.total_calculated_cost == Decimal("0.0110")
    assert len(result.evaluations) == 2


def test_missing_judge_price_keeps_total_cost_unknown() -> None:
    """Never report generator-only cost as the complete optimization economics."""
    executor, _, _, _ = build_executor(
        (response(0.95),),
        include_judge_price=False,
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.COMPLETED
    assert result.model_usages[0].calculated_cost == Decimal("0.001")
    assert result.model_usages[1].calculated_cost is None
    assert result.total_calculated_cost is None
    assert result.total_cost_provenance is None


def test_malformed_judge_output_fails_run_but_retains_priced_usage() -> None:
    """Stop without a score while retaining measured call economics."""
    executor, _, strong, judge = build_executor(("not-json",))

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.FAILED
    assert result.final_output is None
    assert result.final_evaluation is None
    assert result.evaluations == ()
    assert result.contract_met is None
    assert len(strong.calls) == 0
    assert len(judge.calls) == 1
    assert tuple(usage.model_role for usage in result.model_usages) == (
        ModelRole.SMALL,
        ModelRole.JUDGE,
    )
    assert result.total_calculated_cost == Decimal("0.0015")
