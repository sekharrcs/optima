"""Focused behavior tests for plan-honoring strong-direct execution."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from optima.context import (
    ContextPreservationEvidence,
    ContextReductionResult,
    FakeContextReducer,
    RecordingTokenCounter,
    RegexTokenCounter,
)
from optima.cost import CostCalculator, PriceCatalog, PriceCatalogEntry
from optima.domain.execution import (
    ContextReductionOutcome,
    ContextSource,
    ExecutionEventCode,
    ExecutionPlan,
    ExecutionStatus,
    ExecutionStepType,
    ModelPolicy,
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
from optima.evaluation import EvaluationEvidence, EvaluationRequest, FakeEvaluator
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
        """Return one monotonically increasing test timestamp."""
        value = self._value
        self._value += 0.001
        return value


class RaisingProvider:
    """STRONG provider double that raises one configured operational error."""

    provider_name = "raising-provider"
    deployment_name = "raising-deployment"
    model_role = ModelRole.STRONG

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls: list[ModelProviderRequest] = []

    async def generate(self, request: ModelProviderRequest) -> object:
        self.calls.append(request)
        raise self._error


class RaisingEvaluator:
    """Evaluator double that raises one configured operational error."""

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


def request_profile() -> RequestProfile:
    """Build a HIGH-complexity profile that must select STRONG_DIRECT."""
    return RequestProfile(
        task_type=TaskType.GENERAL_REASONING,
        complexity=Complexity.HIGH,
        input_tokens=500,
        risk_tier=RiskTier.LOW,
        cache_eligible=False,
        has_large_context=False,
    )


def quality_contract() -> QualityContract:
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


def planner_request_binding(
    *, context: str = "Original architecture context"
) -> RequestBinding:
    """Build the exact current binding supplied to Planner V1."""
    return build_request_binding(
        input_text="Assess the architecture tradeoffs",
        context=context,
        reference_output=None,
        criteria=(),
        metadata={},
        task_type=TaskType.GENERAL_REASONING,
        complexity=Complexity.HIGH,
    )


def strong_direct_plan() -> ExecutionPlan:
    """Build the authoritative Planner V1 strong-direct plan."""
    result = select_plan(
        PlannerInput(
            request_profile=request_profile(),
            request_binding=planner_request_binding(),
            quality_contract=quality_contract(),
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
    assert result.model_policy is ModelPolicy.STRONG_DIRECT
    return result


def reduction_plan() -> ExecutionPlan:
    """Build a strong-direct plan with context reduction selected."""
    reduction_profile = request_profile().model_copy(
        update={"input_tokens": 4_000, "has_large_context": True}
    )
    result = select_plan(
        PlannerInput(
            request_profile=reduction_profile,
            request_binding=planner_request_binding(context=reduction_context()),
            quality_contract=quality_contract(),
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
    assert result.model_policy is ModelPolicy.STRONG_DIRECT
    return result


def execution_request() -> ExecutionRequest:
    """Build one complete direct-plan execution request."""
    return ExecutionRequest(
        run_id="run-strong-direct",
        correlation_id="correlation-strong-direct",
        input_text="Assess the architecture tradeoffs",
        context="Original architecture context",
        quality_contract=quality_contract(),
        request_profile=request_profile(),
        execution_plan=strong_direct_plan(),
    )


def reduction_request() -> ExecutionRequest:
    """Build a direct request whose authoritative plan selects reduction."""
    original_context = reduction_context()
    reduction_profile = request_profile().model_copy(
        update={"input_tokens": 4_000, "has_large_context": True}
    )
    return execution_request().model_copy(
        update={
            "context": original_context,
            "request_profile": reduction_profile,
            "execution_plan": reduction_plan(),
        }
    )


def measured_reduction_result() -> ContextReductionResult:
    """Build reduction evidence measured with the runtime token counter."""
    request = reduction_request()
    assert request.context is not None
    reduced_context = "Priya Nair owns incident INC-204."
    counter = RegexTokenCounter()
    return ContextReductionResult(
        reduced_context=reduced_context,
        original_token_count=counter.count(request.context),
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


def provider_response(
    *,
    provider_cost: Decimal | None = None,
    provider_provenance: PricingProvenance | None = None,
) -> FakeProviderResponse:
    """Build one measured direct STRONG provider response."""
    return FakeProviderResponse(
        output_text="strong direct output",
        input_tokens=120,
        output_tokens=30,
        calculated_cost=provider_cost,
        pricing_provenance=provider_provenance,
    )


def cost_calculator(*entries: PriceCatalogEntry) -> CostCalculator:
    """Build deterministic centralized pricing for execution tests."""
    configured_entries = entries or (
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
    evidence: EvaluationEvidence | None = None,
    *,
    evaluator: object | None = None,
    small_provider: object | None = None,
    strong_provider: object | None = None,
    calculator: CostCalculator | None = None,
    context_reducer: object | None = None,
    token_counter: object | None = None,
) -> tuple[PlanExecutor, object, object, object]:
    """Build isolated providers, evaluator, pricing, and executor."""
    small = small_provider or build_fake_small_provider(
        provider_name="fake",
        deployment_name="small",
        responses=(
            FakeProviderResponse(
                output_text="unused small output",
                input_tokens=50,
                output_tokens=10,
            ),
        ),
        clock=IncrementingClock(),
    )
    strong = strong_provider or build_fake_strong_provider(
        provider_name="fake",
        deployment_name="strong",
        responses=(provider_response(),),
        clock=IncrementingClock(),
    )
    configured_evaluator = evaluator
    if configured_evaluator is None:
        if evidence is None:
            raise ValueError("evidence is required when evaluator is not configured")
        configured_evaluator = FakeEvaluator(responses=(evidence,))
    executor = PlanExecutor(
        small_provider=small,  # type: ignore[arg-type]
        strong_provider=strong,  # type: ignore[arg-type]
        evaluator=configured_evaluator,  # type: ignore[arg-type]
        cost_calculator=calculator or cost_calculator(),
        context_reducer=context_reducer,  # type: ignore[arg-type]
        token_counter=token_counter,  # type: ignore[arg-type]
        monotonic_clock=IncrementingClock(),
        utc_now=lambda: datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )
    return executor, small, strong, configured_evaluator


def evaluation_evidence(score: float, *, valid: bool = True) -> EvaluationEvidence:
    """Build deterministic evaluator evidence for one direct attempt."""
    return EvaluationEvidence(
        evaluator_type="fake-deterministic",
        evaluator_valid=valid,
        score=score,
    )


def test_keep_original_strong_direct_passes_with_one_measured_attempt() -> None:
    """Call only STRONG once, evaluate once, and return measured pass evidence."""
    executor, small, strong, evaluator = build_executor(evaluation_evidence(0.94))
    request = execution_request()

    result = asyncio.run(executor.execute(request))

    assert result.status is RunStatus.COMPLETED
    assert result.final_output == "strong direct output"
    assert result.contract_met is True
    assert result.escalated is False
    assert len(small.calls) == 0  # type: ignore[attr-defined]
    assert len(strong.calls) == 1  # type: ignore[attr-defined]
    assert strong.calls[0].request.context == request.context  # type: ignore[attr-defined]
    assert len(evaluator.calls) == 1  # type: ignore[attr-defined]
    assert evaluator.calls[0].request.metadata["model_role"] == "STRONG"  # type: ignore[attr-defined]
    assert [step.step_type for step in result.steps] == [
        ExecutionStepType.MODEL_CALL,
        ExecutionStepType.QUALITY_EVALUATION,
        ExecutionStepType.RETURN,
    ]
    assert result.steps[0].context_source is ContextSource.ORIGINAL
    assert result.model_usages[0].model_role is ModelRole.STRONG
    assert result.total_input_tokens == 120
    assert result.total_output_tokens == 30
    assert result.total_tokens == 150
    assert result.total_calculated_cost == Decimal("0.0093")
    assert result.model_usages[0].pricing_provenance == PricingProvenance(
        catalog_version="test-v1",
        currency="TEST",
    )
    assert result.total_cost_provenance == PricingProvenance(
        catalog_version="test-v1",
        currency="TEST",
    )
    assert (
        sum(
            step.step_type is ExecutionStepType.MODEL_CALL
            and step.facts["model_role"] == ModelRole.STRONG.value
            for step in result.steps
        )
        == 1
    )
    assert (
        sum(
            step.step_type is ExecutionStepType.MODEL_CALL
            and step.facts["model_role"] == ModelRole.SMALL.value
            for step in result.steps
        )
        == 0
    )
    assert (
        sum(step.step_type is ExecutionStepType.ESCALATION for step in result.steps)
        == 0
    )
    assert all(
        step.facts["model_role"] == ModelRole.STRONG.value
        for step in result.steps
        if step.step_type
        in {ExecutionStepType.QUALITY_EVALUATION, ExecutionStepType.RETURN}
    )
    runtime_events = [code for step in result.steps for code in step.event_codes]
    assert runtime_events.count(ExecutionEventCode.ESCALATION_REQUIRED) == 0
    assert runtime_events.count(ExecutionEventCode.ESCALATED_TO_STRONG) == 0


def test_strong_direct_valid_quality_failure_returns_completed_false() -> None:
    """Return the final STRONG output with a measured unmet contract."""
    executor, small, strong, evaluator = build_executor(evaluation_evidence(0.80))

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.COMPLETED
    assert result.final_output == "strong direct output"
    assert result.contract_met is False
    assert result.escalated is False
    assert len(small.calls) == 0  # type: ignore[attr-defined]
    assert len(strong.calls) == 1  # type: ignore[attr-defined]
    assert len(evaluator.calls) == 1  # type: ignore[attr-defined]
    assert ExecutionEventCode.FINAL_QUALITY_CONTRACT_NOT_MET in {
        code for step in result.steps for code in step.event_codes
    }


def test_strong_direct_invalid_final_evidence_fails_closed() -> None:
    """Keep output and contract status unavailable when final evidence is invalid."""
    executor, small, strong, evaluator = build_executor(
        evaluation_evidence(1.0, valid=False)
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.FAILED
    assert result.final_output is None
    assert result.contract_met is None
    assert result.final_evaluation == evaluator.calls[0].result  # type: ignore[attr-defined]
    assert result.escalated is False
    assert len(small.calls) == 0  # type: ignore[attr-defined]
    assert len(strong.calls) == 1  # type: ignore[attr-defined]
    assert len(evaluator.calls) == 1  # type: ignore[attr-defined]
    assert result.steps[-1].step_type is ExecutionStepType.RETURN
    assert result.steps[-1].status is ExecutionStatus.FAILED


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
def test_strong_provider_operational_failure_does_not_fabricate_evidence(
    error: Exception,
    expected_status: RunStatus,
    expected_step_status: ExecutionStatus,
) -> None:
    """Stop after one STRONG attempt without output, usage, or evaluation facts."""
    evaluator = FakeEvaluator(responses=(evaluation_evidence(0.95),))
    provider = RaisingProvider(error)
    executor, small, _, _ = build_executor(
        evaluator=evaluator,
        strong_provider=provider,
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is expected_status
    assert result.steps[-1].status is expected_step_status
    assert result.final_output is None
    assert result.contract_met is None
    assert result.model_usages == ()
    assert result.evaluations == ()
    assert result.final_evaluation is None
    assert result.total_input_tokens is None
    assert result.total_output_tokens is None
    assert result.total_tokens is None
    assert result.total_calculated_cost is None
    assert result.total_cost_provenance is None
    assert len(provider.calls) == 1
    assert len(small.calls) == 0  # type: ignore[attr-defined]
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
def test_strong_evaluator_operational_failure_retains_completed_usage(
    error: Exception,
    expected_status: RunStatus,
    expected_step_status: ExecutionStatus,
) -> None:
    """Fail closed after evaluation interruption while retaining STRONG usage."""
    evaluator = RaisingEvaluator(error)
    executor, small, strong, _ = build_executor(evaluator=evaluator)

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is expected_status
    assert result.steps[-1].status is expected_step_status
    assert result.final_output is None
    assert result.contract_met is None
    assert len(result.model_usages) == 1
    assert result.evaluations == ()
    assert result.final_evaluation is None
    assert result.total_input_tokens == 120
    assert result.total_output_tokens == 30
    assert result.total_tokens == 150
    assert result.total_calculated_cost == Decimal("0.0093")
    assert len(small.calls) == 0  # type: ignore[attr-defined]
    assert len(strong.calls) == 1  # type: ignore[attr-defined]
    assert len(evaluator.calls) == 1


def test_wrong_evaluator_threshold_becomes_domain_valid_failure() -> None:
    """Reject quality evidence measured against a different threshold."""
    configured = FakeEvaluator(responses=(evaluation_evidence(0.95),))

    class WrongThresholdEvaluator:
        async def evaluate(
            self,
            request: EvaluationRequest,
            contract: QualityContract,
        ) -> object:
            result = await configured.evaluate(request, contract)
            return result.model_copy(update={"threshold": 0.80})

    executor, small, strong, _ = build_executor(evaluator=WrongThresholdEvaluator())

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.FAILED
    assert result.final_output is None
    assert result.contract_met is None
    assert len(result.model_usages) == 1
    assert result.evaluations == ()
    assert result.final_evaluation is None
    assert result.total_tokens == 150
    assert len(small.calls) == 0  # type: ignore[attr-defined]
    assert len(strong.calls) == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "usage_update",
    [
        {"run_id": "other-run"},
        {"model_role": ModelRole.SMALL},
        {"provider": "other-provider"},
        {"deployment": "other-deployment"},
    ],
    ids=[
        "wrong-run-id",
        "wrong-model-role",
        "wrong-provider-identity",
        "wrong-deployment-identity",
    ],
)
def test_misaligned_strong_provider_usage_becomes_domain_valid_failure(
    usage_update: dict[str, object],
) -> None:
    """Reject provider usage not aligned to its request or dependency identity."""
    valid_strong = build_fake_strong_provider(
        provider_name="fake",
        deployment_name="strong",
        responses=(provider_response(),),
        clock=IncrementingClock(),
    )

    class MisalignedStrongProvider:
        provider_name = "fake"
        deployment_name = "strong"
        model_role = ModelRole.STRONG

        async def generate(
            self,
            request: ModelProviderRequest,
        ) -> ModelProviderResult:
            result = await valid_strong.generate(request)
            return result.model_copy(
                update={"usage": result.usage.model_copy(update=usage_update)}
            )

    evaluator = FakeEvaluator(responses=(evaluation_evidence(0.95),))
    executor, small, _, _ = build_executor(
        evaluator=evaluator,
        strong_provider=MisalignedStrongProvider(),
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.FAILED
    assert result.final_output is None
    assert result.contract_met is None
    assert result.model_usages == ()
    assert result.evaluations == ()
    assert result.total_tokens is None
    assert result.total_calculated_cost is None
    assert len(valid_strong.calls) == 1
    assert len(small.calls) == 0  # type: ignore[attr-defined]
    assert len(evaluator.calls) == 0


def test_central_pricing_overrides_provider_supplied_cost() -> None:
    """Use catalog pricing instead of provider-authored monetary claims."""
    provider_provenance = PricingProvenance(
        catalog_version="provider-v0",
        currency="WRONG",
    )
    strong = build_fake_strong_provider(
        provider_name="fake",
        deployment_name="strong",
        responses=(
            provider_response(
                provider_cost=Decimal("999"),
                provider_provenance=provider_provenance,
            ),
        ),
        clock=IncrementingClock(),
    )
    executor, _, _, _ = build_executor(
        evaluation_evidence(0.95),
        strong_provider=strong,
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert strong.calls[0].result.usage.calculated_cost == Decimal("999")
    assert strong.calls[0].result.usage.pricing_provenance == provider_provenance
    assert result.model_usages[0].calculated_cost == Decimal("0.0093")
    assert result.model_usages[0].pricing_provenance == PricingProvenance(
        catalog_version="test-v1",
        currency="TEST",
    )
    assert result.total_calculated_cost == Decimal("0.0093")


def test_unknown_pricing_overrides_provider_cost_with_unavailable() -> None:
    """Keep cost unavailable when no central catalog entry matches STRONG."""
    strong = build_fake_strong_provider(
        provider_name="unpriced",
        deployment_name="strong",
        responses=(
            provider_response(
                provider_cost=Decimal("999"),
                provider_provenance=PricingProvenance(
                    catalog_version="provider-v0",
                    currency="WRONG",
                ),
            ),
        ),
        clock=IncrementingClock(),
    )
    executor, _, _, _ = build_executor(
        evaluation_evidence(0.95),
        strong_provider=strong,
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert result.status is RunStatus.COMPLETED
    assert result.total_tokens == 150
    assert result.model_usages[0].calculated_cost is None
    assert result.model_usages[0].pricing_provenance is None
    assert result.total_calculated_cost is None
    assert result.total_cost_provenance is None


def test_keep_original_bypasses_configured_reducer() -> None:
    """Do not invoke reduction dependencies when the plan keeps original context."""
    reducer = FakeContextReducer((measured_reduction_result(),))
    counter = RecordingTokenCounter(RegexTokenCounter())
    executor, _, strong, _ = build_executor(
        evaluation_evidence(0.95),
        context_reducer=reducer,
        token_counter=counter,
    )

    result = asyncio.run(executor.execute(execution_request()))

    assert reducer.calls == ()
    assert counter.calls == ()
    assert strong.calls[0].request.context == execution_request().context  # type: ignore[attr-defined]
    assert all(
        step.step_type is not ExecutionStepType.CONTEXT_REDUCTION
        for step in result.steps
    )


def test_successful_reduction_uses_reduced_model_context_and_original_evaluation() -> (
    None
):
    """Validate reduction once while evaluating STRONG against original context."""
    request = reduction_request()
    reduced = measured_reduction_result()
    reducer = FakeContextReducer((reduced,))
    counter = RecordingTokenCounter(RegexTokenCounter())
    evaluator = FakeEvaluator(responses=(evaluation_evidence(0.95),))
    executor, small, strong, _ = build_executor(
        evaluator=evaluator,
        context_reducer=reducer,
        token_counter=counter,
    )

    result = asyncio.run(executor.execute(request))

    assert len(reducer.calls) == 1
    assert counter.calls == (request.context, reduced.reduced_context)
    assert len(small.calls) == 0  # type: ignore[attr-defined]
    assert len(strong.calls) == 1  # type: ignore[attr-defined]
    assert strong.calls[0].request.context == reduced.reduced_context  # type: ignore[attr-defined]
    assert evaluator.calls[0].request.context == request.context
    assert result.steps[0].status is ExecutionStatus.SUCCEEDED
    assert result.steps[0].context_reduction is not None
    assert result.steps[0].context_reduction.outcome is ContextReductionOutcome.APPLIED
    assert result.steps[1].context_source is ContextSource.REDUCED


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (RuntimeError("reducer failed"), ExecutionStatus.FAILED),
        (TimeoutError("reducer timed out"), ExecutionStatus.TIMED_OUT),
    ],
)
def test_reduction_fault_falls_back_to_original_and_calls_strong_once(
    error: Exception,
    expected_status: ExecutionStatus,
) -> None:
    """Recover optional reduction faults without changing direct model policy."""
    request = reduction_request()
    reducer = FakeContextReducer((error,))
    executor, small, strong, _ = build_executor(
        evaluation_evidence(0.95),
        context_reducer=reducer,
        token_counter=RegexTokenCounter(),
    )

    result = asyncio.run(executor.execute(request))

    assert result.status is RunStatus.COMPLETED
    assert result.steps[0].status is expected_status
    assert result.steps[0].context_reduction is not None
    assert result.steps[0].context_reduction.outcome is (
        ContextReductionOutcome.FAILED_USING_ORIGINAL
    )
    assert len(small.calls) == 0  # type: ignore[attr-defined]
    assert len(strong.calls) == 1  # type: ignore[attr-defined]
    assert strong.calls[0].request.context == request.context  # type: ignore[attr-defined]
    assert result.steps[1].context_source is ContextSource.ORIGINAL


@pytest.mark.parametrize(
    ("reducer", "counter"),
    [
        (None, RegexTokenCounter()),
        (FakeContextReducer((measured_reduction_result(),)), None),
    ],
)
def test_reduction_plan_requires_dependencies_before_any_model_call(
    reducer: object | None,
    counter: object | None,
) -> None:
    """Fail structurally before STRONG when selected reduction cannot execute."""
    executor, small, strong, evaluator = build_executor(
        evaluation_evidence(0.95),
        context_reducer=reducer,
        token_counter=counter,
    )

    with pytest.raises(ContextReductionDependencyError, match="requires a configured"):
        asyncio.run(executor.execute(reduction_request()))
    assert len(small.calls) == 0  # type: ignore[attr-defined]
    assert len(strong.calls) == 0  # type: ignore[attr-defined]
    assert len(evaluator.calls) == 0  # type: ignore[attr-defined]
