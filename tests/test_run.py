"""Tests for measured model usage and actual run results."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
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
    QualityProfile,
    RiskTier,
    build_quality_contract,
)
from optima.domain.request_profile import Complexity, RequestProfile, TaskType
from optima.domain.run import ModelUsage, RunResult, RunStatus


def quality_contract() -> object:
    """Build the shared High Quality Contract."""
    return build_quality_contract(
        quality_profile=QualityProfile.HIGH,
        optimization_mode=OptimizationMode.COST,
        risk_tier=RiskTier.MEDIUM,
    )


def request_profile() -> RequestProfile:
    """Build the shared Request Profile."""
    return RequestProfile(
        task_type=TaskType.LOG_ANALYSIS,
        complexity=Complexity.MEDIUM,
        input_tokens=2500,
        risk_tier=RiskTier.MEDIUM,
        cache_eligible=True,
        has_large_context=False,
    )


def decision_evidence(
    *,
    base_model_policy: ModelPolicy | None,
    final_model_policy: ModelPolicy | None,
    cache_candidate_assessed: bool = False,
) -> PlannerDecisionEvidence:
    """Build typed evidence shared by run-result plan fixtures."""
    return PlannerDecisionEvidence(
        profile_risk_tier=RiskTier.MEDIUM,
        contract_risk_tier=RiskTier.MEDIUM,
        effective_risk_tier=RiskTier.MEDIUM,
        module_states=PlannerModuleStates(
            semantic_cache_enabled=True,
            context_reduction_enabled=True,
            historical_policy_enabled=True,
            foundry_router_comparator_enabled=False,
        ),
        cache_candidate_assessed=cache_candidate_assessed,
        base_model_policy=base_model_policy,
        final_model_policy=final_model_policy,
    )


def execution_plan() -> ExecutionPlan:
    """Build the shared small-first execution plan."""
    return ExecutionPlan(
        cache_policy=CachePolicy.SKIP,
        context_policy=ContextPolicy.KEEP_ORIGINAL,
        model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
        initial_model_role=ModelRole.SMALL,
        verification_required=True,
        escalation_model_role=ModelRole.STRONG,
        optimization_mode=OptimizationMode.COST,
        reason_codes=(
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
            PlannerReasonCode.SMALL_FIRST_SELECTED,
        ),
        human_readable_name="Small -> Verify -> Escalate if needed",
        decision_evidence=decision_evidence(
            base_model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            final_model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
        ),
    )


def passing_evaluation() -> EvaluationResult:
    """Build final evidence that meets the shared contract."""
    return EvaluationResult(
        evaluator_type="deterministic",
        evaluator_valid=True,
        score=0.93,
        threshold=0.90,
        mandatory_checks_passed=True,
        passed=True,
        reasons=("Required quality met",),
    )


def successful_step(sequence: int, step_type: ExecutionStepType) -> ExecutionStep:
    """Build one successful execution trace step."""
    return ExecutionStep(
        sequence=sequence,
        step_type=step_type,
        status=ExecutionStatus.SUCCEEDED,
        latency_ms=10,
    )


def unsuccessful_step(
    sequence: int,
    step_type: ExecutionStepType,
    status: ExecutionStatus,
) -> ExecutionStep:
    """Build one failed, timed-out, or skipped execution trace step."""
    return ExecutionStep(
        sequence=sequence,
        step_type=step_type,
        status=status,
        latency_ms=10,
        error=(
            "Step did not complete"
            if status in {ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT}
            else None
        ),
    )


def model_usage(
    *,
    request_id: str = "provider-request-1",
    run_id: str = "run-1",
    model_role: ModelRole = ModelRole.SMALL,
    input_tokens: int = 100,
    output_tokens: int = 20,
    calculated_cost: Decimal | None = None,
) -> ModelUsage:
    """Build one measured model-call usage record."""
    return ModelUsage(
        request_id=request_id,
        run_id=run_id,
        provider="foundry",
        deployment=f"{model_role.value.lower()}-deployment",
        model_role=model_role,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=125,
        calculated_cost=calculated_cost,
    )


def completed_run(**updates: object) -> RunResult:
    """Build a valid completed run with optional test overrides."""
    evaluation = passing_evaluation()
    values: dict[str, object] = {
        "run_id": "run-1",
        "correlation_id": "correlation-1",
        "created_at": datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        "status": RunStatus.COMPLETED,
        "quality_contract": quality_contract(),
        "request_profile": request_profile(),
        "execution_plan": execution_plan(),
        "steps": (
            successful_step(0, ExecutionStepType.MODEL_CALL),
            successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(2, ExecutionStepType.RETURN),
        ),
        "model_usages": (model_usage(),),
        "evaluations": (evaluation,),
        "final_evaluation": evaluation,
        "final_output": "Final answer",
        "contract_met": True,
        "escalated": False,
        "latency_ms": 30,
        "error": None,
    }
    values.update(updates)
    return RunResult.model_validate(values)


def interrupted_run(
    *,
    status: RunStatus,
    steps: tuple[ExecutionStep, ...],
    model_usages: tuple[ModelUsage, ...] = (),
    evaluations: tuple[EvaluationResult, ...] = (),
) -> RunResult:
    """Build a failed or timed-out run without fabricating measurements."""
    return completed_run(
        status=status,
        steps=steps,
        model_usages=model_usages,
        evaluations=evaluations,
        final_evaluation=None,
        final_output=None,
        contract_met=None,
        latency_ms=30,
        error="Run did not complete",
    )


@pytest.mark.parametrize("calculated_cost", [Decimal("0"), Decimal("0.00125")])
def test_model_usage_preserves_measured_call_facts_and_decimal_cost(
    calculated_cost: Decimal,
) -> None:
    """Represent every required model-call usage fact."""
    usage = ModelUsage(
        request_id="provider-request-1",
        run_id="run-1",
        provider="foundry",
        deployment="small-deployment",
        model_role=ModelRole.SMALL,
        input_tokens=100,
        output_tokens=20,
        cached_tokens=10,
        latency_ms=125,
        calculated_cost=calculated_cost,
    )

    assert usage.calculated_cost == calculated_cost
    assert usage.cached_tokens == 10


def test_model_usage_allows_unavailable_cost_without_placeholder_zero() -> None:
    """Use None when Slice 6 has not calculated a model-call cost."""
    usage = ModelUsage(
        request_id="provider-request-1",
        run_id="run-1",
        provider="foundry",
        deployment="small-deployment",
        model_role=ModelRole.SMALL,
        input_tokens=100,
        output_tokens=20,
        latency_ms=125,
    )

    assert usage.calculated_cost is None
    assert usage.cached_tokens is None


@pytest.mark.parametrize("cost", [Decimal("-0.01"), Decimal("NaN")])
def test_model_usage_rejects_invalid_cost(cost: Decimal) -> None:
    """Reject negative or non-finite calculated monetary values."""
    with pytest.raises(ValidationError):
        ModelUsage(
            request_id="provider-request-1",
            run_id="run-1",
            provider="foundry",
            deployment="small-deployment",
            model_role=ModelRole.SMALL,
            input_tokens=100,
            output_tokens=20,
            latency_ms=125,
            calculated_cost=cost,
        )


def test_completed_run_preserves_request_profile_and_measured_contract_result() -> None:
    """Expose profile facts and valid final quality evidence in Run History."""
    result = completed_run()

    assert result.request_profile.task_type is TaskType.LOG_ANALYSIS
    assert result.request_profile.complexity is Complexity.MEDIUM
    assert result.contract_met is True


def test_run_aggregates_one_complete_model_call() -> None:
    """Expose exact token and Decimal cost totals for a small-only run."""
    result = completed_run(
        model_usages=(
            model_usage(
                input_tokens=101,
                output_tokens=19,
                calculated_cost=Decimal("0.0011"),
            ),
        )
    )

    assert result.total_input_tokens == 101
    assert result.total_output_tokens == 19
    assert result.total_tokens == 120
    assert result.total_calculated_cost == Decimal("0.0011")


def test_run_aggregates_escalated_calls_in_execution_order() -> None:
    """Include both measured calls using exact Decimal arithmetic."""
    small_evaluation = EvaluationResult(
        evaluator_type="deterministic",
        evaluator_valid=True,
        score=0.80,
        threshold=0.90,
        mandatory_checks_passed=True,
        passed=False,
        reasons=("Small result did not meet quality",),
    )
    final_evaluation = passing_evaluation()
    result = completed_run(
        steps=(
            successful_step(0, ExecutionStepType.MODEL_CALL),
            successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(2, ExecutionStepType.ESCALATION),
            successful_step(3, ExecutionStepType.MODEL_CALL),
            successful_step(4, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(5, ExecutionStepType.RETURN),
        ),
        model_usages=(
            model_usage(
                input_tokens=100,
                output_tokens=20,
                calculated_cost=Decimal("0.0011"),
            ),
            model_usage(
                request_id="provider-request-2",
                model_role=ModelRole.STRONG,
                input_tokens=110,
                output_tokens=30,
                calculated_cost=Decimal("0.0022"),
            ),
        ),
        evaluations=(small_evaluation, final_evaluation),
        final_evaluation=final_evaluation,
        escalated=True,
    )

    assert tuple(usage.model_role for usage in result.model_usages) == (
        ModelRole.SMALL,
        ModelRole.STRONG,
    )
    assert result.total_input_tokens == 210
    assert result.total_output_tokens == 50
    assert result.total_tokens == 260
    assert result.total_calculated_cost == Decimal("0.0033")


def test_run_does_not_convert_unavailable_cost_to_zero() -> None:
    """Keep total cost unavailable when an executed call has no measured cost."""
    result = completed_run(
        model_usages=(
            model_usage(input_tokens=9, output_tokens=4, calculated_cost=None),
        )
    )

    assert result.total_tokens == 13
    assert result.total_calculated_cost is None


def test_escalated_known_and_unknown_costs_do_not_form_partial_total() -> None:
    """Keep aggregate cost unavailable if either executed call lacks cost."""
    first_evaluation = EvaluationResult(
        evaluator_type="deterministic",
        evaluator_valid=True,
        score=0.80,
        threshold=0.90,
        mandatory_checks_passed=True,
        passed=False,
        reasons=("Small result did not meet quality",),
    )
    final_evaluation = passing_evaluation()
    result = completed_run(
        steps=(
            successful_step(0, ExecutionStepType.MODEL_CALL),
            successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(2, ExecutionStepType.ESCALATION),
            successful_step(3, ExecutionStepType.MODEL_CALL),
            successful_step(4, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(5, ExecutionStepType.RETURN),
        ),
        model_usages=(
            model_usage(calculated_cost=Decimal("0.00125")),
            model_usage(
                request_id="provider-request-2",
                model_role=ModelRole.STRONG,
                calculated_cost=None,
            ),
        ),
        evaluations=(first_evaluation, final_evaluation),
        final_evaluation=final_evaluation,
        escalated=True,
    )

    assert result.total_tokens == 240
    assert result.total_calculated_cost is None


def test_completed_run_can_record_measured_contract_failure() -> None:
    """Use False only when a valid final evaluation measured failure."""
    failed_evaluation = EvaluationResult(
        evaluator_type="deterministic",
        evaluator_valid=True,
        score=0.80,
        threshold=0.90,
        mandatory_checks_passed=True,
        passed=False,
        reasons=("Required quality not met",),
    )

    result = completed_run(
        evaluations=(failed_evaluation,),
        final_evaluation=failed_evaluation,
        contract_met=False,
    )

    assert result.contract_met is False


def test_completed_semantic_cache_run_has_no_model_usage() -> None:
    """Represent accepted cache reuse with compatible evaluation evidence."""
    cache_plan = ExecutionPlan(
        cache_policy=CachePolicy.USE_CACHED_RESULT,
        context_policy=ContextPolicy.NOT_APPLICABLE,
        verification_required=False,
        optimization_mode=OptimizationMode.COST,
        reason_codes=(
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
            PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        ),
        human_readable_name="Semantic Cache Hit",
        decision_evidence=decision_evidence(
            base_model_policy=None,
            final_model_policy=None,
            cache_candidate_assessed=True,
        ),
    )

    result = completed_run(
        execution_plan=cache_plan,
        steps=(
            successful_step(0, ExecutionStepType.SEMANTIC_CACHE),
            successful_step(1, ExecutionStepType.RETURN),
        ),
        model_usages=(),
    )

    assert result.execution_plan.cache_policy is CachePolicy.USE_CACHED_RESULT
    assert result.model_usages == ()
    assert result.total_input_tokens == 0
    assert result.total_output_tokens == 0
    assert result.total_tokens == 0
    assert result.total_calculated_cost == Decimal("0")


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.TIMED_OUT])
def test_interrupted_run_uses_none_for_unmeasured_contract(status: RunStatus) -> None:
    """Do not convert unavailable compliance evidence into False."""
    failed_step_status = (
        ExecutionStatus.FAILED
        if status is RunStatus.FAILED
        else ExecutionStatus.TIMED_OUT
    )
    result = interrupted_run(
        status=status,
        steps=(
            unsuccessful_step(
                sequence=0,
                step_type=ExecutionStepType.MODEL_CALL,
                status=failed_step_status,
            ),
        ),
    )

    assert result.contract_met is None


def test_run_rejects_naive_created_at() -> None:
    """Require an unambiguous timezone-aware run timestamp."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        completed_run(created_at=datetime(2026, 8, 18, 12, 0))


