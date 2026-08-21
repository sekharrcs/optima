"""Behavior tests for plan-honoring small-first execution."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from optima.context import (
    ContextPreservationEvidence,
    ContextReductionResult,
    FakeContextReducer,
    RecordingTokenCounter,
    RegexTokenCounter,
)
from optima.cost import CostCalculator, PriceCatalog, PriceCatalogEntry
from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    ContextReductionOutcome,
    ContextSource,
    ExecutionEventCode,
    ExecutionPlan,
    ExecutionStatus,
    ExecutionStepType,
    ModelRole,
)
from optima.domain.quality_contract import (
    OptimizationMode,
    QualityContract,
    QualityProfile,
    RiskTier,
)
from optima.domain.request_binding import RequestBinding, build_request_binding
from optima.domain.request_profile import Complexity, RequestProfile, TaskType
from optima.domain.run import PricingProvenance, RunStatus
from optima.evaluation import (
    DeterministicCheckResult,
    EvaluationEvidence,
    EvaluationRequest,
    FakeEvaluator,
)
from optima.execution import (
    ContextReductionDependencyError,
    ExecutionRequest,
    PlanExecutor,
)
from optima.planner import (
    ContextReducerCapability,
    ModuleConfiguration,
    PlannerInput,
    PlanningFailure,
    select_plan,
)
from optima.providers import (
    FakeProviderResponse,
    ModelProviderRequest,
    ModelProviderResult,
    build_fake_small_provider,
    build_fake_strong_provider,
)


class IncrementingClock:
    """Deterministic monotonic clock requiring no sleeps."""

    def __init__(self) -> None:
        self._value = 0.0

    def now(self) -> float:
        value = self._value
        self._value += 0.001
        return value


class RaisingProvider:
    """Provider test double that raises one configured operational error."""

    provider_name = "raising-provider"
    deployment_name = "raising-deployment"

    def __init__(self, role: ModelRole, error: Exception) -> None:
        self.model_role = role
        self._error = error
        self.calls: list[ModelProviderRequest] = []

    async def generate(self, request: ModelProviderRequest) -> object:
        self.calls.append(request)
        raise self._error


class RaisingEvaluator:
    """Evaluator test double that raises one configured operational error."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls: list[EvaluationRequest] = []

    async def evaluate(
        self,
        request: EvaluationRequest,
        quality_contract: QualityContract,
    ) -> object:
        self.calls.append(request)
        raise self._error


class ConstructedEvaluator:
    """Evaluator that returns unchecked structurally invalid evidence."""

    async def evaluate(
        self,
        request: EvaluationRequest,
        quality_contract: QualityContract,
    ) -> EvaluationResult:
        valid = EvaluationResult(
            evaluator_type="constructed",
            evaluator_valid=True,
            score=1.0,
            threshold=quality_contract.minimum_quality_score,
            mandatory_checks_passed=True,
            passed=True,
            reasons=("Accepted",),
        )
        values = {
            field_name: getattr(valid, field_name)
            for field_name in type(valid).model_fields
        }
        values["reasons"] = ()
        return EvaluationResult.model_construct(**values)


def profile() -> RequestProfile:
    """Build a complete profile that deterministically selects small-first."""
    return RequestProfile(
        task_type=TaskType.SUMMARIZATION,
        complexity=Complexity.LOW,
        input_tokens=100,
        risk_tier=RiskTier.LOW,
        cache_eligible=False,
        has_large_context=False,
    )


def contract() -> QualityContract:
    """Build the shared High Cost Quality Contract."""
    return QualityContract(
        quality_profile=QualityProfile.HIGH,
        minimum_quality_score=0.90,
        optimization_mode=OptimizationMode.COST,
        risk_tier=RiskTier.LOW,
    )


def reduction_context() -> str:
    """Return the original context shared by reduction request fixtures."""
    return (
        "Priya Nair owns incident INC-204.\n"
        "INC-204 affected 37 requests.\n"
        "Unrelated social update for the wider team."
    )


def planner_request_binding(*, context: str = "Incident context") -> RequestBinding:
    """Build the exact current binding supplied to Planner V1."""
    return build_request_binding(
        input_text="Summarize the incident",
        context=context,
        reference_output="Expected summary",
        criteria=("Preserve the outcome",),
        metadata={"scenario": "slice-5"},
        task_type=TaskType.SUMMARIZATION,
        complexity=Complexity.LOW,
    )


