"""Tests for truthful decision, trace, formatting, and history projections."""

from decimal import Decimal

import httpx
from fastapi.testclient import TestClient

from optima.api.demo import app as demo_app
from optima.comparison import (
    BaselineComparisonRequest,
    BaselineComparisonService,
    BenchmarkCaseIdentity,
    ComparableRun,
    ComparisonArm,
)
from optima.domain.execution import ExecutionStatus, PlannerReasonCode
from optima.domain.quality_contract import QualityProfile
from optima.domain.run import PricingProvenance, RunResult, RunStatus
from ui.api_client import OptimaApiClient
from ui.history import (
    HistoryEntry,
    add_entry,
    aggregate_dashboard,
    history_rows,
    select_entry,
)
from ui.models import ExecuteInputs
from ui.presentation import (
    REASON_EXPLANATIONS,
    ContractState,
    attempted_model_call_count,
    context_reduction_view,
    contract_state,
    decision_view,
    format_cost,
    outcome_label,
    trace_rows,
)


def execute_result(**updates: object) -> RunResult:
    """Execute the real local demo API and parse its response through the client."""
    inputs = ExecuteInputs.model_validate({"input_text": "Summarize this", **updates})
    response = TestClient(demo_app).post(
        "/api/v1/runs",
        json=inputs.to_run_request().model_dump(mode="json", exclude_none=True),
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(response.status_code, json=response.json())
    )
    return OptimaApiClient(transport=transport).execute(inputs.to_run_request())


def execute_reduction_result() -> RunResult:
    """Execute one deterministic local request that selects context reduction."""
    return execute_result(
        input_text="Summarize incident requirements",
        context=(
            "Priya Nair owns incident INC-204.\n"
            "INC-204 affected 37 requests.\n"
            "Unrelated social update for the wider team."
        ),
        input_tokens=4_000,
        has_large_context=True,
    )


def failed_reduction_result() -> RunResult:
    """Build a validated completed run that recovered with original context."""
    payload = execute_reduction_result().model_dump(
        mode="json",
        exclude_computed_fields=True,
    )
    reduction_step = payload["steps"][0]
    original_tokens = reduction_step["context_reduction"]["original_token_count"]
    reduction_step.update(
        {
            "status": "FAILED",
            "context_reduction": {
                "outcome": "FAILED_USING_ORIGINAL",
                "original_token_count": original_tokens,
                "effective_token_count": original_tokens,
                "reducer_name": "deterministic-extractive-reducer-v1",
                "method": None,
                "token_counter_name": "regex-token-counter-v1",
                "context_source": "ORIGINAL",
                "preservation": None,
            },
            "error": "Context reduction RuntimeError",
        }
    )
    for step in payload["steps"]:
        if step["step_type"] == "MODEL_CALL":
            step["context_source"] = "ORIGINAL"
    return RunResult.model_validate(payload)


def interrupted_model_call_result(step_status: ExecutionStatus) -> RunResult:
    """Build a validated interrupted run with no usage measurement."""
    payload = execute_result().model_dump(mode="json", exclude_computed_fields=True)
    payload.update(
        {
            "status": (
                RunStatus.TIMED_OUT
                if step_status is ExecutionStatus.TIMED_OUT
                else RunStatus.FAILED
            ),
            "steps": [
                {
                    "sequence": 0,
                    "step_type": "MODEL_CALL",
                    "status": step_status,
                    "latency_ms": 0,
                    "event_codes": [],
                    "facts": {},
                    "error": (
                        None
                        if step_status is ExecutionStatus.SKIPPED
                        else "Provider interrupted"
                    ),
                }
            ],
            "model_usages": [],
            "evaluations": [],
            "final_evaluation": None,
            "final_output": None,
            "contract_met": None,
            "escalated": False,
            "latency_ms": 0,
            "error": "Execution interrupted",
        }
    )
    return RunResult.model_validate(payload)


