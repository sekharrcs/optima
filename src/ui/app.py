"""Streamlit decision demo for actual OPTIMA API evidence."""

import os
from dataclasses import asdict
from decimal import Decimal
from enum import StrEnum
from typing import cast

import streamlit as st

from optima.comparison import BaselineComparison
from optima.domain.quality_contract import OptimizationMode, QualityProfile, RiskTier
from optima.domain.request_profile import Complexity, TaskType
from optima.domain.run import PricingProvenance, RunResult
from ui.api_client import DEFAULT_API_BASE_URL, ApiClientError, OptimaApiClient
from ui.history import (
    HistoryEntry,
    add_entry,
    aggregate_dashboard,
    history_rows,
    select_entry,
)
from ui.models import ExecuteInputs
from ui.presentation import (
    ContractState,
    decision_view,
    format_cost,
    format_score,
    trace_rows,
)

PRIMARY_VIEWS = ("Execute", "Dashboard", "Run History")
HISTORY_KEY = "optima_session_history"


def _history() -> tuple[HistoryEntry, ...]:
    """Return only validated session-local history entries."""
    value = st.session_state.get(HISTORY_KEY, ())
    if not isinstance(value, tuple) or not all(
        isinstance(entry, HistoryEntry) for entry in value
    ):
        st.session_state[HISTORY_KEY] = ()
        return ()
    return cast(tuple[HistoryEntry, ...], value)


def _store_result(
    result: RunResult,
    comparison: BaselineComparison | None = None,
) -> None:
    """Retain one actual API result for the current Streamlit session only."""
    st.session_state[HISTORY_KEY] = add_entry(
        _history(),
        HistoryEntry(result=result, comparison=comparison),
    )


def _enum_selectbox[EnumValue: StrEnum](
    label: str,
    options: tuple[EnumValue, ...],
    *,
    default: EnumValue,
) -> EnumValue:
    """Render a typed enum selectbox with one explicit default."""
    return st.selectbox(
        label,
        options,
        index=options.index(default),
        format_func=lambda value: value.value.replace("_", " ").title(),
    )


def execute_page() -> None:
    """Render request controls and one backend-authored execution decision."""
    st.title("OPTIMA")
    st.subheader("Quality-Constrained AI Execution Optimizer")
    st.write(
        "Find the most efficient execution plan allowed by the Quality Contract "
        "and Optimization Mode."
    )
    st.info(
        "Quality Profile defines the minimum acceptable quality. Optimization Mode "
        "controls how aggressively OPTIMA pursues lower-cost paths; it never lowers "
        "the required quality threshold."
    )

    with st.form("execute_optima"):
        input_text = st.text_area(
            "Task or request",
            height=140,
            placeholder="Describe the work OPTIMA should execute.",
        )
        context = st.text_area(
            "Optional context or supporting data",
            height=110,
            placeholder="Add evidence or context required for the answer.",
        )
        quality_profile = _enum_selectbox(
            "Quality Profile",
            tuple(QualityProfile),
            default=QualityProfile.HIGH,
        )
        optimization_mode = _enum_selectbox(
            "Optimization Mode",
            tuple(OptimizationMode),
            default=OptimizationMode.COST,
        )

        with st.expander("Advanced demo inputs", expanded=False):
            st.caption(
                "These Request Profile values are supplied demo inputs. The current "
                "API does not infer or measure a profile."
            )
            task_type = _enum_selectbox(
                "Task type",
                tuple(TaskType),
                default=TaskType.SUMMARIZATION,
            )
            complexity = _enum_selectbox(
                "Request complexity",
                tuple(Complexity),
                default=Complexity.LOW,
            )
            input_tokens = st.number_input(
                "Supplied input-token count",
                min_value=0,
                value=500,
                step=100,
            )
            profile_risk = _enum_selectbox(
                "Supplied profile risk tier",
                tuple(RiskTier),
                default=RiskTier.LOW,
            )
            contract_risk = _enum_selectbox(
                "Quality Contract risk tier",
                tuple(RiskTier),
                default=RiskTier.LOW,
            )
            cache_eligible = st.checkbox("Cache eligible", value=False)
            has_large_context = st.checkbox("Has large context", value=False)
            api_base_url = st.text_input(
                "OPTIMA API base URL",
                value=os.getenv("OPTIMA_API_BASE_URL", DEFAULT_API_BASE_URL),
                help=(
                    "Use the local demo API or another configured OPTIMA "
                    "FastAPI instance."
                ),
            )
        submitted = st.form_submit_button(
            "Run with OPTIMA",
            type="primary",
            width="stretch",
        )

    if submitted:
        if not input_text.strip():
            st.error("Enter a task or request before running OPTIMA.")
        else:
            request = ExecuteInputs(
                input_text=input_text.strip(),
                context=context.strip() or None,
                quality_profile=quality_profile,
                optimization_mode=optimization_mode,
                task_type=task_type,
                complexity=complexity,
                input_tokens=int(input_tokens),
                profile_risk_tier=profile_risk,
                contract_risk_tier=contract_risk,
                cache_eligible=cache_eligible,
                has_large_context=has_large_context,
            ).to_run_request()
            try:
                with st.status("Executing the selected OPTIMA plan...", expanded=True):
                    st.write("Submitting the supplied request and profile to FastAPI")
                    result = OptimaApiClient(api_base_url).execute(request)
                    st.write("Validating returned RunResult evidence")
                _store_result(result)
            except (ApiClientError, ValueError) as error:
                _render_api_error(error)

    entries = _history()
    if entries:
        _render_execute_result(entries[0])
    else:
        st.caption("No execution evidence is retained in this session yet.")