def plan() -> ExecutionPlan:
    """Build the shared plan through authoritative Planner V1."""
    result = select_plan(
        PlannerInput(
            request_profile=profile(),
            request_binding=planner_request_binding(),
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


def reduction_plan() -> ExecutionPlan:
    """Build a small-first plan with context reduction selected."""
    reduction_profile = profile().model_copy(
        update={"input_tokens": 4_000, "has_large_context": True}
    )
    result = select_plan(
        PlannerInput(
            request_profile=reduction_profile,
            request_binding=planner_request_binding(context=reduction_context()),
            quality_contract=contract(),
            modules=ModuleConfiguration(
                semantic_cache_enabled=False,
                context_reduction_enabled=True,
                historical_policy_enabled=False,
                foundry_router_comparator_enabled=False,
            ),
            reducer_capability=ContextReducerCapability(
                available=True,
                task_safe=True,
                approved_for_critical_high_risk=False,
            ),
        )
    )
    assert not isinstance(result, PlanningFailure)
    return result


def execution_request() -> ExecutionRequest:
    """Build complete executor input with evaluator-owned measurement inputs."""
    return ExecutionRequest(
        run_id="run-slice-5",
        correlation_id="correlation-slice-5",
        input_text="Summarize the incident",
        context="Incident context",
        reference_output="Expected summary",
        criteria=("Preserve the outcome",),
        metadata={"scenario": "slice-5"},
        quality_contract=contract(),
        request_profile=profile(),
        execution_plan=plan(),
    )


def test_execution_request_rejects_plan_rebound_to_request_or_contract() -> None:
    """Keep planner routing bound to current request and contract facts."""
    request = execution_request()

    with pytest.raises(ValidationError, match="request binding"):
        request.model_copy(
            update={
                "request_profile": request.request_profile.model_copy(
                    update={
                        "task_type": TaskType.GENERAL_REASONING,
                        "complexity": Complexity.HIGH,
                    }
                )
            }
        )
    with pytest.raises(ValidationError, match="Quality Contract"):
        request.model_copy(
            update={
                "quality_contract": request.quality_contract.model_copy(
                    update={
                        "quality_profile": QualityProfile.CRITICAL,
                        "minimum_quality_score": 0.95,
                    }
                )
            }
        )


def reduction_request() -> ExecutionRequest:
    """Build executor input whose authoritative plan selects reduction."""
    original_context = reduction_context()
    reduction_profile = profile().model_copy(
        update={"input_tokens": 4_000, "has_large_context": True}
    )
    return execution_request().model_copy(
        update={
            "context": original_context,
            "request_profile": reduction_profile,
            "execution_plan": reduction_plan(),
        }
    )


def measured_reduction_result(
    *,
    original_context: str | None = None,
    reduced_context: str = "Priya Nair owns incident INC-204.",
) -> ContextReductionResult:
    """Build reduction evidence measured with the runtime counter."""
    counter = RegexTokenCounter()
    original = original_context or reduction_request().context
    assert original is not None
    return ContextReductionResult(
        reduced_context=reduced_context,
        original_token_count=counter.count(original),
        reduced_token_count=counter.count(reduced_context),
        reducer_name="fake-context-reducer",
        method="EXTRACTIVE_TEST",
        token_counter_name=counter.counter_name,
        preservation=ContextPreservationEvidence(
            source_order_preserved=True,
            original_segment_count=3,
            retained_segment_indexes=(0,),
            removed_duplicate_count=0,
            removed_irrelevant_count=2,
            task_terms_used=("incident",),
        ),
    )


def evidence(score: float, *, valid: bool = True) -> EvaluationEvidence:
    """Build evaluator-owned evidence for one configured attempt."""
    return EvaluationEvidence(
        evaluator_type="fake-deterministic",
        evaluator_valid=valid,
        score=score,
        metadata={"source": "test"},
    )


def provider_response(
    output: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cost: Decimal | None = None,
    provenance: PricingProvenance | None = None,
) -> FakeProviderResponse:
    """Build one measured fake provider response."""
    return FakeProviderResponse(
        output_text=output,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        calculated_cost=cost,
        pricing_provenance=provenance,
    )


def cost_calculator(*entries: PriceCatalogEntry) -> CostCalculator:
    """Build deterministic artificial pricing for execution tests."""
    configured_entries = entries or (
        PriceCatalogEntry(
            provider="fake",
            deployment="small",
            input_rate_per_million_tokens=Decimal("2"),
            output_rate_per_million_tokens=Decimal("40"),
        ),
        PriceCatalogEntry(
            provider="fake",
            deployment="strong",
            input_rate_per_million_tokens=Decimal("30"),
            output_rate_per_million_tokens=Decimal("190"),
        ),
    )
    return CostCalculator(
        PriceCatalog(
            version="test-v1",
            currency="TEST",
            entries=configured_entries,
        )
    )


def build_executor(
    *,
    evaluator: object,
    small_provider: object | None = None,
    strong_provider: object | None = None,
    calculator: CostCalculator | None = None,
    context_reducer: object | None = None,
    token_counter: object | None = None,
) -> tuple[PlanExecutor, object, object]:
    """Build fresh dependencies for one isolated execution test."""
    small = small_provider or build_fake_small_provider(
        provider_name="fake",
        deployment_name="small",
        responses=(
            provider_response(
                "small output",
                input_tokens=100,
                output_tokens=20,
            ),
        ),
        clock=IncrementingClock(),
    )
    strong = strong_provider or build_fake_strong_provider(
        provider_name="fake",
        deployment_name="strong",
        responses=(
            provider_response(
                "strong output",
                input_tokens=110,
                output_tokens=30,
            ),
        ),
        clock=IncrementingClock(),
    )
    return (
        PlanExecutor(
            small_provider=small,  # type: ignore[arg-type]
            strong_provider=strong,  # type: ignore[arg-type]
            evaluator=evaluator,  # type: ignore[arg-type]
            cost_calculator=calculator or cost_calculator(),
            context_reducer=context_reducer,  # type: ignore[arg-type]
            token_counter=token_counter,  # type: ignore[arg-type]
            monotonic_clock=IncrementingClock(),
            utc_now=lambda: datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
        ),
        small,
        strong,
    )


def test_reduction_runs_once_before_small_pass_and_preserves_original_evaluation() -> (
    None
):
    """Use measured reduced context for SMALL while evaluating against original."""
    request = reduction_request()
    reduced = measured_reduction_result()
    reducer = FakeContextReducer((reduced,))
    evaluator = FakeEvaluator(responses=(evidence(0.93),))
    executor, small, strong = build_executor(
        evaluator=evaluator,
        context_reducer=reducer,
        token_counter=RegexTokenCounter(),
    )

    result = asyncio.run(executor.execute(request))

    assert len(reducer.calls) == 1
    assert reducer.calls[0].input_text == request.input_text
    assert reducer.calls[0].context == request.context
    assert len(small.calls) == 1  # type: ignore[attr-defined]
    assert small.calls[0].request.input_text == request.input_text  # type: ignore[attr-defined]
    assert small.calls[0].request.context == reduced.reduced_context  # type: ignore[attr-defined]
    assert len(strong.calls) == 0  # type: ignore[attr-defined]
    assert evaluator.calls[0].request.context == request.context
    assert result.steps[0].step_type is ExecutionStepType.CONTEXT_REDUCTION
    assert result.steps[0].status is ExecutionStatus.SUCCEEDED
    reduction = result.steps[0].context_reduction
    assert reduction is not None
    assert reduction.outcome is ContextReductionOutcome.APPLIED
    assert reduction.original_token_count == reduced.original_token_count
    assert reduction.effective_token_count == reduced.reduced_token_count
    assert result.steps[1].context_source is ContextSource.REDUCED


def test_escalation_reuses_same_validated_reduced_context() -> None:
    """Send one reduced context to both model calls after failed SMALL quality."""
    request = reduction_request()
    reduced = measured_reduction_result()
    reducer = FakeContextReducer((reduced,))
    evaluator = FakeEvaluator(responses=(evidence(0.80), evidence(0.95)))
    executor, small, strong = build_executor(
        evaluator=evaluator,
        context_reducer=reducer,
        token_counter=RegexTokenCounter(),
    )

    result = asyncio.run(executor.execute(request))

    assert len(reducer.calls) == 1
    assert small.calls[0].request.context == reduced.reduced_context  # type: ignore[attr-defined]
    assert strong.calls[0].request.context == reduced.reduced_context  # type: ignore[attr-defined]
    assert tuple(call.request.context for call in evaluator.calls) == (
        request.context,
        request.context,
    )
    assert result.escalated is True
    assert [step.step_type for step in result.steps] == [
        ExecutionStepType.CONTEXT_REDUCTION,
        ExecutionStepType.MODEL_CALL,
        ExecutionStepType.QUALITY_EVALUATION,
        ExecutionStepType.ESCALATION,
        ExecutionStepType.MODEL_CALL,
        ExecutionStepType.QUALITY_EVALUATION,
        ExecutionStepType.RETURN,
    ]


def test_keep_original_bypasses_configured_reducer_and_preserves_context() -> None:
    """Do not invoke or report reduction when the selected policy keeps context."""
    reducer = FakeContextReducer((measured_reduction_result(),))
    counter = RecordingTokenCounter(RegexTokenCounter())
    executor, small, _ = build_executor(
        evaluator=FakeEvaluator(responses=(evidence(0.93),)),
        context_reducer=reducer,
        token_counter=counter,
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert reducer.calls == ()
    assert counter.calls == ()
    assert small.calls[0].request.context == execution_request().context  # type: ignore[attr-defined]
    assert all(
        step.step_type is not ExecutionStepType.CONTEXT_REDUCTION
        for step in result.steps
    )


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (RuntimeError("reducer failed"), ExecutionStatus.FAILED),
        (TimeoutError("reducer timed out"), ExecutionStatus.TIMED_OUT),
    ],
)
def test_reducer_operational_fault_uses_original_context_truthfully(
    error: Exception,
    expected_status: ExecutionStatus,
) -> None:
    """Recover optional optimization faults without claiming token reduction."""
    request = reduction_request()
    reducer = FakeContextReducer((error,))
    executor, small, _ = build_executor(
        evaluator=FakeEvaluator(responses=(evidence(0.93),)),
        context_reducer=reducer,
        token_counter=RegexTokenCounter(),
    )

    result = asyncio.run(executor.execute(request))

    reduction_step = result.steps[0]
    reduction = reduction_step.context_reduction
    assert result.status is RunStatus.COMPLETED
    assert reduction_step.status is expected_status
    assert reduction is not None
    assert reduction.outcome is ContextReductionOutcome.FAILED_USING_ORIGINAL
    assert reduction.original_token_count == reduction.effective_token_count
    assert reduction.context_source is ContextSource.ORIGINAL
    assert small.calls[0].request.context == request.context  # type: ignore[attr-defined]
    assert result.steps[1].context_source is ContextSource.ORIGINAL


def test_invalid_reducer_count_evidence_falls_back_to_original() -> None:
    """Independently reject reducer counts that disagree with the runtime counter."""
    request = reduction_request()
    valid = measured_reduction_result()
    inconsistent = valid.model_copy(
        update={"original_token_count": valid.original_token_count + 1}
    )
    reducer = FakeContextReducer((inconsistent,))
    executor, small, _ = build_executor(
        evaluator=FakeEvaluator(responses=(evidence(0.93),)),
        context_reducer=reducer,
        token_counter=RegexTokenCounter(),
    )

    result = asyncio.run(executor.execute(request))

    assert result.steps[0].status is ExecutionStatus.FAILED
    assert result.steps[0].error == "Context reduction ValueError"
    assert small.calls[0].request.context == request.context  # type: ignore[attr-defined]


@pytest.mark.parametrize("invalid_context", ["", None])
def test_reducer_output_contract_violation_falls_back_to_original(
    invalid_context: str | None,
) -> None:
    """Reject empty or non-reducing output after revalidating the reducer result."""
    request = reduction_request()
    assert request.context is not None
    invalid_output = request.context if invalid_context is None else invalid_context
    measured = measured_reduction_result()
    forged_values = {
        field_name: getattr(measured, field_name)
        for field_name in type(measured).model_fields
    }
    forged_values["reduced_context"] = invalid_output
    malicious = ContextReductionResult.model_construct(
        **forged_values,
    )
    reducer = FakeContextReducer((malicious,))
    executor, small, _ = build_executor(
        evaluator=FakeEvaluator(responses=(evidence(0.93),)),
        context_reducer=reducer,
        token_counter=RegexTokenCounter(),
    )

    result = asyncio.run(executor.execute(request))

    assert result.steps[0].status is ExecutionStatus.FAILED
    assert result.steps[0].context_reduction is not None
    assert result.steps[0].context_reduction.outcome is (
        ContextReductionOutcome.FAILED_USING_ORIGINAL
    )
    assert small.calls[0].request.context == request.context  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("reducer", "counter"),
    [
        (None, RegexTokenCounter()),
        (FakeContextReducer((measured_reduction_result(),)), None),
    ],
)
def test_reduction_plan_requires_reducer_and_counter_before_model_calls(
    reducer: object | None,
    counter: object | None,
) -> None:
    """Fail structurally when a selected plan cannot execute its first component."""
    executor, small, strong = build_executor(
        evaluator=FakeEvaluator(responses=(evidence(0.93),)),
        context_reducer=reducer,
        token_counter=counter,
    )

    with pytest.raises(ContextReductionDependencyError, match="requires a configured"):
        asyncio.run(executor.execute(reduction_request()))
    assert len(small.calls) == 0  # type: ignore[attr-defined]
    assert len(strong.calls) == 0  # type: ignore[attr-defined]


