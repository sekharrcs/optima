"""Tests for measured baseline-versus-OPTIMA comparisons."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import JsonValue, ValidationError

from optima.comparison import (
    BaselineComparisonRequest,
    BaselineComparisonService,
    BenchmarkCaseIdentity,
    ComparableRun,
    ComparisonArm,
    ExecutionMetrics,
)
from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    ExecutionEventCode,
    ExecutionPlan,
    ExecutionStatus,
    ExecutionStep,
    ExecutionStepType,
    ModelPolicy,
    ModelRole,
    PlannerDecisionEvidence,
    PlannerModuleStates,
    PlannerReasonCode,
)
from optima.domain.quality_contract import (
    OptimizationMode,
    QualityContract,
    QualityProfile,
    RiskTier,
    build_quality_contract,
)
from optima.domain.request_binding import RequestBinding, build_request_binding
from optima.domain.request_profile import Complexity, RequestProfile, TaskType
from optima.domain.run import ModelUsage, PricingProvenance, RunResult, RunStatus
from optima.planner.policies import effective_risk_tier


def quality_contract(
    *,
    optimization_mode: OptimizationMode = OptimizationMode.COST,
) -> QualityContract:
    """Build the shared comparison Quality Contract."""
    return build_quality_contract(
        quality_profile=QualityProfile.HIGH,
        optimization_mode=optimization_mode,
        risk_tier=RiskTier.MEDIUM,
    )


def request_profile(**updates: object) -> RequestProfile:
    """Build the shared comparison Request Profile."""
    values: dict[str, object] = {
        "task_type": TaskType.LOG_ANALYSIS,
        "complexity": Complexity.MEDIUM,
        "input_tokens": 800,
        "risk_tier": RiskTier.MEDIUM,
        "cache_eligible": False,
        "has_large_context": False,
    }
    values.update(updates)
    return RequestProfile.model_validate(values)


def request_binding(profile: RequestProfile | None = None) -> RequestBinding:
    """Build the shared comparison request identity."""
    bound_profile = profile or request_profile()
    return build_request_binding(
        input_text="Analyze the benchmark logs",
        context=None,
        reference_output=None,
        criteria=(),
        metadata={},
        task_type=bound_profile.task_type,
        complexity=bound_profile.complexity,
    )


def execution_plan(
    *,
    contract: QualityContract | None = None,
    profile: RequestProfile | None = None,
) -> ExecutionPlan:
    """Build a valid small-first plan for measured test runs."""
    bound_contract = contract or quality_contract()
    bound_profile = profile or request_profile()
    mode_reason = {
        OptimizationMode.COST: PlannerReasonCode.OPTIMIZATION_MODE_COST,
        OptimizationMode.BALANCED: PlannerReasonCode.OPTIMIZATION_MODE_BALANCED,
        OptimizationMode.QUALITY: PlannerReasonCode.OPTIMIZATION_MODE_QUALITY,
    }[bound_contract.optimization_mode]
    return ExecutionPlan(
        cache_policy=CachePolicy.SKIP,
        context_policy=ContextPolicy.KEEP_ORIGINAL,
        model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
        initial_model_role=ModelRole.SMALL,
        verification_required=True,
        escalation_model_role=ModelRole.STRONG,
        optimization_mode=bound_contract.optimization_mode,
        quality_profile=bound_contract.quality_profile,
        reason_codes=(
            mode_reason,
            PlannerReasonCode.SMALL_FIRST_SELECTED,
        ),
        human_readable_name="Small -> Verify -> Escalate if needed",
        decision_evidence=PlannerDecisionEvidence(
            profile_risk_tier=bound_profile.risk_tier,
            contract_risk_tier=bound_contract.risk_tier,
            effective_risk_tier=effective_risk_tier(
                bound_profile.risk_tier,
                bound_contract.risk_tier,
            ),
            module_states=PlannerModuleStates(
                semantic_cache_enabled=False,
                context_reduction_enabled=False,
                historical_policy_enabled=False,
                foundry_router_comparator_enabled=False,
            ),
            cache_candidate_assessed=False,
            base_model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            final_model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
        ),
        request_binding=request_binding(bound_profile),
    )


def evaluation(
    *,
    score: float = 0.95,
    evaluator_type: str = "deterministic",
    evaluator_valid: bool = True,
    passed: bool = True,
    metadata: dict[str, JsonValue] | None = None,
) -> EvaluationResult:
    """Build final quality evidence for a measured run."""
    return EvaluationResult(
        evaluator_type=evaluator_type,
        evaluator_valid=evaluator_valid,
        score=score,
        threshold=0.90,
        mandatory_checks_passed=True,
        passed=passed,
        reasons=("Measured benchmark quality",),
        metadata=metadata or {},
    )


def step(
    sequence: int,
    step_type: ExecutionStepType,
    *,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    model_role: ModelRole = ModelRole.SMALL,
    contract_met: bool = True,
    error: str = "Execution interrupted",
    request_id: str = "provider-request-1",
    evaluation_result: EvaluationResult | None = None,
) -> ExecutionStep:
    """Build one execution trace fact."""
    facts: dict[str, JsonValue]
    event_codes: tuple[ExecutionEventCode, ...]
    if step_type is ExecutionStepType.ESCALATION:
        facts = {
            "from_model_role": ModelRole.SMALL.value,
            "to_model_role": ModelRole.STRONG.value,
        }
        event_codes = (
            ExecutionEventCode.ESCALATION_REQUIRED,
            ExecutionEventCode.ESCALATED_TO_STRONG,
        )
    else:
        event_codes = ()
        if step_type is ExecutionStepType.MODEL_CALL:
            facts = {
                "model_role": model_role.value,
                "provider": "foundry",
                "deployment": f"{model_role.value.lower()}-deployment",
                "request_id": request_id,
            }
        elif step_type is ExecutionStepType.QUALITY_EVALUATION:
            if evaluation_result is None:
                raise ValueError("successful evaluation steps require evidence")
            facts = {
                "model_role": model_role.value,
                "evaluator_type": evaluation_result.evaluator_type,
                "evaluator_valid": evaluation_result.evaluator_valid,
                "score": evaluation_result.score,
                "threshold": evaluation_result.threshold,
                "passed": evaluation_result.passed,
            }
            if evaluation_result.passed:
                event_codes = (ExecutionEventCode.QUALITY_CONTRACT_MET,)
            else:
                event_codes = ()
                if (
                    evaluation_result.evaluator_valid
                    and evaluation_result.score < evaluation_result.threshold
                ):
                    event_codes += (ExecutionEventCode.QUALITY_THRESHOLD_NOT_MET,)
                if model_role is ModelRole.STRONG and evaluation_result.evaluator_valid:
                    event_codes += (ExecutionEventCode.FINAL_QUALITY_CONTRACT_NOT_MET,)
        elif (
            step_type is ExecutionStepType.RETURN
            and status is ExecutionStatus.SUCCEEDED
        ):
            facts = {
                "model_role": model_role.value,
                "contract_met": contract_met,
            }
        elif step_type in {
            ExecutionStepType.RETURN,
        }:
            facts = {"model_role": model_role.value}
        else:
            facts = {}
    return ExecutionStep(
        sequence=sequence,
        step_type=step_type,
        status=status,
        latency_ms=0,
        event_codes=event_codes,
        facts=facts,
        error=(
            error
            if status in {ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT}
            else None
        ),
    )


def usage(
    *,
    run_id: str,
    request_id: str,
    input_tokens: int,
    output_tokens: int,
    cost: Decimal | None,
    provenance: PricingProvenance | None,
    model_role: ModelRole = ModelRole.SMALL,
) -> ModelUsage:
    """Build one measured provider usage record."""
    return ModelUsage(
        request_id=request_id,
        run_id=run_id,
        provider="foundry",
        deployment=f"{model_role.value.lower()}-deployment",
        model_role=model_role,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=100,
        calculated_cost=cost,
        pricing_provenance=provenance,
    )


def completed_run(
    *,
    run_id: str,
    input_tokens: int = 800,
    output_tokens: int = 200,
    cost: Decimal | None = Decimal("4"),
    latency_ms: int = 1000,
    score: float = 0.95,
    contract_met: bool = True,
    evaluator_type: str = "deterministic",
    evaluator_metadata: dict[str, JsonValue] | None = None,
    contract: QualityContract | None = None,
    profile: RequestProfile | None = None,
    model_calls: int = 1,
    catalog_version: str = "benchmark-v1",
    currency: str = "USD",
) -> RunResult:
    """Build a complete measured run with one or two model calls."""
    bound_contract = contract or quality_contract()
    bound_profile = profile or request_profile()
    final_evaluation = evaluation(
        score=score,
        evaluator_type=evaluator_type,
        passed=contract_met,
        metadata=evaluator_metadata,
    )
    uses_llm_judge = evaluator_type == "llm_judge"
    steps: tuple[ExecutionStep, ...]
    usages: tuple[ModelUsage, ...]
    evaluations: tuple[EvaluationResult, ...]
    provenance = (
        PricingProvenance(
            catalog_version=catalog_version,
            currency=currency,
        )
        if cost is not None
        else None
    )
    if model_calls == 1:
        steps = (
            step(
                0,
                ExecutionStepType.MODEL_CALL,
                request_id=f"{run_id}-request-1",
            ),
            step(
                1,
                ExecutionStepType.QUALITY_EVALUATION,
                evaluation_result=final_evaluation,
            ),
            step(2, ExecutionStepType.RETURN, contract_met=contract_met),
        )
        if uses_llm_judge:
            steps = (
                steps[0],
                steps[1].model_copy(
                    update={
                        "facts": {
                            **steps[1].facts,
                            "judge_call_recorded": True,
                        }
                    }
                ),
                steps[2],
            )
        usages = (
            usage(
                run_id=run_id,
                request_id=f"{run_id}-request-1",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=cost,
                provenance=provenance,
            ),
        )
        if uses_llm_judge:
            usages += (
                usage(
                    run_id=run_id,
                    request_id=f"{run_id}-judge-request-1",
                    input_tokens=0,
                    output_tokens=0,
                    cost=Decimal("0") if cost is not None else None,
                    provenance=provenance,
                    model_role=ModelRole.JUDGE,
                ),
            )
        evaluations = (final_evaluation,)
        escalated = False
    else:
        first_evaluation = evaluation(
            score=0.50,
            evaluator_type=evaluator_type,
            passed=False,
            metadata=evaluator_metadata,
        )
        if cost is None:
            first_cost = None
            second_cost = None
        else:
            first_cost = cost / 2
            second_cost = cost - first_cost
        steps = (
            step(
                0,
                ExecutionStepType.MODEL_CALL,
                request_id=f"{run_id}-request-1",
            ),
            step(
                1,
                ExecutionStepType.QUALITY_EVALUATION,
                evaluation_result=first_evaluation,
            ),
            step(2, ExecutionStepType.ESCALATION),
            step(
                3,
                ExecutionStepType.MODEL_CALL,
                model_role=ModelRole.STRONG,
                request_id=f"{run_id}-request-2",
            ),
            step(
                4,
                ExecutionStepType.QUALITY_EVALUATION,
                model_role=ModelRole.STRONG,
                evaluation_result=final_evaluation,
            ),
            step(
                5,
                ExecutionStepType.RETURN,
                model_role=ModelRole.STRONG,
                contract_met=contract_met,
            ),
        )
        if uses_llm_judge:
            steps = tuple(
                step_value.model_copy(
                    update={
                        "facts": {
                            **step_value.facts,
                            "judge_call_recorded": True,
                        }
                    }
                )
                if step_value.step_type is ExecutionStepType.QUALITY_EVALUATION
                else step_value
                for step_value in steps
            )
        usages = (
            usage(
                run_id=run_id,
                request_id=f"{run_id}-request-1",
                input_tokens=input_tokens // 2,
                output_tokens=output_tokens // 2,
                cost=first_cost,
                provenance=provenance,
            ),
            usage(
                run_id=run_id,
                request_id=f"{run_id}-request-2",
                input_tokens=input_tokens - input_tokens // 2,
                output_tokens=output_tokens - output_tokens // 2,
                cost=second_cost,
                provenance=provenance,
                model_role=ModelRole.STRONG,
            ),
        )
        if uses_llm_judge:
            judge_cost = Decimal("0") if cost is not None else None
            usages = (
                usages[0],
                usage(
                    run_id=run_id,
                    request_id=f"{run_id}-judge-request-1",
                    input_tokens=0,
                    output_tokens=0,
                    cost=judge_cost,
                    provenance=provenance,
                    model_role=ModelRole.JUDGE,
                ),
                usages[1],
                usage(
                    run_id=run_id,
                    request_id=f"{run_id}-judge-request-2",
                    input_tokens=0,
                    output_tokens=0,
                    cost=judge_cost,
                    provenance=provenance,
                    model_role=ModelRole.JUDGE,
                ),
            )
        evaluations = (first_evaluation, final_evaluation)
        escalated = True

    return RunResult(
        run_id=run_id,
        correlation_id=f"{run_id}-correlation",
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        status=RunStatus.COMPLETED,
        quality_contract=bound_contract,
        request_profile=bound_profile,
        request_binding=request_binding(bound_profile),
        execution_plan=execution_plan(
            contract=bound_contract,
            profile=bound_profile,
        ),
        steps=steps,
        model_usages=usages,
        evaluations=evaluations,
        final_evaluation=final_evaluation,
        final_output="Measured answer",
        contract_met=contract_met,
        escalated=escalated,
        latency_ms=latency_ms,
    )


def invalid_evaluation_run(*, run_id: str, evaluator_type: str) -> RunResult:
    """Build an interrupted run whose final evaluator evidence is invalid."""
    final_evaluation = evaluation(
        score=0.99,
        evaluator_type=evaluator_type,
        evaluator_valid=False,
        passed=False,
    )
    return RunResult(
        run_id=run_id,
        correlation_id=f"{run_id}-correlation",
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        status=RunStatus.FAILED,
        quality_contract=quality_contract(),
        request_profile=request_profile(),
        request_binding=request_binding(),
        execution_plan=execution_plan(),
        steps=(
            step(
                0,
                ExecutionStepType.MODEL_CALL,
                request_id=f"{run_id}-request-1",
            ),
            step(
                1,
                ExecutionStepType.QUALITY_EVALUATION,
                evaluation_result=final_evaluation,
            ),
            step(
                2,
                ExecutionStepType.RETURN,
                status=ExecutionStatus.FAILED,
                error="Evaluator evidence invalid",
            ),
        ),
        model_usages=(
            usage(
                run_id=run_id,
                request_id=f"{run_id}-request-1",
                input_tokens=100,
                output_tokens=20,
                cost=Decimal("1"),
                provenance=PricingProvenance(
                    catalog_version="benchmark-v1",
                    currency="USD",
                ),
            ),
        ),
        evaluations=(final_evaluation,),
        final_evaluation=final_evaluation,
        final_output=None,
        contract_met=None,
        escalated=False,
        latency_ms=200,
        error="Evaluator evidence invalid",
    )


def incomplete_run(*, run_id: str) -> RunResult:
    """Build a provider failure with unavailable complete-attempt totals."""
    return RunResult(
        run_id=run_id,
        correlation_id=f"{run_id}-correlation",
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        status=RunStatus.FAILED,
        quality_contract=quality_contract(),
        request_profile=request_profile(),
        request_binding=request_binding(),
        execution_plan=execution_plan(),
        steps=(
            step(
                0,
                ExecutionStepType.MODEL_CALL,
                status=ExecutionStatus.FAILED,
            ),
        ),
        model_usages=(),
        evaluations=(),
        final_evaluation=None,
        final_output=None,
        contract_met=None,
        escalated=False,
        latency_ms=150,
        error="Provider failed",
    )


def comparison_request(
    baseline: RunResult,
    optima: RunResult,
    *,
    baseline_arm: ComparisonArm = ComparisonArm.BASELINE,
    optima_arm: ComparisonArm = ComparisonArm.OPTIMA,
    baseline_identity: BenchmarkCaseIdentity | None = None,
    optima_identity: BenchmarkCaseIdentity | None = None,
) -> BaselineComparisonRequest:
    """Build a typed request for two explicitly identified runs."""
    identity = BenchmarkCaseIdentity(
        benchmark_case_id="case-001",
        input_fingerprint="sha256:canonical-input",
    )
    return BaselineComparisonRequest(
        baseline=ComparableRun(
            arm=baseline_arm,
            identity=baseline_identity or identity,
            run_result=baseline,
        ),
        optima=ComparableRun(
            arm=optima_arm,
            identity=optima_identity or identity,
            run_result=optima,
        ),
    )


def test_comparison_preserves_metrics_and_computes_positive_savings() -> None:
    """Preserve both arms and compute every delta in its documented direction."""
    baseline = completed_run(run_id="baseline", model_calls=2)
    optima = completed_run(
        run_id="optima",
        input_tokens=400,
        output_tokens=100,
        cost=Decimal("1"),
        latency_ms=800,
        score=0.92,
    )

    result = BaselineComparisonService().compare(comparison_request(baseline, optima))

    assert result.baseline.model_calls == 2
    assert result.optima.model_calls == 1
    assert result.baseline.input_tokens == 800
    assert result.optima.output_tokens == 100
    assert result.baseline.total_tokens == 1000
    assert result.optima.total_tokens == 500
    assert result.baseline.cost == Decimal("4")
    assert result.optima.cost == Decimal("1")
    assert result.baseline.cost_provenance == PricingProvenance(
        catalog_version="benchmark-v1",
        currency="USD",
    )
    assert result.optima.cost_provenance == result.baseline.cost_provenance
    assert result.baseline.latency_ms == 1000
    assert result.optima.quality_score == 0.92
    assert result.optima.contract_met is True
    assert result.model_calls_delta == -1
    assert result.input_tokens_delta == -400
    assert result.output_tokens_delta == -100
    assert result.total_tokens_delta == -500
    assert result.cost_delta == Decimal("-3")
    assert result.latency_ms_delta == -200
    assert result.quality_score_delta == pytest.approx(-0.03)
    assert result.token_reduction_percentage == Decimal("50")
    assert result.cost_reduction_percentage == Decimal("75")
    assert result.latency_percentage_change == Decimal("-20")


def judge_identity(**updates: JsonValue) -> dict[str, JsonValue]:
    """Build complete versioned LLM-judge comparison identity."""
    values: dict[str, JsonValue] = {
        "prompt_version": "optima-llm-judge-prompt-v1",
        "schema_version": "optima-llm-judge-response-v1",
        "judge_model": "judge-model-v1",
        "judge_deployment": "judge-deployment",
    }
    values.update(updates)
    return values


def test_comparison_model_calls_include_judge_inference() -> None:
    """Count all paid generator and evaluator model calls in both arms."""
    baseline = completed_run(
        run_id="baseline",
        model_calls=2,
        evaluator_type="llm_judge",
        evaluator_metadata=judge_identity(),
    )
    optima = completed_run(
        run_id="optima",
        model_calls=1,
        evaluator_type="llm_judge",
        evaluator_metadata=judge_identity(),
    )

    result = BaselineComparisonService().compare(comparison_request(baseline, optima))

    assert result.baseline.model_calls == 4
    assert result.optima.model_calls == 2
    assert result.model_calls_delta == -2


def test_equal_measurements_produce_zero_deltas_and_percentages() -> None:
    """Represent no measured change as zero rather than unavailable."""
    result = BaselineComparisonService().compare(
        comparison_request(
            completed_run(run_id="baseline"),
            completed_run(run_id="optima"),
        )
    )

    assert result.total_tokens_delta == 0
    assert result.cost_delta == Decimal("0")
    assert result.latency_ms_delta == 0
    assert result.quality_score_delta == 0.0
    assert result.token_reduction_percentage == Decimal("0")
    assert result.cost_reduction_percentage == Decimal("0")
    assert result.latency_percentage_change == Decimal("0")


def test_regressions_remain_visible_as_negative_reductions() -> None:
    """Do not clamp increased token use, cost, or latency."""
    result = BaselineComparisonService().compare(
        comparison_request(
            completed_run(
                run_id="baseline",
                input_tokens=400,
                output_tokens=100,
                cost=Decimal("1"),
                latency_ms=500,
            ),
            completed_run(
                run_id="optima",
                input_tokens=800,
                output_tokens=200,
                cost=Decimal("2"),
                latency_ms=750,
            ),
        )
    )

    assert result.total_tokens_delta == 500
    assert result.cost_delta == Decimal("1")
    assert result.latency_ms_delta == 250
    assert result.token_reduction_percentage == Decimal("-100")
    assert result.cost_reduction_percentage == Decimal("-100")
    assert result.latency_percentage_change == Decimal("50")


def test_missing_complete_measurements_remain_unavailable() -> None:
    """Do not form partial token or cost comparisons from incomplete attempts."""
    result = BaselineComparisonService().compare(
        comparison_request(
            incomplete_run(run_id="baseline"),
            completed_run(run_id="optima"),
        )
    )

    assert result.baseline.model_calls == 1
    assert result.baseline.input_tokens is None
    assert result.baseline.output_tokens is None
    assert result.baseline.total_tokens is None
    assert result.baseline.cost is None
    assert result.input_tokens_delta is None
    assert result.output_tokens_delta is None
    assert result.total_tokens_delta is None
    assert result.cost_delta is None
    assert result.token_reduction_percentage is None
    assert result.cost_reduction_percentage is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_type", TaskType.SUMMARIZATION),
        ("complexity", Complexity.HIGH),
        ("input_tokens", 801),
        ("risk_tier", RiskTier.HIGH),
        ("cache_eligible", True),
        ("has_large_context", True),
    ],
)
def test_request_rejects_different_request_profiles(
    field: str,
    value: object,
) -> None:
    """Require every typed RequestProfile fact to match between both arms."""
    with pytest.raises(ValidationError, match="same RequestProfile"):
        comparison_request(
            completed_run(run_id="baseline"),
            completed_run(
                run_id="optima",
                profile=request_profile(**{field: value}),
            ),
        )


@pytest.mark.parametrize(
    ("catalog_version", "currency"),
    [
        ("benchmark-v2", "USD"),
        ("benchmark-v1", "EUR"),
    ],
)
def test_request_rejects_incompatible_pricing_provenance(
    catalog_version: str,
    currency: str,
) -> None:
    """Reject cost comparisons across catalog versions or currencies."""
    with pytest.raises(ValidationError, match="same pricing provenance"):
        comparison_request(
            completed_run(run_id="baseline"),
            completed_run(
                run_id="optima",
                catalog_version=catalog_version,
                currency=currency,
            ),
        )


def test_missing_optima_measurements_remain_unavailable() -> None:
    """Apply the same no-fabrication rule when OPTIMA measurements are absent."""
    result = BaselineComparisonService().compare(
        comparison_request(
            completed_run(run_id="baseline"),
            incomplete_run(run_id="optima"),
        )
    )

    assert result.optima.input_tokens is None
    assert result.optima.output_tokens is None
    assert result.optima.total_tokens is None
    assert result.optima.cost is None
    assert result.total_tokens_delta is None
    assert result.cost_delta is None
    assert result.token_reduction_percentage is None
    assert result.cost_reduction_percentage is None


def test_missing_cost_does_not_hide_available_token_measurements() -> None:
    """Keep independently measured tokens when only pricing is unavailable."""
    result = BaselineComparisonService().compare(
        comparison_request(
            completed_run(run_id="baseline", cost=None),
            completed_run(run_id="optima", cost=Decimal("1")),
        )
    )

    assert result.baseline.total_tokens == 1000
    assert result.total_tokens_delta == 0
    assert result.baseline.cost is None
    assert result.cost_delta is None
    assert result.cost_reduction_percentage is None


def test_zero_baseline_denominators_make_percentages_unavailable() -> None:
    """Avoid fabricating percentage changes or dividing by zero."""
    result = BaselineComparisonService().compare(
        comparison_request(
            completed_run(
                run_id="baseline",
                input_tokens=0,
                output_tokens=0,
                cost=Decimal("0"),
                latency_ms=0,
            ),
            completed_run(
                run_id="optima",
                input_tokens=10,
                output_tokens=5,
                cost=Decimal("1"),
                latency_ms=10,
            ),
        )
    )

    assert result.total_tokens_delta == 15
    assert result.cost_delta == Decimal("1")
    assert result.latency_ms_delta == 10
    assert result.token_reduction_percentage is None
    assert result.cost_reduction_percentage is None
    assert result.latency_percentage_change is None


def test_quality_is_preserved_as_evidence_not_efficiency_improvement() -> None:
    """Expose valid scores and contract outcomes independently from savings."""
    result = BaselineComparisonService().compare(
        comparison_request(
            completed_run(run_id="baseline", score=0.98),
            completed_run(run_id="optima", score=0.91),
        )
    )

    assert result.baseline.evaluator_type == "deterministic"
    assert result.baseline.quality_score == 0.98
    assert result.optima.quality_score == 0.91
    assert result.baseline.contract_met is True
    assert result.optima.contract_met is True
    assert result.quality_score_delta == pytest.approx(-0.07)


def test_baseline_pass_and_optima_fail_remain_visible() -> None:
    """Expose an OPTIMA contract regression independently from efficiency."""
    result = BaselineComparisonService().compare(
        comparison_request(
            completed_run(run_id="baseline", score=0.95, contract_met=True),
            completed_run(
                run_id="optima",
                score=0.85,
                contract_met=False,
                model_calls=2,
            ),
        )
    )

    assert result.baseline.contract_met is True
    assert result.optima.contract_met is False
    assert result.baseline.quality_score == 0.95
    assert result.optima.quality_score == 0.85
    assert result.quality_score_delta == pytest.approx(-0.10)


def test_invalid_final_evaluation_contributes_no_quality_or_contract_status() -> None:
    """Exclude invalid score and pass/fail evidence without rejecting its type."""
    result = BaselineComparisonService().compare(
        comparison_request(
            invalid_evaluation_run(
                run_id="baseline",
                evaluator_type="invalid-baseline-judge",
            ),
            completed_run(
                run_id="optima",
                evaluator_type="deterministic",
            ),
        )
    )

    assert result.baseline.evaluator_type is None
    assert result.baseline.quality_score is None
    assert result.baseline.contract_met is None
    assert result.optima.quality_score == 0.95
    assert result.quality_score_delta is None


@pytest.mark.parametrize(
    ("baseline_arm", "optima_arm", "message"),
    [
        (ComparisonArm.OPTIMA, ComparisonArm.OPTIMA, "BASELINE arm"),
        (ComparisonArm.BASELINE, ComparisonArm.BASELINE, "OPTIMA arm"),
    ],
)
def test_request_rejects_incorrect_arm_labels(
    baseline_arm: ComparisonArm,
    optima_arm: ComparisonArm,
    message: str,
) -> None:
    """Require one correctly labeled baseline and one correctly labeled OPTIMA run."""
    with pytest.raises(ValidationError, match=message):
        comparison_request(
            completed_run(run_id="baseline"),
            completed_run(run_id="optima"),
            baseline_arm=baseline_arm,
            optima_arm=optima_arm,
        )


@pytest.mark.parametrize(
    "optima_identity",
    [
        BenchmarkCaseIdentity(
            benchmark_case_id="case-002",
            input_fingerprint="sha256:canonical-input",
        ),
        BenchmarkCaseIdentity(
            benchmark_case_id="case-001",
            input_fingerprint="sha256:different-input",
        ),
    ],
)
def test_request_rejects_case_or_fingerprint_mismatch(
    optima_identity: BenchmarkCaseIdentity,
) -> None:
    """Require exact benchmark case and canonical input identity."""
    with pytest.raises(ValidationError, match="same benchmark identity"):
        comparison_request(
            completed_run(run_id="baseline"),
            completed_run(run_id="optima"),
            optima_identity=optima_identity,
        )


def test_request_rejects_same_run_id() -> None:
    """Prevent one measured run from serving as both comparison arms."""
    with pytest.raises(ValidationError, match="different run IDs"):
        comparison_request(
            completed_run(run_id="same-run"),
            completed_run(run_id="same-run"),
        )


def test_request_rejects_different_quality_contracts() -> None:
    """Require exact Quality Contract equality before claiming savings."""
    with pytest.raises(ValidationError, match="same Quality Contract"):
        comparison_request(
            completed_run(run_id="baseline"),
            completed_run(
                run_id="optima",
                contract=quality_contract(
                    optimization_mode=OptimizationMode.BALANCED,
                ),
            ),
        )


def test_request_rejects_different_valid_evaluator_types() -> None:
    """Compare quality only when both valid final evaluations use one evaluator type."""
    with pytest.raises(ValidationError, match="same evaluator identity"):
        comparison_request(
            completed_run(
                run_id="baseline",
                evaluator_type="deterministic",
            ),
            completed_run(
                run_id="optima",
                evaluator_type="llm-judge",
            ),
        )


@pytest.mark.parametrize(
    ("identity_update", "message"),
    [
        ({"judge_model": "different-model"}, "same evaluator identity"),
        ({"judge_deployment": "different-deployment"}, "same evaluator identity"),
        ({"prompt_version": "different-prompt"}, "same evaluator identity"),
        ({"schema_version": "different-schema"}, "same evaluator identity"),
        ({"judge_model": None}, "complete evaluator identity"),
    ],
    ids=["model", "deployment", "prompt", "schema", "incomplete"],
)
def test_request_rejects_incompatible_llm_judge_identity(
    identity_update: dict[str, JsonValue],
    message: str,
) -> None:
    """Compare model-generated quality only under one complete judge identity."""
    with pytest.raises(ValidationError, match=message):
        comparison_request(
            completed_run(
                run_id="baseline",
                evaluator_type="llm_judge",
                evaluator_metadata=judge_identity(),
            ),
            completed_run(
                run_id="optima",
                evaluator_type="llm_judge",
                evaluator_metadata=judge_identity(**identity_update),
            ),
        )


def test_comparison_contracts_are_immutable_and_forbid_extra_fields() -> None:
    """Keep comparison inputs stable and reject untyped data."""
    identity = BenchmarkCaseIdentity(
        benchmark_case_id="case-001",
        input_fingerprint="sha256:canonical-input",
    )

    with pytest.raises(ValidationError, match="frozen"):
        identity.benchmark_case_id = "changed"
    with pytest.raises(ValidationError, match="Extra inputs"):
        BenchmarkCaseIdentity.model_validate(
            {
                "benchmark_case_id": "case-001",
                "input_fingerprint": "sha256:canonical-input",
                "unexpected": True,
            }
        )


@pytest.mark.parametrize(
    ("cost", "cost_provenance"),
    [
        (Decimal("1"), None),
        (
            None,
            PricingProvenance(
                catalog_version="benchmark-v1",
                currency="USD",
            ),
        ),
    ],
)
def test_execution_metrics_reject_cost_without_matching_provenance(
    cost: Decimal | None,
    cost_provenance: PricingProvenance | None,
) -> None:
    """Keep comparison output amounts inseparable from catalog identity."""
    with pytest.raises(ValidationError, match="must be provided together"):
        ExecutionMetrics(
            arm=ComparisonArm.BASELINE,
            run_id="baseline",
            model_calls=1,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cost=cost,
            cost_provenance=cost_provenance,
            latency_ms=100,
            evaluator_type="deterministic",
            quality_score=0.95,
            contract_met=True,
        )