def _render_api_error(error: ApiClientError | ValueError) -> None:
    """Render failures as failures without exposing sensitive transport details."""
    if isinstance(error, ApiClientError):
        st.error(f"Execution failed: {error.message}")
        with st.expander("API error evidence"):
            st.json(
                {
                    "code": error.code,
                    "status_code": error.status_code,
                    "facts": error.facts or {},
                }
            )
    else:
        st.error(f"Configuration error: {error}")


def _render_execute_result(entry: HistoryEntry) -> None:
    """Render decision evidence before the answer and trace."""
    result = entry.result
    decision = decision_view(result)
    st.divider()
    st.header("OPTIMA Decision")
    st.subheader(decision.plan_name)
    decision_columns = st.columns(4)
    decision_columns[0].metric("Complexity", decision.complexity)
    decision_columns[1].metric("Contract", decision.contract_state.value)
    decision_columns[2].metric("Final quality", decision.final_quality)
    decision_columns[3].metric("Escalation", decision.escalation)

    if decision.contract_state is ContractState.MET:
        st.success(
            f"Contract Met: {decision.quality_profile} requires "
            f"{decision.threshold}; measured quality was {decision.final_quality}."
        )
    elif decision.contract_state is ContractState.NOT_MET:
        st.error(
            f"Contract Not Met: {decision.quality_profile} requires "
            f"{decision.threshold}; measured quality was {decision.final_quality}."
        )
    else:
        st.warning("Contract Unavailable: no valid final evaluation was returned.")

    st.write(f"**Optimization Mode:** {decision.optimization_mode}")
    st.write("**Plan components:** " + " | ".join(decision.components))
    st.write("**Why Planner V1 selected this plan:**")
    for reason in decision.reason_explanations:
        st.write(f"- {reason}")

    st.subheader("Measured resources")
    resource_columns = st.columns(4)
    resource_columns[0].metric(
        "Model calls",
        decision.model_calls,
    )
    resource_columns[1].metric(
        "Total tokens",
        result.total_tokens if result.total_tokens is not None else "Unavailable",
    )
    resource_columns[2].metric(
        "Calculated cost",
        format_cost(result.total_calculated_cost, result.total_cost_provenance),
    )
    resource_columns[3].metric("Latency", f"{result.latency_ms} ms")

    _render_comparison(entry.comparison)

    st.header("Final answer")
    if result.final_output is not None:
        st.write(result.final_output)
    else:
        st.error(result.error or "No final output was returned.")

    st.header("Actual execution trace")
    _render_trace(result)
    with st.expander("Advanced and debug evidence"):
        st.write(f"Run ID: `{result.run_id}`")
        st.write(f"Correlation ID: `{result.correlation_id}`")
        st.write("Planner reason codes")
        st.code("\n".join(decision.reason_codes), language="text")
        st.json(
            {
                "request_profile": result.request_profile.model_dump(mode="json"),
                "quality_contract": result.quality_contract.model_dump(mode="json"),
                "execution_plan": result.execution_plan.model_dump(mode="json"),
            }
        )


def _render_trace(result: RunResult) -> None:
    """Render exactly the ordered steps returned by FastAPI."""
    for row in trace_rows(result.steps):
        label = f"{row.sequence + 1}. {row.step}: {row.status} ({row.latency})"
        if row.status == "Succeeded":
            st.success(label)
        elif row.status == "Skipped":
            st.info(label)
        else:
            st.error(label)
        if row.events:
            st.caption(" | ".join(row.events))
        if row.error:
            st.caption(f"Error: {row.error}")
        if row.facts:
            with st.expander(f"Step {row.sequence + 1} facts"):
                st.json(row.facts)