def test_escalated_true_requires_escalation_step() -> None:
    """Reject an escalation flag unsupported by the execution trace."""
    with pytest.raises(ValidationError, match="ESCALATION"):
        completed_run(escalated=True)


def test_escalation_step_requires_escalated_true() -> None:
    """Reject an escalation trace hidden by a false summary flag."""
    with pytest.raises(ValidationError, match="ESCALATION"):
        completed_run(
            steps=(
                successful_step(0, ExecutionStepType.MODEL_CALL),
                successful_step(1, ExecutionStepType.ESCALATION),
                successful_step(2, ExecutionStepType.MODEL_CALL),
                successful_step(3, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(4, ExecutionStepType.RETURN),
            )
        )


def test_escalated_run_accepts_bidirectionally_consistent_trace() -> None:
    """Accept escalation only when both trace and summary record it."""
    first_evaluation = EvaluationResult(
        evaluator_type="deterministic",
        evaluator_valid=True,
        score=0.80,
        threshold=0.90,
        mandatory_checks_passed=True,
        passed=False,
        reasons=("Small result did not meet quality",),
    )
    final_evaluation = passing_evaluation()
    result = completed_run(
        steps=(
            successful_step(0, ExecutionStepType.MODEL_CALL),
            successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(2, ExecutionStepType.ESCALATION),
            successful_step(3, ExecutionStepType.MODEL_CALL),
            successful_step(4, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(5, ExecutionStepType.RETURN),
        ),
        model_usages=(
            model_usage(),
            model_usage(
                request_id="provider-request-2",
                model_role=ModelRole.STRONG,
            ),
        ),
        evaluations=(first_evaluation, final_evaluation),
        final_evaluation=final_evaluation,
        escalated=True,
    )

    assert result.escalated is True


@pytest.mark.parametrize(
    "contract_met",
    [False, None],
)
def test_run_rejects_contract_result_inconsistent_with_final_evaluation(
    contract_met: bool | None,
) -> None:
    """Require three-state compliance to follow valid final evidence."""
    with pytest.raises(ValidationError, match="contract_met"):
        completed_run(contract_met=contract_met)


def test_run_rejects_final_evaluation_not_last_in_trace() -> None:
    """Identify the final quality measurement unambiguously."""
    earlier_evaluation = EvaluationResult(
        evaluator_type="deterministic",
        evaluator_valid=True,
        score=0.80,
        threshold=0.90,
        mandatory_checks_passed=True,
        passed=False,
        reasons=("Small result did not meet quality",),
    )

    with pytest.raises(ValidationError, match="final recorded evaluation"):
        completed_run(
            steps=(
                successful_step(0, ExecutionStepType.MODEL_CALL),
                successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(2, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(3, ExecutionStepType.RETURN),
            ),
            evaluations=(passing_evaluation(), earlier_evaluation),
            final_evaluation=passing_evaluation(),
        )


def test_run_rejects_unordered_or_duplicate_execution_steps() -> None:
    """Require a deterministic actual execution trace."""
    with pytest.raises(ValidationError, match="unique and ascending"):
        completed_run(
            steps=(
                successful_step(1, ExecutionStepType.MODEL_CALL),
                successful_step(1, ExecutionStepType.RETURN),
            )
        )


def test_run_rejects_usage_from_another_run() -> None:
    """Keep model-call usage correlated to its owning run."""
    usage = ModelUsage(
        request_id="provider-request-1",
        run_id="other-run",
        provider="foundry",
        deployment="small-deployment",
        model_role=ModelRole.SMALL,
        input_tokens=100,
        output_tokens=20,
        latency_ms=125,
    )

    with pytest.raises(ValidationError, match="belong to this run"):
        completed_run(model_usages=(usage,))


def test_run_rejects_model_call_without_usage_record() -> None:
    """Require structured usage facts for every model-call trace step."""
    with pytest.raises(ValidationError, match="successful calls"):
        completed_run(model_usages=())


def test_run_rejects_evaluation_step_without_result() -> None:
    """Require structured evidence for every model-plan evaluation step."""
    with pytest.raises(ValidationError, match="evaluation result"):
        completed_run(
            evaluations=(),
            final_evaluation=None,
            contract_met=None,
        )


@pytest.mark.parametrize(
    ("run_status", "step_status"),
    [
        (RunStatus.FAILED, ExecutionStatus.FAILED),
        (RunStatus.TIMED_OUT, ExecutionStatus.TIMED_OUT),
    ],
)
def test_unsuccessful_model_call_allows_unavailable_usage(
    run_status: RunStatus,
    step_status: ExecutionStatus,
) -> None:
    """Represent an attempted call without inventing unavailable usage."""
    result = interrupted_run(
        status=run_status,
        steps=(unsuccessful_step(0, ExecutionStepType.MODEL_CALL, step_status),),
    )

    assert result.model_usages == ()


@pytest.mark.parametrize(
    ("run_status", "step_status"),
    [
        (RunStatus.FAILED, ExecutionStatus.FAILED),
        (RunStatus.TIMED_OUT, ExecutionStatus.TIMED_OUT),
    ],
)
def test_unsuccessful_model_call_preserves_available_usage(
    run_status: RunStatus,
    step_status: ExecutionStatus,
) -> None:
    """Retain measured usage when an unsuccessful provider call supplied it."""
    usage = model_usage()
    result = interrupted_run(
        status=run_status,
        steps=(unsuccessful_step(0, ExecutionStepType.MODEL_CALL, step_status),),
        model_usages=(usage,),
    )

    assert result.model_usages == (usage,)


def test_incomplete_attempt_measurements_do_not_fabricate_totals() -> None:
    """Keep totals unavailable when a failed fallback has no usage record."""
    small_evaluation = EvaluationResult(
        evaluator_type="deterministic",
        evaluator_valid=True,
        score=0.80,
        threshold=0.90,
        mandatory_checks_passed=True,
        passed=False,
        reasons=("Small result did not meet quality",),
    )
    result = completed_run(
        status=RunStatus.FAILED,
        steps=(
            successful_step(0, ExecutionStepType.MODEL_CALL),
            successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(2, ExecutionStepType.ESCALATION),
            unsuccessful_step(
                3,
                ExecutionStepType.MODEL_CALL,
                ExecutionStatus.FAILED,
            ),
        ),
        model_usages=(
            model_usage(
                input_tokens=100,
                output_tokens=20,
                calculated_cost=Decimal("0.0011"),
            ),
        ),
        evaluations=(small_evaluation,),
        final_evaluation=None,
        final_output=None,
        contract_met=None,
        escalated=True,
        error="Strong provider failed",
    )

    assert result.total_input_tokens is None
    assert result.total_output_tokens is None
    assert result.total_tokens is None
    assert result.total_calculated_cost is None


def test_skipped_model_call_does_not_require_usage() -> None:
    """Do not fabricate usage for a model call that was never attempted."""
    result = interrupted_run(
        status=RunStatus.FAILED,
        steps=(
            unsuccessful_step(
                0,
                ExecutionStepType.MODEL_CALL,
                ExecutionStatus.SKIPPED,
            ),
        ),
    )

    assert result.model_usages == ()


def test_run_rejects_more_usage_than_non_skipped_model_calls() -> None:
    """Reject usage records that cannot map to an attempted model call."""
    with pytest.raises(ValidationError, match="non-skipped attempts"):
        interrupted_run(
            status=RunStatus.FAILED,
            steps=(
                unsuccessful_step(
                    0,
                    ExecutionStepType.MODEL_CALL,
                    ExecutionStatus.SKIPPED,
                ),
            ),
            model_usages=(model_usage(),),
        )


@pytest.mark.parametrize(
    ("run_status", "step_status"),
    [
        (RunStatus.FAILED, ExecutionStatus.FAILED),
        (RunStatus.TIMED_OUT, ExecutionStatus.TIMED_OUT),
    ],
)
def test_unsuccessful_evaluation_allows_unavailable_result(
    run_status: RunStatus,
    step_status: ExecutionStatus,
) -> None:
    """Represent an unsuccessful evaluation without fabricated evidence."""
    result = interrupted_run(
        status=run_status,
        steps=(
            unsuccessful_step(
                0,
                ExecutionStepType.QUALITY_EVALUATION,
                step_status,
            ),
        ),
    )

    assert result.evaluations == ()


def test_skipped_evaluation_does_not_require_result() -> None:
    """Do not fabricate evidence for an evaluation that was never attempted."""
    result = interrupted_run(
        status=RunStatus.FAILED,
        steps=(
            unsuccessful_step(
                0,
                ExecutionStepType.QUALITY_EVALUATION,
                ExecutionStatus.SKIPPED,
            ),
        ),
    )

    assert result.evaluations == ()


def test_run_rejects_more_results_than_non_skipped_evaluations() -> None:
    """Reject evaluation evidence that cannot map to an attempted evaluation."""
    with pytest.raises(ValidationError, match="non-skipped attempts"):
        interrupted_run(
            status=RunStatus.FAILED,
            steps=(
                unsuccessful_step(
                    0,
                    ExecutionStepType.QUALITY_EVALUATION,
                    ExecutionStatus.SKIPPED,
                ),
            ),
            evaluations=(passing_evaluation(),),
        )
