"""Tests for measured model usage and actual run results."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import JsonValue, ValidationError

from optima.context import ContextPreservationEvidence
from optima.domain.cache import CacheCandidate, CacheCandidateAssessment
from optima.domain.embedding import EmbeddingUsage
from optima.domain.evaluation import EvaluationResult
from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    ContextReductionEvidence,
    ContextReductionOutcome,
    ContextSource,
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
    SemanticCacheEvidence,
    SemanticCacheOutcome,
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


def quality_contract() -> QualityContract:
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
        cache_eligible=False,
        has_large_context=False,
    )


def cache_request_binding() -> RequestBinding:
    """Build the complete binding shared by cache-run fixtures."""
    return build_request_binding(
        input_text="Analyze incident ARC-9",
        context="Incident ARC-9 is resolved.",
        reference_output=None,
        criteria=(),
        metadata={},
        task_type=TaskType.LOG_ANALYSIS,
        complexity=Complexity.MEDIUM,
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
        quality_profile=QualityProfile.HIGH,
        reason_codes=(
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
            PlannerReasonCode.SMALL_FIRST_SELECTED,
        ),
        human_readable_name="Small -> Verify -> Escalate if needed",
        decision_evidence=decision_evidence(
            base_model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
            final_model_policy=ModelPolicy.SMALL_FIRST_WITH_FALLBACK,
        ),
        request_binding=cache_request_binding(),
    )


def reduction_execution_plan() -> ExecutionPlan:
    """Build the shared plan with reduction selected before small-first."""
    return execution_plan().model_copy(
        update={
            "context_policy": ContextPolicy.REDUCE,
            "human_readable_name": (
                "Reduce Context -> Small -> Verify -> Escalate if needed"
            ),
        }
    )


def strong_direct_execution_plan() -> ExecutionPlan:
    """Build a valid strong-direct plan for run invariant tests."""
    return execution_plan().model_copy(
        update={
            "model_policy": ModelPolicy.STRONG_DIRECT,
            "initial_model_role": ModelRole.STRONG,
            "escalation_model_role": None,
            "reason_codes": (
                PlannerReasonCode.OPTIMIZATION_MODE_COST,
                PlannerReasonCode.STRONG_MODEL_REQUIRED,
            ),
            "human_readable_name": "Strong -> Verify",
            "decision_evidence": decision_evidence(
                base_model_policy=ModelPolicy.STRONG_DIRECT,
                final_model_policy=ModelPolicy.STRONG_DIRECT,
            ),
        }
    )


def applied_reduction_step(sequence: int = 0) -> ExecutionStep:
    """Build one valid measured context-reduction execution step."""
    return ExecutionStep(
        sequence=sequence,
        step_type=ExecutionStepType.CONTEXT_REDUCTION,
        status=ExecutionStatus.SUCCEEDED,
        latency_ms=10,
        context_reduction=ContextReductionEvidence(
            outcome=ContextReductionOutcome.APPLIED,
            original_token_count=100,
            effective_token_count=40,
            reducer_name="extractive-v1",
            method="EXTRACTIVE",
            token_counter_name="counter-v1",
            context_source=ContextSource.REDUCED,
            preservation=ContextPreservationEvidence(
                source_order_preserved=True,
                original_segment_count=2,
                retained_segment_indexes=(0,),
                removed_duplicate_count=0,
                removed_irrelevant_count=1,
            ),
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


def successful_step(
    sequence: int,
    step_type: ExecutionStepType,
    *,
    request_id: str | None = "provider-request-1",
) -> ExecutionStep:
    """Build one successful execution trace step."""
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
        if step_type is ExecutionStepType.MODEL_CALL:
            facts = {
                "model_role": ModelRole.SMALL.value,
                "provider": "foundry",
                "deployment": "small-deployment",
                "request_id": request_id,
            }
            event_codes = ()
        elif step_type is ExecutionStepType.QUALITY_EVALUATION:
            facts = {
                "model_role": ModelRole.SMALL.value,
                "evaluator_type": "deterministic",
                "evaluator_valid": True,
                "score": 0.93,
                "threshold": 0.90,
                "passed": True,
            }
            event_codes = (ExecutionEventCode.QUALITY_CONTRACT_MET,)
        elif step_type is ExecutionStepType.RETURN:
            facts = {
                "model_role": ModelRole.SMALL.value,
                "contract_met": True,
            }
            event_codes = ()
        else:
            facts = {}
            event_codes = ()
    return ExecutionStep(
        sequence=sequence,
        step_type=step_type,
        status=ExecutionStatus.SUCCEEDED,
        latency_ms=10,
        event_codes=event_codes,
        facts=facts,
    )


def evaluation_step(
    sequence: int,
    evaluation: EvaluationResult,
    *,
    role: ModelRole = ModelRole.SMALL,
) -> ExecutionStep:
    """Build one successful evaluation step from its recorded evidence."""
    event_codes: tuple[ExecutionEventCode, ...] = ()
    if evaluation.passed:
        event_codes = (ExecutionEventCode.QUALITY_CONTRACT_MET,)
    else:
        if evaluation.evaluator_valid and evaluation.score < evaluation.threshold:
            event_codes += (ExecutionEventCode.QUALITY_THRESHOLD_NOT_MET,)
        if role is ModelRole.STRONG and evaluation.evaluator_valid:
            event_codes += (ExecutionEventCode.FINAL_QUALITY_CONTRACT_NOT_MET,)
    return ExecutionStep(
        sequence=sequence,
        step_type=ExecutionStepType.QUALITY_EVALUATION,
        status=ExecutionStatus.SUCCEEDED,
        latency_ms=10,
        event_codes=event_codes,
        facts={
            "model_role": role.value,
            "evaluator_type": evaluation.evaluator_type,
            "evaluator_valid": evaluation.evaluator_valid,
            "score": evaluation.score,
            "threshold": evaluation.threshold,
            "passed": evaluation.passed,
        },
    )


def unsuccessful_step(
    sequence: int,
    step_type: ExecutionStepType,
    status: ExecutionStatus,
) -> ExecutionStep:
    """Build one failed, timed-out, or skipped execution trace step."""
    facts: dict[str, JsonValue]
    facts = (
        {"model_role": ModelRole.SMALL.value}
        if step_type
        in {
            ExecutionStepType.MODEL_CALL,
            ExecutionStepType.QUALITY_EVALUATION,
            ExecutionStepType.RETURN,
        }
        else {}
    )
    return ExecutionStep(
        sequence=sequence,
        step_type=step_type,
        status=status,
        latency_ms=10,
        facts=facts,
        error=(
            "Step did not complete"
            if status in {ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT}
            else None
        ),
    )


def strong_direct_model_step(
    sequence: int,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    *,
    model_role: str | None = ModelRole.STRONG.value,
    request_id: str = "provider-request-1",
) -> ExecutionStep:
    """Build one strong-direct model-call step with explicit role facts."""
    return strong_direct_role_step(
        sequence,
        ExecutionStepType.MODEL_CALL,
        status,
        model_role=model_role,
        request_id=request_id,
    )


def strong_direct_role_step(
    sequence: int,
    step_type: ExecutionStepType,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    *,
    model_role: str | None = ModelRole.STRONG.value,
    contract_met: bool = True,
    request_id: str = "provider-request-1",
) -> ExecutionStep:
    """Build one strong-direct step with explicit role facts."""
    step = (
        successful_step(sequence, step_type)
        if status is ExecutionStatus.SUCCEEDED
        else unsuccessful_step(sequence, step_type, status)
    )
    facts: dict[str, JsonValue] = {}
    if model_role is not None:
        if step_type is ExecutionStepType.MODEL_CALL:
            facts = {
                "model_role": model_role,
                "provider": "foundry",
                "deployment": "strong-deployment",
                "request_id": request_id,
            }
        elif step_type is ExecutionStepType.QUALITY_EVALUATION:
            facts = {
                "model_role": model_role,
                "evaluator_type": "deterministic",
                "evaluator_valid": True,
                "score": 0.93,
                "threshold": 0.90,
                "passed": True,
            }
        else:
            facts = {"model_role": model_role}
        if step_type is ExecutionStepType.RETURN:
            facts["contract_met"] = contract_met
    return step.model_copy(update={"facts": facts})


def model_usage(
    *,
    request_id: str | None = "provider-request-1",
    run_id: str = "run-1",
    model_role: ModelRole = ModelRole.SMALL,
    input_tokens: int | None = 100,
    output_tokens: int | None = 20,
    provider_total_tokens: int | None = None,
    calculated_cost: Decimal | None = None,
    pricing_provenance: PricingProvenance | None = None,
) -> ModelUsage:
    """Build one measured model-call usage record."""
    authoritative_provenance = pricing_provenance
    if calculated_cost is not None and authoritative_provenance is None:
        authoritative_provenance = PricingProvenance(
            catalog_version="test-v1",
            currency="TEST",
        )
    return ModelUsage(
        request_id=request_id,
        run_id=run_id,
        provider="foundry",
        deployment=f"{model_role.value.lower()}-deployment",
        model_role=model_role,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider_total_tokens=provider_total_tokens,
        latency_ms=125,
        calculated_cost=calculated_cost,
        pricing_provenance=authoritative_provenance,
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
        "request_binding": cache_request_binding(),
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
        pricing_provenance=PricingProvenance(
            catalog_version="test-v1",
            currency="TEST",
        ),
    )

    assert usage.calculated_cost == calculated_cost
    assert usage.pricing_provenance == PricingProvenance(
        catalog_version="test-v1",
        currency="TEST",
    )
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
    assert usage.pricing_provenance is None
    assert usage.cached_tokens is None


def test_model_usage_preserves_unavailable_tokens_and_provider_total() -> None:
    """Keep absent token categories unavailable without discarding a reported total."""
    usage = ModelUsage(
        request_id="provider-request-1",
        run_id="run-1",
        provider="foundry",
        deployment="small-deployment",
        model_role=ModelRole.SMALL,
        provider_total_tokens=37,
        latency_ms=125,
    )

    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.provider_total_tokens == 37
    assert usage.cached_tokens is None


@pytest.mark.parametrize(
    ("calculated_cost", "pricing_provenance"),
    [
        (Decimal("0.001"), None),
        (
            None,
            PricingProvenance(catalog_version="test-v1", currency="TEST"),
        ),
    ],
)
def test_model_usage_rejects_cost_without_matching_provenance(
    calculated_cost: Decimal | None,
    pricing_provenance: PricingProvenance | None,
) -> None:
    """Prevent an amount or catalog assertion from appearing independently."""
    with pytest.raises(ValidationError, match="must be provided together"):
        ModelUsage(
            request_id="provider-request-1",
            run_id="run-1",
            provider="foundry",
            deployment="small-deployment",
            model_role=ModelRole.SMALL,
            input_tokens=100,
            output_tokens=20,
            latency_ms=125,
            calculated_cost=calculated_cost,
            pricing_provenance=pricing_provenance,
        )


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


def test_reduce_run_requires_one_leading_step_and_matching_model_source() -> None:
    """Accept a trace only when its model context source matches applied evidence."""
    result = completed_run(
        execution_plan=reduction_execution_plan(),
        steps=(
            applied_reduction_step(),
            successful_step(1, ExecutionStepType.MODEL_CALL).model_copy(
                update={"context_source": ContextSource.REDUCED}
            ),
            successful_step(2, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(3, ExecutionStepType.RETURN),
        ),
    )

    assert result.steps[0].context_reduction is not None
    assert result.steps[1].context_source is ContextSource.REDUCED


def test_reduce_run_rejects_missing_reduction_step() -> None:
    """Prevent a selected reduction plan from omitting runtime evidence."""
    with pytest.raises(ValidationError, match="one leading context-reduction step"):
        completed_run(execution_plan=reduction_execution_plan())


def test_reduce_run_rejects_late_or_duplicate_reduction_steps() -> None:
    """Require the optional optimization to execute exactly once before models."""
    model_step = successful_step(0, ExecutionStepType.MODEL_CALL).model_copy(
        update={"context_source": ContextSource.REDUCED}
    )
    late_steps = (
        model_step,
        applied_reduction_step(1),
        successful_step(2, ExecutionStepType.QUALITY_EVALUATION),
        successful_step(3, ExecutionStepType.RETURN),
    )
    duplicate_steps = (
        applied_reduction_step(0),
        applied_reduction_step(1),
        successful_step(2, ExecutionStepType.MODEL_CALL).model_copy(
            update={"context_source": ContextSource.REDUCED}
        ),
        successful_step(3, ExecutionStepType.QUALITY_EVALUATION),
        successful_step(4, ExecutionStepType.RETURN),
    )

    with pytest.raises(ValidationError, match="one leading context-reduction step"):
        completed_run(execution_plan=reduction_execution_plan(), steps=late_steps)
    with pytest.raises(ValidationError, match="one leading context-reduction step"):
        completed_run(execution_plan=reduction_execution_plan(), steps=duplicate_steps)


def test_reduce_run_rejects_model_context_source_mismatch() -> None:
    """Prevent applied evidence when a model step reports original context."""
    with pytest.raises(ValidationError, match="must match reduction outcome"):
        completed_run(
            execution_plan=reduction_execution_plan(),
            steps=(
                applied_reduction_step(),
                successful_step(1, ExecutionStepType.MODEL_CALL).model_copy(
                    update={"context_source": ContextSource.ORIGINAL}
                ),
                successful_step(2, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(3, ExecutionStepType.RETURN),
            ),
        )


def test_keep_original_run_rejects_reduction_evidence() -> None:
    """Prevent a bypassed module from fabricating a reduction attempt."""
    with pytest.raises(ValidationError, match="cannot record reduction attempts"):
        completed_run(
            steps=(
                applied_reduction_step(),
                successful_step(1, ExecutionStepType.MODEL_CALL),
                successful_step(2, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(3, ExecutionStepType.RETURN),
            )
        )


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
    assert result.total_cost_provenance == PricingProvenance(
        catalog_version="test-v1",
        currency="TEST",
    )


def test_run_prefers_each_provider_reported_total_without_losing_categories() -> None:
    """Use a provider total consistent with categories as the exact call total."""
    result = completed_run(
        model_usages=(
            model_usage(
                input_tokens=101,
                output_tokens=19,
                provider_total_tokens=120,
            ),
        )
    )

    assert result.total_input_tokens == 101
    assert result.total_output_tokens == 19
    assert result.total_tokens == 120


def test_model_usage_rejects_total_inconsistent_with_reported_categories() -> None:
    """Reject a provider total that contradicts both reported token categories."""
    with pytest.raises(
        ValidationError,
        match="provider_total_tokens must equal input_tokens plus output_tokens",
    ):
        model_usage(input_tokens=101, output_tokens=19, provider_total_tokens=125)


def test_run_keeps_missing_token_categories_and_total_unavailable() -> None:
    """Do not turn a missing provider measurement into a zero or partial total."""
    result = completed_run(
        model_usages=(model_usage(input_tokens=None, output_tokens=19),)
    )

    assert result.total_input_tokens is None
    assert result.total_output_tokens == 19
    assert result.total_tokens is None
    assert result.total_calculated_cost is None


def test_model_usage_allows_absent_request_correlation_id() -> None:
    """Keep the request-correlation id unavailable instead of fabricating one."""
    usage = model_usage(request_id=None)

    assert usage.request_id is None
    assert usage.model_dump(mode="json")["request_id"] is None


def test_run_binds_model_call_without_request_correlation_id() -> None:
    """Bind a model-call step and serialization to usage with no request id."""
    result = completed_run(
        steps=(
            successful_step(0, ExecutionStepType.MODEL_CALL, request_id=None),
            successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(2, ExecutionStepType.RETURN),
        ),
        model_usages=(model_usage(request_id=None),),
    )

    assert result.model_usages[0].request_id is None
    assert result.steps[0].facts["request_id"] is None
    assert result.model_dump(mode="json")["model_usages"][0]["request_id"] is None


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
            evaluation_step(1, small_evaluation),
            successful_step(2, ExecutionStepType.ESCALATION),
            strong_direct_model_step(3, request_id="provider-request-2"),
            evaluation_step(4, final_evaluation, role=ModelRole.STRONG),
            strong_direct_role_step(5, ExecutionStepType.RETURN),
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
    assert result.total_cost_provenance == PricingProvenance(
        catalog_version="test-v1",
        currency="TEST",
    )


def test_run_does_not_convert_unavailable_cost_to_zero() -> None:
    """Keep total cost unavailable when an executed call has no measured cost."""
    result = completed_run(
        model_usages=(
            model_usage(input_tokens=9, output_tokens=4, calculated_cost=None),
        )
    )

    assert result.total_tokens == 13
    assert result.total_calculated_cost is None
    assert result.total_cost_provenance is None


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
            evaluation_step(1, first_evaluation),
            successful_step(2, ExecutionStepType.ESCALATION),
            strong_direct_model_step(3, request_id="provider-request-2"),
            evaluation_step(4, final_evaluation, role=ModelRole.STRONG),
            strong_direct_role_step(5, ExecutionStepType.RETURN),
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
    assert result.total_cost_provenance is None


def test_run_rejects_incompatible_calculated_cost_provenance() -> None:
    """Prevent one run from aggregating different currencies or catalogs."""
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

    with pytest.raises(ValidationError, match="compatible provenance"):
        completed_run(
            steps=(
                successful_step(0, ExecutionStepType.MODEL_CALL),
                evaluation_step(1, first_evaluation),
                successful_step(2, ExecutionStepType.ESCALATION),
                strong_direct_model_step(3, request_id="provider-request-2"),
                evaluation_step(4, final_evaluation, role=ModelRole.STRONG),
                strong_direct_role_step(5, ExecutionStepType.RETURN),
            ),
            model_usages=(
                model_usage(calculated_cost=Decimal("0.0011")),
                model_usage(
                    request_id="provider-request-2",
                    model_role=ModelRole.STRONG,
                    calculated_cost=Decimal("0.0022"),
                    pricing_provenance=PricingProvenance(
                        catalog_version="test-v2",
                        currency="TEST",
                    ),
                ),
            ),
            evaluations=(first_evaluation, final_evaluation),
            final_evaluation=final_evaluation,
            escalated=True,
        )


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
        execution_plan=strong_direct_execution_plan(),
        steps=(
            strong_direct_model_step(0),
            evaluation_step(1, failed_evaluation, role=ModelRole.STRONG),
            strong_direct_role_step(
                2,
                ExecutionStepType.RETURN,
                contract_met=False,
            ),
        ),
        model_usages=(model_usage(model_role=ModelRole.STRONG),),
        evaluations=(failed_evaluation,),
        final_evaluation=failed_evaluation,
        contract_met=False,
    )

    assert result.contract_met is False


def completed_semantic_cache_run(
    *,
    embedding_usage: EmbeddingUsage | None = None,
) -> RunResult:
    """Build accepted cache reuse with compatible evaluation evidence."""
    request_binding = cache_request_binding()
    source_evaluation = EvaluationResult(
        evaluator_type="source-deterministic",
        evaluator_valid=True,
        score=0.93,
        threshold=0.80,
        mandatory_checks_passed=True,
        passed=True,
        reasons=("Source contract passed",),
        metadata={"source_run_id": "run-source-1"},
    )
    candidate = CacheCandidate(
        source_run_id="run-source-1",
        output_text="Cached final answer",
        request_binding=request_binding,
        similarity=0.97,
        prior_evaluation=source_evaluation,
        contract_compatible=True,
        safe_to_reuse=True,
    )
    assessment = CacheCandidateAssessment.from_candidate(candidate)
    cache_plan = ExecutionPlan(
        cache_policy=CachePolicy.USE_CACHED_RESULT,
        context_policy=ContextPolicy.NOT_APPLICABLE,
        verification_required=False,
        optimization_mode=OptimizationMode.COST,
        quality_profile=QualityProfile.HIGH,
        reason_codes=(
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
            PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        ),
        human_readable_name="Cached Result",
        decision_evidence=decision_evidence(
            base_model_policy=None,
            final_model_policy=None,
            cache_candidate_assessed=True,
        ),
        cache_candidate=candidate,
        cache_candidate_assessment=assessment,
        request_binding=request_binding,
    )
    cache_evidence = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.REUSED,
        lookup_latency_ms=4,
        planner_reason_code=PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        source_run_id=candidate.source_run_id,
        similarity=candidate.similarity,
        prior_evaluation=source_evaluation,
        candidate_assessment=assessment,
        embedding_usage=embedding_usage,
    )

    result = completed_run(
        execution_plan=cache_plan,
        request_profile=request_profile().model_copy(update={"cache_eligible": True}),
        request_binding=request_binding,
        steps=(
            ExecutionStep(
                sequence=0,
                step_type=ExecutionStepType.SEMANTIC_CACHE,
                status=ExecutionStatus.SUCCEEDED,
                latency_ms=4,
                event_codes=(
                    ExecutionEventCode.CACHE_RESULT_REUSED,
                    ExecutionEventCode.QUALITY_CONTRACT_MET,
                ),
                semantic_cache=cache_evidence,
            ),
            successful_step(1, ExecutionStepType.RETURN).model_copy(
                update={
                    "facts": {
                        "contract_met": True,
                        "source_run_id": candidate.source_run_id,
                    }
                }
            ),
        ),
        semantic_cache=cache_evidence,
        model_usages=(),
        evaluations=(),
        final_evaluation=None,
        final_output=candidate.output_text,
        contract_met=True,
    )
    return result


def test_completed_semantic_cache_run_has_no_model_usage() -> None:
    """Represent accepted cache reuse with compatible evaluation evidence."""
    result = completed_semantic_cache_run()
    candidate = result.execution_plan.cache_candidate
    assert candidate is not None
    source_evaluation = candidate.prior_evaluation

    assert result.execution_plan.cache_policy is CachePolicy.USE_CACHED_RESULT
    assert result.model_usages == ()
    assert result.evaluations == ()
    assert result.final_evaluation is None
    assert result.semantic_cache is not None
    assert result.semantic_cache.prior_evaluation == source_evaluation
    assert result.semantic_cache.prior_evaluation.threshold == 0.80
    assert result.total_input_tokens == 0
    assert result.total_output_tokens == 0
    assert result.total_tokens == 0
    assert result.total_calculated_cost is None
    assert result.total_cost_provenance is None


def test_cache_hit_totals_include_priced_embedding_usage() -> None:
    """A cache hit must report the embedding tokens and cost it consumed."""
    provenance = PricingProvenance(catalog_version="catalog-v1", currency="USD")
    usage = EmbeddingUsage(
        run_id="run-1",
        provider="microsoft-foundry-apim",
        deployment="optima-embed",
        embedding_profile="profile-hash",
        input_tokens=12,
        latency_ms=3,
        calculated_cost=Decimal("0.00004"),
        pricing_provenance=provenance,
    )

    result = completed_semantic_cache_run(embedding_usage=usage)

    assert result.total_input_tokens == 12
    assert result.total_output_tokens == 0
    assert result.total_tokens == 12
    assert result.total_calculated_cost == Decimal("0.00004")
    assert result.total_cost_provenance == provenance


def test_cache_hit_without_embedding_cost_reports_incomplete_cost() -> None:
    """Do not fabricate a zero cost when embedding usage lacks pricing."""
    usage = EmbeddingUsage(
        run_id="run-1",
        provider="microsoft-foundry-apim",
        deployment="optima-embed",
        embedding_profile="profile-hash",
        input_tokens=12,
        latency_ms=3,
    )

    result = completed_semantic_cache_run(embedding_usage=usage)

    assert result.total_tokens == 12
    assert result.total_calculated_cost is None
    assert result.total_cost_provenance is None


def test_cache_hit_without_embedding_tokens_reports_unknown_tokens() -> None:
    """Report unknown token totals when the embedding usage omits tokens."""
    usage = EmbeddingUsage(
        run_id="run-1",
        provider="microsoft-foundry-apim",
        deployment="optima-embed",
        embedding_profile="profile-hash",
        latency_ms=3,
    )

    result = completed_semantic_cache_run(embedding_usage=usage)

    assert result.total_input_tokens is None
    assert result.total_tokens is None


def test_run_rejects_removed_miss_evidence_and_cache_step() -> None:
    """Require lookup evidence for every enabled cache-eligible model run."""
    miss = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.MISS,
        lookup_latency_ms=4,
        planner_reason_code=PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED,
    )
    plan = execution_plan().model_copy(
        update={
            "reason_codes": (
                PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED,
                PlannerReasonCode.OPTIMIZATION_MODE_COST,
                PlannerReasonCode.SMALL_FIRST_SELECTED,
            )
        }
    )
    result = completed_run(
        execution_plan=plan,
        request_profile=request_profile().model_copy(update={"cache_eligible": True}),
        semantic_cache=miss,
        steps=(
            ExecutionStep(
                sequence=0,
                step_type=ExecutionStepType.SEMANTIC_CACHE,
                status=ExecutionStatus.SUCCEEDED,
                latency_ms=4,
                event_codes=(ExecutionEventCode.CACHE_MISS,),
                semantic_cache=miss,
            ),
            successful_step(1, ExecutionStepType.MODEL_CALL),
            successful_step(2, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(3, ExecutionStepType.RETURN),
        ),
    )
    forged = result.model_dump(mode="json", exclude_computed_fields=True)
    forged["semantic_cache"] = None
    forged["steps"] = forged["steps"][1:]
    for sequence, step in enumerate(forged["steps"]):
        step["sequence"] = sequence

    with pytest.raises(ValidationError, match="cache outcome evidence"):
        RunResult.model_validate(forged)


def test_run_rejects_cache_miss_without_model_call() -> None:
    """Require normal small-first model execution after a cache miss."""
    miss = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.MISS,
        lookup_latency_ms=4,
        planner_reason_code=PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED,
    )
    plan = execution_plan().model_copy(
        update={
            "reason_codes": (
                PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED,
                PlannerReasonCode.OPTIMIZATION_MODE_COST,
                PlannerReasonCode.SMALL_FIRST_SELECTED,
            )
        }
    )
    result = completed_run(
        request_profile=request_profile().model_copy(update={"cache_eligible": True}),
        execution_plan=plan,
        semantic_cache=miss,
        steps=(
            ExecutionStep(
                sequence=0,
                step_type=ExecutionStepType.SEMANTIC_CACHE,
                status=ExecutionStatus.SUCCEEDED,
                latency_ms=4,
                event_codes=(ExecutionEventCode.CACHE_MISS,),
                semantic_cache=miss,
            ),
            successful_step(1, ExecutionStepType.MODEL_CALL),
            successful_step(2, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(3, ExecutionStepType.RETURN),
        ),
    )
    forged = result.model_dump(mode="json", exclude_computed_fields=True)
    forged["steps"] = [forged["steps"][0], *forged["steps"][2:]]
    forged["model_usages"] = []
    for sequence, step in enumerate(forged["steps"]):
        step["sequence"] = sequence

    with pytest.raises(ValidationError, match="small-first"):
        RunResult.model_validate(forged)


def test_run_rejects_cache_events_outside_cache_step() -> None:
    """Keep cache outcome events exclusive to the leading cache step."""
    hit = completed_semantic_cache_run().model_dump(
        mode="json",
        exclude_computed_fields=True,
    )
    hit["steps"][1]["event_codes"] = [ExecutionEventCode.CACHE_MISS]

    with pytest.raises(ValidationError, match="cache event codes"):
        RunResult.model_validate(hit)

    miss = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.MISS,
        lookup_latency_ms=4,
        planner_reason_code=PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED,
    )
    plan = execution_plan().model_copy(
        update={
            "reason_codes": (
                PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED,
                PlannerReasonCode.OPTIMIZATION_MODE_COST,
                PlannerReasonCode.SMALL_FIRST_SELECTED,
            )
        }
    )
    miss_result = completed_run(
        request_profile=request_profile().model_copy(update={"cache_eligible": True}),
        execution_plan=plan,
        semantic_cache=miss,
        steps=(
            ExecutionStep(
                sequence=0,
                step_type=ExecutionStepType.SEMANTIC_CACHE,
                status=ExecutionStatus.SUCCEEDED,
                latency_ms=4,
                event_codes=(ExecutionEventCode.CACHE_MISS,),
                semantic_cache=miss,
            ),
            successful_step(1, ExecutionStepType.MODEL_CALL),
            successful_step(2, ExecutionStepType.QUALITY_EVALUATION),
            successful_step(3, ExecutionStepType.RETURN),
        ),
    ).model_dump(mode="json", exclude_computed_fields=True)
    miss_result["steps"][1]["event_codes"] = [ExecutionEventCode.CACHE_RESULT_REUSED]

    with pytest.raises(ValidationError, match="cache event codes"):
        RunResult.model_validate(miss_result)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            {
                "request_profile": request_profile().model_copy(
                    update={"cache_eligible": False}
                )
            },
            "eligib",
        ),
        (
            {
                "steps": (
                    ExecutionStep(
                        sequence=0,
                        step_type=ExecutionStepType.SEMANTIC_CACHE,
                        status=ExecutionStatus.SUCCEEDED,
                        latency_ms=4,
                        event_codes=(
                            ExecutionEventCode.CACHE_RESULT_REUSED,
                            ExecutionEventCode.QUALITY_CONTRACT_MET,
                        ),
                        semantic_cache=SemanticCacheEvidence(
                            outcome=SemanticCacheOutcome.REUSED,
                            lookup_latency_ms=4,
                            planner_reason_code=(
                                PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH
                            ),
                            source_run_id="run-source-1",
                            similarity=0.97,
                            prior_evaluation=EvaluationResult(
                                evaluator_type="source-deterministic",
                                evaluator_valid=True,
                                score=0.93,
                                threshold=0.80,
                                mandatory_checks_passed=True,
                                passed=True,
                                reasons=("Source contract passed",),
                            ),
                        ),
                    ),
                    unsuccessful_step(
                        1,
                        ExecutionStepType.RETURN,
                        ExecutionStatus.FAILED,
                    ),
                )
            },
            "cache step",
        ),
    ],
)
def test_cache_hit_rejects_forged_terminal_or_profile_facts(
    mutation: dict[str, object],
    message: str,
) -> None:
    """Reject cache hits with ineligible profiles or unsuccessful returns."""
    request_binding = cache_request_binding()
    source_evaluation = EvaluationResult(
        evaluator_type="source-deterministic",
        evaluator_valid=True,
        score=0.93,
        threshold=0.80,
        mandatory_checks_passed=True,
        passed=True,
        reasons=("Source contract passed",),
    )
    candidate = CacheCandidate(
        source_run_id="run-source-1",
        output_text="Cached final answer",
        request_binding=request_binding,
        similarity=0.97,
        prior_evaluation=source_evaluation,
        contract_compatible=True,
        safe_to_reuse=True,
    )
    assessment = CacheCandidateAssessment.from_candidate(candidate)
    cache_evidence = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.REUSED,
        lookup_latency_ms=4,
        planner_reason_code=PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        source_run_id=candidate.source_run_id,
        similarity=candidate.similarity,
        prior_evaluation=source_evaluation,
        candidate_assessment=assessment,
    )
    plan = ExecutionPlan(
        cache_policy=CachePolicy.USE_CACHED_RESULT,
        context_policy=ContextPolicy.NOT_APPLICABLE,
        verification_required=False,
        optimization_mode=OptimizationMode.COST,
        quality_profile=QualityProfile.HIGH,
        reason_codes=(
            PlannerReasonCode.OPTIMIZATION_MODE_COST,
            PlannerReasonCode.CACHE_HIGH_CONFIDENCE_MATCH,
        ),
        human_readable_name="Cached Result",
        decision_evidence=decision_evidence(
            base_model_policy=None,
            final_model_policy=None,
            cache_candidate_assessed=True,
        ),
        cache_candidate=candidate,
        cache_candidate_assessment=assessment,
        request_binding=request_binding,
    )
    values: dict[str, object] = {
        "execution_plan": plan,
        "request_profile": request_profile().model_copy(
            update={"cache_eligible": True}
        ),
        "request_binding": request_binding,
        "semantic_cache": cache_evidence,
        "steps": (
            ExecutionStep(
                sequence=0,
                step_type=ExecutionStepType.SEMANTIC_CACHE,
                status=ExecutionStatus.SUCCEEDED,
                latency_ms=4,
                event_codes=(
                    ExecutionEventCode.CACHE_RESULT_REUSED,
                    ExecutionEventCode.QUALITY_CONTRACT_MET,
                ),
                semantic_cache=cache_evidence,
            ),
            successful_step(1, ExecutionStepType.RETURN),
        ),
        "model_usages": (),
        "evaluations": (),
        "final_evaluation": None,
        "final_output": candidate.output_text,
        "contract_met": True,
    }
    values.update(mutation)

    with pytest.raises(ValidationError, match=message):
        completed_run(**values)


def test_model_run_rejects_cache_evidence_with_different_planner_reason() -> None:
    """Prevent transported runtime evidence from contradicting Planner V1."""
    cache_evidence = SemanticCacheEvidence(
        outcome=SemanticCacheOutcome.MISS,
        lookup_latency_ms=2,
        planner_reason_code=PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED,
    )

    with pytest.raises(ValidationError, match="reason must appear"):
        completed_run(
            request_profile=request_profile().model_copy(
                update={"cache_eligible": True}
            ),
            semantic_cache=cache_evidence,
            steps=(
                ExecutionStep(
                    sequence=0,
                    step_type=ExecutionStepType.SEMANTIC_CACHE,
                    status=ExecutionStatus.SUCCEEDED,
                    latency_ms=2,
                    event_codes=(ExecutionEventCode.CACHE_MISS,),
                    semantic_cache=cache_evidence,
                ),
                successful_step(1, ExecutionStepType.MODEL_CALL),
                successful_step(2, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(3, ExecutionStepType.RETURN),
            ),
        )


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
            evaluation_step(1, first_evaluation),
            successful_step(2, ExecutionStepType.ESCALATION),
            strong_direct_model_step(3, request_id="provider-request-2"),
            evaluation_step(4, final_evaluation, role=ModelRole.STRONG),
            strong_direct_role_step(5, ExecutionStepType.RETURN),
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


def test_run_rejects_non_contiguous_step_sequences() -> None:
    """Require executor-emitted zero-based contiguous trace sequences."""
    with pytest.raises(ValidationError, match="contiguous"):
        completed_run(
            steps=(
                successful_step(10, ExecutionStepType.MODEL_CALL),
                successful_step(20, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(30, ExecutionStepType.RETURN),
            )
        )


def test_small_first_run_rejects_strong_only_execution() -> None:
    """Require a small-first trace and usage history to begin with SMALL."""
    with pytest.raises(ValidationError, match="SMALL"):
        completed_run(
            steps=(
                strong_direct_model_step(0),
                strong_direct_role_step(1, ExecutionStepType.QUALITY_EVALUATION),
                strong_direct_role_step(2, ExecutionStepType.RETURN),
            ),
            model_usages=(model_usage(model_role=ModelRole.STRONG),),
        )


def test_interrupted_small_first_run_rejects_strong_only_execution() -> None:
    """Apply SMALL-first causality even when execution is interrupted."""
    with pytest.raises(ValidationError, match="SMALL"):
        completed_run(
            status=RunStatus.FAILED,
            steps=(strong_direct_model_step(0, ExecutionStatus.FAILED),),
            model_usages=(),
            evaluations=(),
            final_evaluation=None,
            final_output=None,
            contract_met=None,
            error="Strong provider failed",
        )


def test_nested_run_evidence_model_copy_revalidates_updates() -> None:
    """Reject invalid nested evidence created through Pydantic model_copy."""
    with pytest.raises(ValidationError):
        PricingProvenance(
            catalog_version="test-v1",
            currency="TEST",
        ).model_copy(update={"currency": ""})


def test_run_model_copy_revalidates_constructed_nested_plan() -> None:
    """Reject unchecked nested plan state supplied to parent model_copy."""
    plan = execution_plan()
    plan_values = {
        field_name: getattr(plan, field_name) for field_name in type(plan).model_fields
    }
    plan_values["human_readable_name"] = ""
    invalid_plan = ExecutionPlan.model_construct(**plan_values)

    with pytest.raises(ValidationError, match="human_readable_name"):
        completed_run().model_copy(update={"execution_plan": invalid_plan})


def test_completed_run_rejects_failed_terminal_return() -> None:
    """Require a completed model trace to end in a successful return."""
    with pytest.raises(ValidationError, match="terminal return"):
        completed_run(
            steps=(
                successful_step(0, ExecutionStepType.MODEL_CALL),
                successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
                unsuccessful_step(
                    2,
                    ExecutionStepType.RETURN,
                    ExecutionStatus.FAILED,
                ),
            )
        )


def test_interrupted_run_rejects_successful_terminal_return() -> None:
    """Prevent interrupted runs from claiming a successful return."""
    with pytest.raises(ValidationError, match="completed run"):
        interrupted_run(
            status=RunStatus.FAILED,
            steps=(
                successful_step(0, ExecutionStepType.MODEL_CALL),
                successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(2, ExecutionStepType.RETURN),
            ),
            model_usages=(model_usage(),),
            evaluations=(passing_evaluation(),),
        )


def test_completed_run_rejects_contradictory_return_contract_fact() -> None:
    """Bind terminal return facts to final quality evidence."""
    terminal = successful_step(2, ExecutionStepType.RETURN).model_copy(
        update={
            "facts": {
                "model_role": ModelRole.SMALL.value,
                "contract_met": False,
            }
        }
    )

    with pytest.raises(ValidationError, match="return facts"):
        completed_run(
            steps=(
                successful_step(0, ExecutionStepType.MODEL_CALL),
                successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
                terminal,
            )
        )


def test_run_rejects_plan_optimization_mode_mismatch() -> None:
    """Bind persisted plans to the run Quality Contract mode."""
    with pytest.raises(ValidationError, match="Optimization Mode"):
        completed_run(
            quality_contract=quality_contract().model_copy(
                update={"optimization_mode": OptimizationMode.QUALITY}
            )
        )


@pytest.mark.parametrize(
    ("run_status", "step_status"),
    [
        (RunStatus.FAILED, ExecutionStatus.TIMED_OUT),
        (RunStatus.TIMED_OUT, ExecutionStatus.FAILED),
        (RunStatus.FAILED, ExecutionStatus.SKIPPED),
    ],
)
def test_interrupted_run_rejects_terminal_status_mismatch(
    run_status: RunStatus,
    step_status: ExecutionStatus,
) -> None:
    """Bind the final interrupted step status to the top-level run status."""
    step = (
        successful_step(0, ExecutionStepType.MODEL_CALL).model_copy(
            update={"status": ExecutionStatus.SKIPPED}
        )
        if step_status is ExecutionStatus.SKIPPED
        else unsuccessful_step(0, ExecutionStepType.MODEL_CALL, step_status)
    )

    with pytest.raises(ValidationError, match="run status"):
        interrupted_run(status=run_status, steps=(step,))


def test_run_rejects_evaluation_step_facts_that_contradict_evidence() -> None:
    """Bind successful evaluation trace facts and events to recorded evidence."""
    forged = successful_step(1, ExecutionStepType.QUALITY_EVALUATION).model_copy(
        update={
            "event_codes": (ExecutionEventCode.FINAL_QUALITY_CONTRACT_NOT_MET,),
            "facts": {
                "model_role": ModelRole.SMALL.value,
                "evaluator_type": "forged",
                "evaluator_valid": True,
                "score": 0.0,
                "threshold": 0.90,
                "passed": False,
            },
        }
    )

    with pytest.raises(ValidationError, match="evaluation step"):
        completed_run(
            steps=(
                successful_step(0, ExecutionStepType.MODEL_CALL),
                forged,
                successful_step(2, ExecutionStepType.RETURN),
            )
        )


def test_run_rejects_evaluation_after_failed_model_call() -> None:
    """Do not evaluate or return a candidate that no model call produced."""
    with pytest.raises(ValidationError, match="requires a successful model call"):
        completed_run(
            steps=(
                unsuccessful_step(
                    0,
                    ExecutionStepType.MODEL_CALL,
                    ExecutionStatus.FAILED,
                ),
                successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(2, ExecutionStepType.RETURN),
            )
        )


def test_small_first_run_rejects_escalation_before_small_evaluation() -> None:
    """Require escalation only after one unsuccessful SMALL evaluation."""
    first_evaluation = passing_evaluation().model_copy(
        update={"score": 0.80, "passed": False}
    )
    with pytest.raises(ValidationError, match="escalation"):
        completed_run(
            steps=(
                successful_step(0, ExecutionStepType.ESCALATION),
                successful_step(1, ExecutionStepType.MODEL_CALL),
                successful_step(2, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(3, ExecutionStepType.MODEL_CALL),
                successful_step(4, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(5, ExecutionStepType.RETURN),
            ),
            model_usages=(
                model_usage(),
                model_usage(request_id="provider-request-2"),
            ),
            evaluations=(first_evaluation, passing_evaluation()),
            escalated=True,
        )


def test_cache_hit_rejects_request_profile_rebinding() -> None:
    """Keep cache-hit request identity bound to profile facts in the result."""
    forged = completed_semantic_cache_run().model_dump(
        mode="json",
        exclude_computed_fields=True,
    )
    forged["request_profile"]["task_type"] = TaskType.GENERAL_REASONING
    forged["request_profile"]["complexity"] = Complexity.HIGH

    with pytest.raises(ValidationError, match="request binding"):
        RunResult.model_validate(forged)


def test_cache_hit_rejects_contradictory_cache_reason() -> None:
    """Allow only the accepted-match cache reason on a reuse plan."""
    result = completed_semantic_cache_run()
    with pytest.raises(ValidationError, match="cache reason"):
        result.execution_plan.model_copy(
            update={
                "reason_codes": (
                    *result.execution_plan.reason_codes,
                    PlannerReasonCode.CACHE_REUSE_UNSAFE,
                )
            }
        )


def test_cache_hit_rejects_forged_return_evidence() -> None:
    """Bind the terminal return facts and events to accepted cache reuse."""
    forged = completed_semantic_cache_run().model_dump(
        mode="json",
        exclude_computed_fields=True,
    )
    forged["steps"][1]["facts"] = {
        "contract_met": False,
        "source_run_id": "forged-source",
    }
    forged["steps"][1]["event_codes"] = [ExecutionEventCode.QUALITY_THRESHOLD_NOT_MET]

    with pytest.raises(ValidationError, match="cache return"):
        RunResult.model_validate(forged)


def test_strong_direct_run_rejects_multiple_model_attempts() -> None:
    """Prevent a direct plan from hiding a retry or second model call."""
    with pytest.raises(ValidationError, match="exactly one model-call attempt"):
        completed_run(
            execution_plan=strong_direct_execution_plan(),
            steps=(
                strong_direct_model_step(0),
                strong_direct_model_step(1),
                successful_step(2, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(3, ExecutionStepType.RETURN),
            ),
            model_usages=(
                model_usage(model_role=ModelRole.STRONG),
                model_usage(
                    request_id="provider-request-2",
                    model_role=ModelRole.STRONG,
                ),
            ),
        )


def test_strong_direct_run_rejects_missing_model_attempt() -> None:
    """Require one attempted provider call even for interrupted direct runs."""
    with pytest.raises(ValidationError, match="exactly one model-call attempt"):
        completed_run(
            execution_plan=strong_direct_execution_plan(),
            status=RunStatus.FAILED,
            steps=(
                unsuccessful_step(
                    0,
                    ExecutionStepType.MODEL_CALL,
                    ExecutionStatus.SKIPPED,
                ),
            ),
            model_usages=(),
            evaluations=(),
            final_evaluation=None,
            final_output=None,
            contract_met=None,
            error="Run did not complete",
        )


@pytest.mark.parametrize("model_role", [None, ModelRole.SMALL.value])
def test_strong_direct_run_rejects_non_strong_model_step_facts(
    model_role: str | None,
) -> None:
    """Require every direct model-call trace step to identify STRONG."""
    with pytest.raises(ValidationError, match="STRONG model_role facts"):
        completed_run(
            execution_plan=strong_direct_execution_plan(),
            steps=(
                strong_direct_model_step(0, model_role=model_role),
                successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(2, ExecutionStepType.RETURN),
            ),
            model_usages=(model_usage(model_role=ModelRole.STRONG),),
        )


def test_strong_direct_run_rejects_non_strong_usage() -> None:
    """Require direct-plan usage to come only from the STRONG role."""
    with pytest.raises(ValidationError, match="STRONG model usage"):
        completed_run(
            execution_plan=strong_direct_execution_plan(),
            steps=(
                strong_direct_model_step(0),
                successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(2, ExecutionStepType.RETURN),
            ),
        )


def test_strong_direct_run_accepts_one_strong_call_and_one_evaluation() -> None:
    """Accept the exact successful strong-direct runtime shape."""
    usage = model_usage(model_role=ModelRole.STRONG)
    result = completed_run(
        execution_plan=strong_direct_execution_plan(),
        steps=(
            strong_direct_model_step(0),
            strong_direct_role_step(1, ExecutionStepType.QUALITY_EVALUATION),
            strong_direct_role_step(2, ExecutionStepType.RETURN),
        ),
        model_usages=(usage,),
    )

    assert result.model_usages == (usage,)
    assert len(result.evaluations) == 1
    assert result.escalated is False


@pytest.mark.parametrize(
    ("step_type", "model_role"),
    [
        (ExecutionStepType.QUALITY_EVALUATION, None),
        (ExecutionStepType.QUALITY_EVALUATION, ModelRole.SMALL.value),
        (ExecutionStepType.RETURN, None),
        (ExecutionStepType.RETURN, ModelRole.SMALL.value),
    ],
    ids=[
        "evaluation-missing-role",
        "evaluation-small-role",
        "return-missing-role",
        "return-small-role",
    ],
)
def test_strong_direct_run_rejects_non_strong_evaluation_and_return_facts(
    step_type: ExecutionStepType,
    model_role: str | None,
) -> None:
    """Require direct evaluation and return steps to identify STRONG."""
    evaluation_role = (
        model_role
        if step_type is ExecutionStepType.QUALITY_EVALUATION
        else ModelRole.STRONG.value
    )
    return_role = (
        model_role if step_type is ExecutionStepType.RETURN else ModelRole.STRONG.value
    )

    with pytest.raises(ValidationError, match="STRONG model_role facts"):
        completed_run(
            execution_plan=strong_direct_execution_plan(),
            steps=(
                strong_direct_model_step(0),
                strong_direct_role_step(
                    1,
                    ExecutionStepType.QUALITY_EVALUATION,
                    model_role=evaluation_role,
                ),
                strong_direct_role_step(
                    2,
                    ExecutionStepType.RETURN,
                    model_role=return_role,
                ),
            ),
            model_usages=(model_usage(model_role=ModelRole.STRONG),),
        )


def test_strong_direct_run_rejects_multiple_evaluation_attempts() -> None:
    """Prevent a direct plan from recording evaluator retries."""
    evaluation = passing_evaluation()
    with pytest.raises(ValidationError, match="must match model-call success"):
        completed_run(
            execution_plan=strong_direct_execution_plan(),
            steps=(
                strong_direct_model_step(0),
                strong_direct_role_step(1, ExecutionStepType.QUALITY_EVALUATION),
                strong_direct_role_step(2, ExecutionStepType.QUALITY_EVALUATION),
                strong_direct_role_step(3, ExecutionStepType.RETURN),
            ),
            model_usages=(model_usage(model_role=ModelRole.STRONG),),
            evaluations=(evaluation, evaluation),
            final_evaluation=evaluation,
        )


def test_strong_direct_run_rejects_missing_evaluation_after_successful_call() -> None:
    """Require one evaluation attempt after the direct model call succeeds."""
    with pytest.raises(ValidationError, match="must match model-call success"):
        completed_run(
            execution_plan=strong_direct_execution_plan(),
            status=RunStatus.FAILED,
            steps=(strong_direct_model_step(0),),
            model_usages=(model_usage(model_role=ModelRole.STRONG),),
            evaluations=(),
            final_evaluation=None,
            final_output=None,
            contract_met=None,
            error="Run did not complete",
        )


def test_strong_direct_run_rejects_evaluation_after_failed_provider_call() -> None:
    """Do not record evaluation attempts when STRONG produced no candidate."""
    with pytest.raises(ValidationError, match="must match model-call success"):
        completed_run(
            execution_plan=strong_direct_execution_plan(),
            status=RunStatus.FAILED,
            steps=(
                strong_direct_model_step(0, ExecutionStatus.FAILED),
                strong_direct_role_step(
                    1,
                    ExecutionStepType.QUALITY_EVALUATION,
                    ExecutionStatus.FAILED,
                ),
            ),
            model_usages=(),
            evaluations=(),
            final_evaluation=None,
            final_output=None,
            contract_met=None,
            error="Strong provider failed",
        )


@pytest.mark.parametrize(
    ("run_status", "step_status"),
    [
        (RunStatus.FAILED, ExecutionStatus.FAILED),
        (RunStatus.TIMED_OUT, ExecutionStatus.TIMED_OUT),
    ],
)
def test_strong_direct_run_accepts_provider_interruption_without_evaluation(
    run_status: RunStatus,
    step_status: ExecutionStatus,
) -> None:
    """Keep truthful direct provider failures valid without invented evidence."""
    result = completed_run(
        execution_plan=strong_direct_execution_plan(),
        status=run_status,
        steps=(strong_direct_model_step(0, step_status),),
        model_usages=(),
        evaluations=(),
        final_evaluation=None,
        final_output=None,
        contract_met=None,
        error="Strong provider did not complete",
    )

    assert result.status is run_status
    assert result.model_usages == ()
    assert result.evaluations == ()


@pytest.mark.parametrize(
    ("run_status", "step_status"),
    [
        (RunStatus.FAILED, ExecutionStatus.FAILED),
        (RunStatus.TIMED_OUT, ExecutionStatus.TIMED_OUT),
    ],
)
def test_strong_direct_run_accepts_evaluator_interruption_after_successful_call(
    run_status: RunStatus,
    step_status: ExecutionStatus,
) -> None:
    """Retain completed STRONG usage when its sole evaluation is interrupted."""
    usage = model_usage(model_role=ModelRole.STRONG)
    result = completed_run(
        execution_plan=strong_direct_execution_plan(),
        status=run_status,
        steps=(
            strong_direct_model_step(0),
            strong_direct_role_step(
                1,
                ExecutionStepType.QUALITY_EVALUATION,
                step_status,
            ),
        ),
        model_usages=(usage,),
        evaluations=(),
        final_evaluation=None,
        final_output=None,
        contract_met=None,
        error="Strong evaluation did not complete",
    )

    assert result.model_usages == (usage,)
    assert result.evaluations == ()


def test_strong_direct_run_rejects_escalation_events() -> None:
    """Reject escalation claims even when no escalation step is present."""
    evaluation_step = successful_step(
        1, ExecutionStepType.QUALITY_EVALUATION
    ).model_copy(update={"event_codes": (ExecutionEventCode.ESCALATED_TO_STRONG,)})
    with pytest.raises(ValidationError, match="escalation evidence"):
        completed_run(
            execution_plan=strong_direct_execution_plan(),
            steps=(
                strong_direct_model_step(0),
                evaluation_step,
                successful_step(2, ExecutionStepType.RETURN),
            ),
            model_usages=(model_usage(model_role=ModelRole.STRONG),),
        )


def test_strong_direct_run_rejects_escalation_step() -> None:
    """Reject a direct trace containing an escalation transition."""
    with pytest.raises(ValidationError, match="escalation evidence"):
        completed_run(
            execution_plan=strong_direct_execution_plan(),
            steps=(
                strong_direct_model_step(0),
                successful_step(1, ExecutionStepType.QUALITY_EVALUATION),
                successful_step(2, ExecutionStepType.ESCALATION),
                successful_step(3, ExecutionStepType.RETURN),
            ),
            model_usages=(model_usage(model_role=ModelRole.STRONG),),
            escalated=True,
        )


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
    with pytest.raises(ValidationError, match="contiguous"):
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
            evaluation_step(1, small_evaluation),
            successful_step(2, ExecutionStepType.ESCALATION),
            unsuccessful_step(
                3,
                ExecutionStepType.MODEL_CALL,
                ExecutionStatus.FAILED,
            ).model_copy(update={"facts": {"model_role": ModelRole.STRONG.value}}),
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
    assert result.total_cost_provenance is None


def test_skipped_model_call_cannot_terminate_interrupted_run() -> None:
    """Reject a skipped operation as the claimed cause of run interruption."""
    with pytest.raises(ValidationError, match="run status"):
        interrupted_run(
            status=RunStatus.FAILED,
            steps=(
                unsuccessful_step(
                    0,
                    ExecutionStepType.MODEL_CALL,
                    ExecutionStatus.SKIPPED,
                ),
            ),
        )


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
            successful_step(0, ExecutionStepType.MODEL_CALL),
            unsuccessful_step(
                1,
                ExecutionStepType.QUALITY_EVALUATION,
                step_status,
            ),
        ),
        model_usages=(model_usage(),),
    )

    assert result.evaluations == ()


def test_skipped_evaluation_cannot_terminate_interrupted_run() -> None:
    """Reject a skipped evaluation as the claimed cause of interruption."""
    with pytest.raises(ValidationError, match="run status"):
        interrupted_run(
            status=RunStatus.FAILED,
            steps=(
                successful_step(0, ExecutionStepType.MODEL_CALL),
                unsuccessful_step(
                    1,
                    ExecutionStepType.QUALITY_EVALUATION,
                    ExecutionStatus.SKIPPED,
                ),
            ),
            model_usages=(model_usage(),),
        )


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