def _render_comparison(comparison: BaselineComparison | None) -> None:
    """Render savings only from a compatibility-validated comparison."""
    st.header("Baseline versus OPTIMA")
    if comparison is None:
        st.info(
            "Baseline not available. No compatible measured baseline evidence was "
            "returned for this run; savings and deltas are not reported."
        )
        return

    baseline = comparison.baseline
    optima = comparison.optima
    st.caption(f"Measured benchmark case: {comparison.identity.benchmark_case_id}")
    st.dataframe(
        [
            {
                "Arm": "Baseline",
                "Model calls": baseline.model_calls,
                "Input tokens": baseline.input_tokens,
                "Output tokens": baseline.output_tokens,
                "Total tokens": baseline.total_tokens,
                "Cost": format_cost(baseline.cost, baseline.cost_provenance),
                "Latency": f"{baseline.latency_ms} ms",
                "Quality": format_score(baseline.quality_score),
                "Contract": ContractState.MET.value
                if baseline.contract_met is True
                else ContractState.NOT_MET.value
                if baseline.contract_met is False
                else ContractState.UNAVAILABLE.value,
            },
            {
                "Arm": "OPTIMA",
                "Model calls": optima.model_calls,
                "Input tokens": optima.input_tokens,
                "Output tokens": optima.output_tokens,
                "Total tokens": optima.total_tokens,
                "Cost": format_cost(optima.cost, optima.cost_provenance),
                "Latency": f"{optima.latency_ms} ms",
                "Quality": format_score(optima.quality_score),
                "Contract": ContractState.MET.value
                if optima.contract_met is True
                else ContractState.NOT_MET.value
                if optima.contract_met is False
                else ContractState.UNAVAILABLE.value,
            },
        ],
        hide_index=True,
        width="stretch",
    )
    savings_columns = st.columns(3)
    savings_columns[0].metric(
        "Token reduction",
        _format_percentage(comparison.token_reduction_percentage),
    )
    savings_columns[1].metric(
        "Cost reduction",
        _format_percentage(comparison.cost_reduction_percentage),
    )
    savings_columns[2].metric(
        "Latency change",
        _format_percentage(comparison.latency_percentage_change),
    )


def dashboard_page() -> None:
    """Render aggregates computed only from session-retained actual runs."""
    st.title("Dashboard")
    st.caption(
        "Session-only in-memory evidence. Refreshing or restarting Streamlit clears "
        "this history; it is not durable storage."
    )
    entries = _history()
    summary = aggregate_dashboard(entries)
    if not entries:
        st.info("Run OPTIMA in this session to populate measured dashboard evidence.")
        return

    kpis = st.columns(4)
    kpis[0].metric(
        "Cost saved vs baseline",
        _format_decimal(summary.cost_saved, summary.cost_provenance),
    )
    kpis[1].metric(
        "Tokens saved vs baseline",
        summary.tokens_saved if summary.tokens_saved is not None else "Unavailable",
    )
    kpis[2].metric(
        "Quality Contract pass rate",
        _format_rate(summary.contract_pass_rate),
        help=f"Measured denominator: {summary.measured_contract_count} runs",
    )
    kpis[3].metric(
        "Latency change vs baseline",
        f"{summary.latency_change_ms} ms"
        if summary.latency_change_ms is not None
        else "Unavailable",
    )

    st.header("Actual execution-plan outcomes")
    st.dataframe(
        [
            {"Planner V1 outcome": label, "Runs": count}
            for label, count in summary.plan_distribution.items()
        ],
        hide_index=True,
        width="stretch",
    )

    st.header("Aggregate baseline versus OPTIMA")
    if summary.comparison_count == 0:
        st.info(
            "Baseline not available. No retained run has compatible measured "
            "baseline evidence, so aggregate savings are not reported."
        )
        return
    st.caption(f"Compatible measured comparisons: {summary.comparison_count}")
    st.dataframe(
        [
            {
                "Arm": "Baseline",
                "Requests": summary.comparison_count,
                "Tokens": summary.baseline_tokens,
                "Cost": _format_decimal(summary.baseline_cost, summary.cost_provenance),
                "Contract pass rate": _format_rate(summary.baseline_pass_rate),
                "Average latency": _format_latency(summary.baseline_average_latency_ms),
            },
            {
                "Arm": "OPTIMA",
                "Requests": summary.comparison_count,
                "Tokens": summary.optima_tokens,
                "Cost": _format_decimal(summary.optima_cost, summary.cost_provenance),
                "Contract pass rate": _format_rate(summary.optima_pass_rate),
                "Average latency": _format_latency(summary.optima_average_latency_ms),
            },
        ],
        hide_index=True,
        width="stretch",
    )