def test_small_passes_without_strong_call() -> None:
    """Return the measured small result and avoid fallback after a valid pass."""
    evaluator = FakeEvaluator(responses=(evidence(0.93),))
    executor, small, strong = build_executor(evaluator=evaluator)

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.COMPLETED
    assert result.final_output == "small output"
    assert result.final_evaluation == evaluator.calls[0].result
    assert result.contract_met is True
    assert result.escalated is False
    assert len(small.calls) == 1  # type: ignore[attr-defined]
    assert len(strong.calls) == 0  # type: ignore[attr-defined]
    assert len(evaluator.calls) == 1
    assert result.total_tokens == 120
    assert result.total_calculated_cost == Decimal("0.001")
    assert small.calls[0].result.usage.calculated_cost is None  # type: ignore[attr-defined]
    assert result.model_usages[0].calculated_cost == Decimal("0.001")
    assert result.model_usages[0].pricing_provenance == PricingProvenance(
        catalog_version="test-v1",
        currency="TEST",
    )
    assert result.total_cost_provenance == PricingProvenance(
        catalog_version="test-v1",
        currency="TEST",
    )


def test_small_fails_then_strong_passes_exactly_once() -> None:
    """Escalate once and return the strong result with both call measurements."""
    evaluator = FakeEvaluator(responses=(evidence(0.80), evidence(0.95)))
    executor, small, strong = build_executor(evaluator=evaluator)

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.COMPLETED
    assert result.final_output == "strong output"
    assert result.final_evaluation == evaluator.calls[1].result
    assert result.contract_met is True
    assert result.escalated is True
    assert len(small.calls) == 1  # type: ignore[attr-defined]
    assert len(strong.calls) == 1  # type: ignore[attr-defined]
    assert tuple(call.request.output_text for call in evaluator.calls) == (
        "small output",
        "strong output",
    )
    assert (
        sum(step.step_type is ExecutionStepType.ESCALATION for step in result.steps)
        == 1
    )
    runtime_events = [code for step in result.steps for code in step.event_codes]
    assert runtime_events.count(ExecutionEventCode.ESCALATION_REQUIRED) == 1
    assert runtime_events.count(ExecutionEventCode.ESCALATED_TO_STRONG) == 1
    assert result.total_input_tokens == 210
    assert result.total_output_tokens == 50
    assert result.total_calculated_cost == Decimal("0.010")
    assert {usage.pricing_provenance for usage in result.model_usages} == {
        PricingProvenance(catalog_version="test-v1", currency="TEST")
    }
    assert result.total_cost_provenance == PricingProvenance(
        catalog_version="test-v1",
        currency="TEST",
    )