def test_reason_explanations_cover_every_planner_code() -> None:
    """Require deterministic text whenever Planner V1 adds or returns a code."""
    assert set(REASON_EXPLANATIONS) == set(PlannerReasonCode)


def test_decision_and_trace_preserve_backend_order_and_non_escalation() -> None:
    """Project a passing small-first run without adding execution facts."""
    result = execute_result()
    decision = decision_view(result)
    trace = trace_rows(result.steps)

    assert decision.contract_state is ContractState.MET
    assert decision.escalation == "Not required"
    assert decision.plan_name == result.execution_plan.human_readable_name
    assert decision.reason_codes == tuple(
        code.value for code in result.execution_plan.reason_codes
    )
    assert [row.sequence for row in trace] == [step.sequence for step in result.steps]
    assert len(trace) == len(result.steps)


def test_presentation_projects_measured_reduction_facts() -> None:
    """Display actual backend counts, method, ratio, and reduced context source."""
    result = execute_reduction_result()
    reduction = context_reduction_view(result)
    trace = trace_rows(result.steps)

    assert reduction is not None
    assert reduction.status == "Applied"
    assert reduction.original_tokens > reduction.effective_tokens
    assert reduction.reduction_percentage.endswith("%")
    assert reduction.method == "RELEVANCE_AND_FACT_EXTRACTIVE_V1"
    assert reduction.context_source == "Reduced"
    assert trace[0].context_reduction is not None
    assert trace[1].context_source == "REDUCED"


def test_presentation_does_not_infer_reduction_without_runtime_evidence() -> None:
    """Keep reduction unavailable when no typed execution step was returned."""
    result = execute_result()

    assert context_reduction_view(result) is None


def test_failed_reduction_shows_original_fallback_without_percentage_claim() -> None:
    """Render failed optimization evidence without claiming measured reduction."""
    result = failed_reduction_result()
    reduction = context_reduction_view(result)

    assert reduction is not None
    assert reduction.status == "Failed; original context used"
    assert reduction.original_tokens == reduction.effective_tokens
    assert reduction.reduction_percentage == "Unavailable"
    assert reduction.method == "Unavailable"
    assert reduction.context_source == "Original"
    assert outcome_label(result).startswith(
        "Context reduction failed; original context"
    )


def test_decision_exposes_escalation_and_final_contract_failure() -> None:
    """Distinguish measured failure after one actual fallback from unavailable."""
    result = execute_result(quality_profile=QualityProfile.CRITICAL)
    decision = decision_view(result)

    assert result.escalated is True
    assert decision.escalation == "Occurred"
    assert decision.contract_state is ContractState.NOT_MET


def test_contract_state_and_cost_formatting_preserve_exact_values() -> None:
    """Keep False, None, exact Decimal cost, currency, and catalog identity distinct."""
    provenance = PricingProvenance(catalog_version="prices-v2", currency="USD")

    assert contract_state(True) is ContractState.MET
    assert contract_state(False) is ContractState.NOT_MET
    assert contract_state(None) is ContractState.UNAVAILABLE
    assert format_cost(Decimal("10"), provenance) == "USD 10 (catalog prices-v2)"
    assert format_cost(Decimal("100.00"), provenance) == ("USD 100 (catalog prices-v2)")
    assert format_cost(Decimal("0.0012300"), provenance) == (
        "USD 0.00123 (catalog prices-v2)"
    )
    assert format_cost(Decimal("0"), provenance) == "USD 0 (catalog prices-v2)"
    assert format_cost(None, provenance) == "Unavailable"
    assert format_cost(Decimal("0"), None) == "Unavailable"


def test_attempted_model_call_count_uses_non_skipped_trace_steps() -> None:
    """Count attempts independently from optional model usage measurements."""
    successful = execute_result()
    failed = interrupted_model_call_result(ExecutionStatus.FAILED)
    timed_out = interrupted_model_call_result(ExecutionStatus.TIMED_OUT)
    skipped = interrupted_model_call_result(ExecutionStatus.SKIPPED)
    escalated = execute_result(quality_profile=QualityProfile.CRITICAL)

    assert len(successful.model_usages) == 1
    assert attempted_model_call_count(successful.steps) == 1
    assert failed.model_usages == ()
    assert attempted_model_call_count(failed.steps) == 1
    assert timed_out.model_usages == ()
    assert attempted_model_call_count(timed_out.steps) == 1
    assert attempted_model_call_count(skipped.steps) == 0
    assert attempted_model_call_count(escalated.steps) == 2


