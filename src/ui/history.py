"""Pure session-history storage values and dashboard aggregation."""

from dataclasses import dataclass
from decimal import Decimal

from optima.comparison import BaselineComparison
from optima.domain.run import PricingProvenance, RunResult
from ui.presentation import ContractState, contract_state, outcome_label


@dataclass(frozen=True)
class HistoryEntry:
    """One actual OPTIMA result with optional validated baseline evidence."""

    result: RunResult
    comparison: BaselineComparison | None = None

    def __post_init__(self) -> None:
        if (
            self.comparison is not None
            and self.comparison.optima.run_id != self.result.run_id
        ):
            raise ValueError("comparison OPTIMA arm must match the history run")


@dataclass(frozen=True)
class HistoryRow:
    """Compact non-sensitive fields for the history table."""

    run_id: str
    timestamp: str
    task_type: str
    complexity: str
    plan_name: str
    final_quality: float | None
    cost: Decimal | None
    cost_provenance: PricingProvenance | None
    cost_savings: Decimal | None
    savings_provenance: PricingProvenance | None
    contract_state: ContractState


@dataclass(frozen=True)
class DashboardSummary:
    """Aggregates computed exclusively from retained actual entries."""

    run_count: int
    measured_contract_count: int
    contract_pass_rate: float | None
    plan_distribution: dict[str, int]
    comparison_count: int
    cost_saved: Decimal | None
    tokens_saved: int | None
    latency_change_ms: int | None
    baseline_tokens: int | None
    optima_tokens: int | None
    baseline_cost: Decimal | None
    optima_cost: Decimal | None
    cost_provenance: PricingProvenance | None
    baseline_pass_rate: float | None
    optima_pass_rate: float | None
    baseline_average_latency_ms: float | None
    optima_average_latency_ms: float | None


def add_entry(
    entries: tuple[HistoryEntry, ...],
    entry: HistoryEntry,
) -> tuple[HistoryEntry, ...]:
    """Add or replace one run and retain newest-first ordering."""
    remaining = tuple(
        existing
        for existing in entries
        if existing.result.run_id != entry.result.run_id
    )
    return (entry, *remaining)


def select_entry(
    entries: tuple[HistoryEntry, ...],
    run_id: str,
) -> HistoryEntry | None:
    """Select one retained run by its opaque identifier."""
    return next(
        (entry for entry in entries if entry.result.run_id == run_id),
        None,
    )


def history_rows(entries: tuple[HistoryEntry, ...]) -> tuple[HistoryRow, ...]:
    """Project compact rows while preserving unavailable measurements."""
    return tuple(
        HistoryRow(
            run_id=entry.result.run_id,
            timestamp=entry.result.created_at.isoformat(),
            task_type=entry.result.request_profile.task_type.value,
            complexity=entry.result.request_profile.complexity.value,
            plan_name=entry.result.execution_plan.human_readable_name,
            final_quality=(
                entry.result.final_evaluation.score
                if entry.result.final_evaluation is not None
                and entry.result.final_evaluation.evaluator_valid
                else None
            ),
            cost=entry.result.total_calculated_cost,
            cost_provenance=entry.result.total_cost_provenance,
            cost_savings=(
                -entry.comparison.cost_delta
                if entry.comparison is not None
                and entry.comparison.cost_delta is not None
                else None
            ),
            savings_provenance=(
                entry.comparison.optima.cost_provenance
                if entry.comparison is not None
                else None
            ),
            contract_state=contract_state(entry.result.contract_met),
        )
        for entry in entries
    )


def aggregate_dashboard(entries: tuple[HistoryEntry, ...]) -> DashboardSummary:
    """Aggregate actual runs and only complete compatible comparison metrics."""
    measured_contracts = [
        entry.result.contract_met
        for entry in entries
        if entry.result.contract_met is not None
    ]
    plan_distribution: dict[str, int] = {}
    for entry in entries:
        label = outcome_label(entry.result)
        plan_distribution[label] = plan_distribution.get(label, 0) + 1

    comparisons = tuple(
        entry.comparison for entry in entries if entry.comparison is not None
    )
    cost_provenance = _common_cost_provenance(comparisons)
    return DashboardSummary(
        run_count=len(entries),
        measured_contract_count=len(measured_contracts),
        contract_pass_rate=(
            sum(measured_contracts) / len(measured_contracts)
            if measured_contracts
            else None
        ),
        plan_distribution=plan_distribution,
        comparison_count=len(comparisons),
        cost_saved=(
            _complete_decimal_savings(
                tuple(comparison.cost_delta for comparison in comparisons)
            )
            if cost_provenance is not None
            else None
        ),
        tokens_saved=_complete_integer_savings(
            tuple(comparison.total_tokens_delta for comparison in comparisons)
        ),
        latency_change_ms=(
            sum(comparison.latency_ms_delta for comparison in comparisons)
            if comparisons
            else None
        ),
        baseline_tokens=_complete_integer_total(
            tuple(comparison.baseline.total_tokens for comparison in comparisons)
        ),
        optima_tokens=_complete_integer_total(
            tuple(comparison.optima.total_tokens for comparison in comparisons)
        ),
        baseline_cost=(
            _complete_decimal_total(
                tuple(comparison.baseline.cost for comparison in comparisons)
            )
            if cost_provenance is not None
            else None
        ),
        optima_cost=(
            _complete_decimal_total(
                tuple(comparison.optima.cost for comparison in comparisons)
            )
            if cost_provenance is not None
            else None
        ),
        cost_provenance=cost_provenance,
        baseline_pass_rate=_pass_rate(
            tuple(comparison.baseline.contract_met for comparison in comparisons)
        ),
        optima_pass_rate=_pass_rate(
            tuple(comparison.optima.contract_met for comparison in comparisons)
        ),
        baseline_average_latency_ms=(
            sum(comparison.baseline.latency_ms for comparison in comparisons)
            / len(comparisons)
            if comparisons
            else None
        ),
        optima_average_latency_ms=(
            sum(comparison.optima.latency_ms for comparison in comparisons)
            / len(comparisons)
            if comparisons
            else None
        ),
    )


def _complete_decimal_savings(
    deltas: tuple[Decimal | None, ...],
) -> Decimal | None:
    if not deltas or any(delta is None for delta in deltas):
        return None
    return -sum((delta for delta in deltas if delta is not None), Decimal("0"))


def _complete_integer_savings(deltas: tuple[int | None, ...]) -> int | None:
    if not deltas or any(delta is None for delta in deltas):
        return None
    return -sum(delta for delta in deltas if delta is not None)


def _complete_decimal_total(values: tuple[Decimal | None, ...]) -> Decimal | None:
    if not values or any(value is None for value in values):
        return None
    return sum((value for value in values if value is not None), Decimal("0"))


def _complete_integer_total(values: tuple[int | None, ...]) -> int | None:
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _pass_rate(values: tuple[bool | None, ...]) -> float | None:
    measured = tuple(value for value in values if value is not None)
    if not measured:
        return None
    return sum(measured) / len(measured)


def _common_cost_provenance(
    comparisons: tuple[BaselineComparison, ...],
) -> PricingProvenance | None:
    if not comparisons:
        return None
    provenances = tuple(comparison.optima.cost_provenance for comparison in comparisons)
    first = provenances[0]
    if first is None or any(provenance != first for provenance in provenances):
        return None
    return first