def run_history_page() -> None:
    """Render compact history and full evidence for one selected run."""
    st.title("Run History")
    st.caption(
        "Session-only in-memory evidence. This view does not claim durable storage."
    )
    entries = _history()
    if not entries:
        st.info("No runs are retained in this Streamlit session.")
        return

    rows = history_rows(entries)
    st.dataframe(
        [
            {
                "Run ID": row.run_id,
                "Timestamp": row.timestamp,
                "Task type": row.task_type,
                "Complexity": row.complexity,
                "Plan": row.plan_name,
                "Final quality": format_score(row.final_quality),
                "Cost": format_cost(
                    row.cost,
                    row.cost_provenance,
                ),
                "Savings vs baseline": format_cost(
                    row.cost_savings,
                    row.savings_provenance,
                ),
                "Contract": row.contract_state.value,
            }
            for row in rows
        ],
        hide_index=True,
        width="stretch",
    )
    selected_run_id = st.selectbox(
        "Inspect run",
        [entry.result.run_id for entry in entries],
    )
    selected = select_entry(entries, selected_run_id)
    if selected is not None:
        _render_run_detail(selected)


def _render_run_detail(entry: HistoryEntry) -> None:
    """Render actual domain evidence for one selected retained run."""
    result = entry.result
    st.header(result.execution_plan.human_readable_name)
    st.write(f"**Operational status:** {result.status.value}")
    st.write(f"**Contract status:** {decision_view(result).contract_state.value}")
    if result.error:
        st.error(result.error)

    st.subheader("Request Profile")
    st.dataframe(
        [
            {"Field": "Task type", "Value": result.request_profile.task_type.value},
            {"Field": "Complexity", "Value": result.request_profile.complexity.value},
            {"Field": "Input tokens", "Value": result.request_profile.input_tokens},
            {"Field": "Risk tier", "Value": result.request_profile.risk_tier.value},
            {
                "Field": "Cache eligible",
                "Value": result.request_profile.cache_eligible,
            },
            {
                "Field": "Has large context",
                "Value": result.request_profile.has_large_context,
            },
        ],
        hide_index=True,
        width="stretch",
    )
    st.subheader("Quality Contract and Optimization Mode")
    st.dataframe(
        [
            {
                "Field": "Quality Profile",
                "Value": result.quality_contract.quality_profile.value,
            },
            {
                "Field": "Required threshold",
                "Value": format_score(result.quality_contract.minimum_quality_score),
            },
            {
                "Field": "Optimization Mode",
                "Value": result.quality_contract.optimization_mode.value,
            },
            {
                "Field": "Risk tier",
                "Value": result.quality_contract.risk_tier.value,
            },
            {
                "Field": "Latency ceiling",
                "Value": (
                    f"{result.quality_contract.max_latency_ms} ms"
                    if result.quality_contract.max_latency_ms is not None
                    else "Unavailable"
                ),
            },
        ],
        hide_index=True,
        width="stretch",
    )
    st.subheader("Planner reason codes")
    st.code(
        "\n".join(code.value for code in result.execution_plan.reason_codes),
        language="text",
    )
    st.subheader("Execution steps")
    st.dataframe(
        [asdict(row) for row in trace_rows(result.steps)],
        hide_index=True,
        width="stretch",
    )
    st.subheader("Model usage")
    if result.model_usages:
        st.dataframe(
            [usage.model_dump(mode="json") for usage in result.model_usages],
            hide_index=True,
            width="stretch",
        )
    else:
        st.write("Unavailable")
    st.subheader("Evaluations")
    if result.evaluations:
        st.dataframe(
            [evaluation.model_dump(mode="json") for evaluation in result.evaluations],
            hide_index=True,
            width="stretch",
        )
    else:
        st.write("Unavailable")
    _render_comparison(entry.comparison)
    with st.expander("Correlation and raw debug evidence"):
        st.write(f"Correlation ID: `{result.correlation_id}`")
        st.json(result.model_dump(mode="json"))


def _format_percentage(value: Decimal | None) -> str:
    return "Unavailable" if value is None else f"{value:.2f}%"


def _format_rate(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value:.1%}"


def _format_decimal(
    value: Decimal | None,
    provenance: PricingProvenance | None,
) -> str:
    if value is None or provenance is None:
        return "Unavailable"
    return format_cost(value, provenance)


def _format_latency(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value:.1f} ms"


def main() -> None:
    """Configure exactly three primary views and run the selected page."""
    st.set_page_config(
        page_title="OPTIMA Decision Demo",
        page_icon=":material/route:",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    pages = [
        st.Page(execute_page, title=PRIMARY_VIEWS[0], icon=":material/play_arrow:"),
        st.Page(dashboard_page, title=PRIMARY_VIEWS[1], icon=":material/monitoring:"),
        st.Page(
            run_history_page,
            title=PRIMARY_VIEWS[2],
            icon=":material/history:",
        ),
    ]
    st.navigation(pages, position="sidebar").run()


if __name__ == "__main__":
    main()