def test_execute_decision_uses_trace_based_model_call_count() -> None:
    """Expose a failed attempt in Execute even when no usage was measured."""
    result = interrupted_model_call_result(ExecutionStatus.FAILED)

    assert decision_view(result).model_calls == 1


def test_session_history_add_select_and_dashboard_use_actual_runs() -> None:
    """Aggregate retained results and never call a small-first success small direct."""
    passing = execute_result()
    failing = execute_result(quality_profile=QualityProfile.CRITICAL)
    entries = add_entry((), HistoryEntry(result=passing))
    entries = add_entry(entries, HistoryEntry(result=failing))
    entries = add_entry(entries, HistoryEntry(result=passing))

    summary = aggregate_dashboard(entries)

    assert len(entries) == 2
    assert entries[0].result.run_id == passing.run_id
    assert select_entry(entries, failing.run_id) is not None
    assert summary.run_count == 2
    assert summary.measured_contract_count == 2
    assert summary.contract_pass_rate == 0.5
    assert summary.plan_distribution == {
        "Small first -> verified without escalation": 1,
        "Small -> Strong escalation": 1,
    }


def test_missing_baseline_is_not_zero_savings() -> None:
    """Keep dashboard and history comparison values unavailable without evidence."""
    entries = (HistoryEntry(result=execute_result()),)
    summary = aggregate_dashboard(entries)
    row = history_rows(entries)[0]

    assert summary.comparison_count == 0
    assert summary.cost_saved is None
    assert summary.tokens_saved is None
    assert summary.latency_change_ms is None
    assert summary.baseline_tokens is None
    assert summary.optima_tokens is None
    assert summary.baseline_cost is None
    assert summary.optima_cost is None
    assert summary.cost_provenance is None
    assert summary.baseline_pass_rate is None
    assert summary.optima_pass_rate is None
    assert row.cost_savings is None


def test_compatible_measured_baseline_is_aggregated() -> None:
    """Show savings only after Slice 6 validates two compatible measured arms."""
    optima = execute_result()
    baseline_payload = optima.model_dump(mode="json", exclude_computed_fields=True)
    baseline_payload["run_id"] = "run-compatible-baseline"
    baseline_payload["correlation_id"] = "correlation-compatible-baseline"
    for index, usage in enumerate(baseline_payload["model_usages"]):
        usage["run_id"] = "run-compatible-baseline"
        usage["request_id"] = f"baseline-request-{index + 1}"
    baseline = RunResult.model_validate(baseline_payload)
    identity = BenchmarkCaseIdentity(
        benchmark_case_id="case-compatible-1",
        input_fingerprint="sha256:compatible",
    )
    comparison = BaselineComparisonService().compare(
        BaselineComparisonRequest(
            baseline=ComparableRun(
                arm=ComparisonArm.BASELINE,
                identity=identity,
                run_result=baseline,
            ),
            optima=ComparableRun(
                arm=ComparisonArm.OPTIMA,
                identity=identity,
                run_result=optima,
            ),
        )
    )

    entries = (HistoryEntry(result=optima, comparison=comparison),)
    summary = aggregate_dashboard(entries)
    row = history_rows(entries)[0]

    assert summary.comparison_count == 1
    assert summary.baseline_tokens == baseline.total_tokens
    assert summary.optima_tokens == optima.total_tokens
    assert summary.cost_saved == Decimal("0")
    assert summary.tokens_saved == 0
    assert summary.cost_provenance == optima.total_cost_provenance
    assert row.cost_savings == Decimal("0")
    assert row.savings_provenance == optima.total_cost_provenance