def test_central_pricing_overwrites_provider_supplied_cost() -> None:
    """Prevent provider cost from competing with centralized calculation."""
    small = build_fake_small_provider(
        provider_name="fake",
        deployment_name="small",
        responses=(
            provider_response(
                "small output",
                input_tokens=100,
                output_tokens=20,
                cost=Decimal("999"),
                provenance=PricingProvenance(
                    catalog_version="provider-v0",
                    currency="WRONG",
                ),
            ),
        ),
        clock=IncrementingClock(),
    )
    executor, _, _ = build_executor(
        evaluator=FakeEvaluator(responses=(evidence(0.93),)),
        small_provider=small,
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert small.calls[0].result.usage.calculated_cost == Decimal("999")
    assert small.calls[0].result.usage.pricing_provenance == PricingProvenance(
        catalog_version="provider-v0",
        currency="WRONG",
    )
    assert result.model_usages[0].calculated_cost == Decimal("0.001")
    assert result.model_usages[0].pricing_provenance == PricingProvenance(
        catalog_version="test-v1",
        currency="TEST",
    )
    assert result.total_calculated_cost == Decimal("0.001")


def test_unknown_pricing_overwrites_provider_cost_with_unavailable() -> None:
    """Keep successful execution cost unavailable when catalog pricing is absent."""
    executor, _, _ = build_executor(
        evaluator=FakeEvaluator(responses=(evidence(0.93),)),
        calculator=cost_calculator(),
        small_provider=build_fake_small_provider(
            provider_name="unpriced",
            deployment_name="small",
            responses=(
                provider_response(
                    "small output",
                    input_tokens=100,
                    output_tokens=20,
                    cost=Decimal("999"),
                    provenance=PricingProvenance(
                        catalog_version="provider-v0",
                        currency="WRONG",
                    ),
                ),
            ),
            clock=IncrementingClock(),
        ),
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.COMPLETED
    assert result.model_usages[0].calculated_cost is None
    assert result.model_usages[0].pricing_provenance is None
    assert result.total_calculated_cost is None
    assert result.total_cost_provenance is None


def test_both_models_validly_fail_returns_final_strong_measurement() -> None:
    """Stop after strong and preserve the final measured contract failure."""
    evaluator = FakeEvaluator(responses=(evidence(0.70), evidence(0.85)))
    executor, small, strong = build_executor(evaluator=evaluator)

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.COMPLETED
    assert result.final_output == "strong output"
    assert result.final_evaluation == evaluator.calls[1].result
    assert result.contract_met is False
    assert result.escalated is True
    assert len(small.calls) == 1  # type: ignore[attr-defined]
    assert len(strong.calls) == 1  # type: ignore[attr-defined]
    assert ExecutionEventCode.FINAL_QUALITY_CONTRACT_NOT_MET in {
        code for step in result.steps for code in step.event_codes
    }


def test_failed_mandatory_check_escalates_without_false_threshold_event() -> None:
    """Expose the evaluator reason without claiming the score missed threshold."""
    evaluator = FakeEvaluator(
        responses=(
            EvaluationEvidence(
                evaluator_type="fake-deterministic",
                evaluator_valid=True,
                score=0.95,
                mandatory_checks=(
                    DeterministicCheckResult(check_id="schema", passed=False),
                ),
            ),
            evidence(0.96),
        )
    )
    executor, _, strong = build_executor(evaluator=evaluator)

    result = asyncio.run(executor.execute(execution_request()))

    first_evaluation_step = next(
        step
        for step in result.steps
        if step.step_type is ExecutionStepType.QUALITY_EVALUATION
    )
    assert result.contract_met is True
    assert len(strong.calls) == 1  # type: ignore[attr-defined]
    assert ExecutionEventCode.QUALITY_THRESHOLD_NOT_MET not in (
        first_evaluation_step.event_codes
    )
    assert "MANDATORY_CHECK_FAILED:schema" in result.evaluations[0].reasons


def test_trace_and_evaluator_inputs_preserve_truthful_request_facts() -> None:
    """Align sequence, run, roles, thresholds, planner reasons, and source inputs."""
    evaluator = FakeEvaluator(responses=(evidence(0.80), evidence(0.95)))
    executor, _, _ = build_executor(evaluator=evaluator)

    result = asyncio.run(executor.execute(execution_request()))

    assert tuple(step.sequence for step in result.steps) == tuple(
        range(len(result.steps))
    )
    assert tuple(usage.run_id for usage in result.model_usages) == (
        result.run_id,
        result.run_id,
    )
    assert tuple(usage.model_role for usage in result.model_usages) == (
        ModelRole.SMALL,
        ModelRole.STRONG,
    )
    assert all(
        evaluation.threshold == result.quality_contract.minimum_quality_score
        for evaluation in result.evaluations
    )
    assert evaluator.calls[0].request.input_text == "Summarize the incident"
    assert evaluator.calls[0].request.context == "Incident context"
    assert evaluator.calls[0].request.reference_output == "Expected summary"
    assert evaluator.calls[0].request.criteria == ("Preserve the outcome",)
    assert evaluator.calls[0].request.metadata["model_role"] == "SMALL"
    assert result.execution_plan.reason_codes == plan().reason_codes


def test_invalid_small_evidence_escalates_but_invalid_final_evidence_fails_closed() -> (
    None
):
    """Never claim compliance or return output from invalid final evidence."""
    evaluator = FakeEvaluator(
        responses=(evidence(1.0, valid=False), evidence(1.0, valid=False))
    )
    executor, small, strong = build_executor(evaluator=evaluator)

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.FAILED
    assert result.final_output is None
    assert result.final_evaluation == evaluator.calls[1].result
    assert result.contract_met is None
    assert result.escalated is True
    assert len(small.calls) == 1  # type: ignore[attr-defined]
    assert len(strong.calls) == 1  # type: ignore[attr-defined]


def test_structurally_invalid_evaluator_result_fails_closed() -> None:
    """Normalize evaluator values inside the evaluation failure boundary."""
    executor, _, _ = build_executor(evaluator=ConstructedEvaluator())

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.FAILED
    assert result.steps[-1].step_type is ExecutionStepType.QUALITY_EVALUATION
    assert result.steps[-1].status is ExecutionStatus.FAILED
    assert result.final_output is None
    assert result.contract_met is None


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_step_status"),
    [
        (RuntimeError("provider failed"), RunStatus.FAILED, ExecutionStatus.FAILED),
        (
            TimeoutError("provider timed out"),
            RunStatus.TIMED_OUT,
            ExecutionStatus.TIMED_OUT,
        ),
    ],
)
def test_small_provider_operational_failure_does_not_escalate_or_fabricate(
    error: Exception,
    expected_status: RunStatus,
    expected_step_status: ExecutionStatus,
) -> None:
    """Stop on provider failure without output, usage, evaluation, or fallback."""
    evaluator = FakeEvaluator(responses=(evidence(0.95),))
    failing_small = RaisingProvider(ModelRole.SMALL, error)
    executor, _, strong = build_executor(
        evaluator=evaluator,
        small_provider=failing_small,
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is expected_status
    assert result.steps[-1].status is expected_step_status
    assert result.final_output is None
    assert result.model_usages == ()
    assert result.evaluations == ()
    assert result.contract_met is None
    assert result.escalated is False
    assert len(strong.calls) == 0  # type: ignore[attr-defined]
    assert len(evaluator.calls) == 0


@pytest.mark.parametrize(
    "usage_update",
    [
        {"run_id": "other-run"},
        {"provider": "other-provider"},
    ],
    ids=["wrong-run-id", "wrong-provider-identity"],
)
def test_misaligned_provider_usage_becomes_domain_valid_failure(
    usage_update: dict[str, object],
) -> None:
    """Reject misaligned SMALL usage without evaluation or fallback."""
    valid_small = build_fake_small_provider(
        provider_name="fake",
        deployment_name="small",
        responses=(
            provider_response(
                "small output",
                input_tokens=100,
                output_tokens=20,
            ),
        ),
        clock=IncrementingClock(),
    )

    class MisalignedProvider:
        provider_name = "fake"
        deployment_name = "small"
        model_role = ModelRole.SMALL

        async def generate(
            self,
            request: ModelProviderRequest,
        ) -> ModelProviderResult:
            result = await valid_small.generate(request)
            return result.model_copy(
                update={"usage": result.usage.model_copy(update=usage_update)}
            )

    evaluator = FakeEvaluator(responses=(evidence(0.95),))
    executor, _, strong = build_executor(
        evaluator=evaluator,
        small_provider=MisalignedProvider(),
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.FAILED
    assert result.final_output is None
    assert result.contract_met is None
    assert result.model_usages == ()
    assert result.evaluations == ()
    assert result.steps[-1].status is ExecutionStatus.FAILED
    assert len(strong.calls) == 0  # type: ignore[attr-defined]
    assert len(evaluator.calls) == 0


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_step_status"),
    [
        (RuntimeError("evaluator failed"), RunStatus.FAILED, ExecutionStatus.FAILED),
        (
            TimeoutError("evaluator timed out"),
            RunStatus.TIMED_OUT,
            ExecutionStatus.TIMED_OUT,
        ),
    ],
)
def test_small_evaluator_operational_failure_does_not_escalate_or_invent_evidence(
    error: Exception,
    expected_status: RunStatus,
    expected_step_status: ExecutionStatus,
) -> None:
    """Preserve small usage but stop before fallback when measurement fails."""
    evaluator = RaisingEvaluator(error)
    executor, small, strong = build_executor(evaluator=evaluator)

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is expected_status
    assert result.steps[-1].status is expected_step_status
    assert result.final_output is None
    assert len(result.model_usages) == 1
    assert result.evaluations == ()
    assert result.contract_met is None
    assert result.escalated is False
    assert len(small.calls) == 1  # type: ignore[attr-defined]
    assert len(strong.calls) == 0  # type: ignore[attr-defined]


def test_mismatched_evaluation_threshold_becomes_domain_valid_failure() -> None:
    """Reject evaluator results measured against a different contract threshold."""
    configured = FakeEvaluator(responses=(evidence(0.95),))

    class WrongThresholdEvaluator:
        async def evaluate(
            self,
            request: EvaluationRequest,
            quality_contract: QualityContract,
        ) -> object:
            result = await configured.evaluate(request, quality_contract)
            return result.model_copy(update={"threshold": 0.80})

    executor, small, strong = build_executor(evaluator=WrongThresholdEvaluator())

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.FAILED
    assert result.final_output is None
    assert result.contract_met is None
    assert result.evaluations == ()
    assert len(result.model_usages) == 1
    assert result.steps[-1].status is ExecutionStatus.FAILED
    assert len(small.calls) == 1  # type: ignore[attr-defined]
    assert len(strong.calls) == 0  # type: ignore[attr-defined]


def test_strong_provider_failure_preserves_small_facts_and_incomplete_totals() -> None:
    """Record one escalation without fabricating failed strong call usage."""
    evaluator = FakeEvaluator(responses=(evidence(0.80),))
    failing_strong = RaisingProvider(ModelRole.STRONG, RuntimeError("failed"))
    executor, small, _ = build_executor(
        evaluator=evaluator,
        strong_provider=failing_strong,
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.FAILED
    assert result.escalated is True
    assert len(small.calls) == 1  # type: ignore[attr-defined]
    assert len(failing_strong.calls) == 1
    assert len(result.model_usages) == 1
    assert len(result.evaluations) == 1
    assert result.total_tokens is None
    assert result.total_calculated_cost is None


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_step_status"),
    [
        (RuntimeError("evaluator failed"), RunStatus.FAILED, ExecutionStatus.FAILED),
        (
            TimeoutError("evaluator timed out"),
            RunStatus.TIMED_OUT,
            ExecutionStatus.TIMED_OUT,
        ),
    ],
)
def test_strong_evaluator_failure_preserves_both_model_usages_without_output(
    error: Exception,
    expected_status: RunStatus,
    expected_step_status: ExecutionStatus,
) -> None:
    """Fail closed after escalation while retaining every completed call fact."""
    first_evaluator = FakeEvaluator(responses=(evidence(0.80),))

    class SecondCallRaisingEvaluator:
        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(
            self,
            request: EvaluationRequest,
            quality_contract: QualityContract,
        ) -> object:
            self.calls += 1
            if self.calls == 1:
                return await first_evaluator.evaluate(request, quality_contract)
            raise error

    executor, small, strong = build_executor(evaluator=SecondCallRaisingEvaluator())

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is expected_status
    assert result.steps[-1].status is expected_step_status
    assert result.final_output is None
    assert result.contract_met is None
    assert result.escalated is True
    assert len(small.calls) == 1  # type: ignore[attr-defined]
    assert len(strong.calls) == 1  # type: ignore[attr-defined]
    assert len(result.model_usages) == 2
    assert len(result.evaluations) == 1
