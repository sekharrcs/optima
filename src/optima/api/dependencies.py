"""Application-scoped runtime dependencies for execution routes."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from optima.config import AppSettings
from optima.context import ContextReducer, TokenCounter
from optima.cost import CostCalculator
from optima.evaluation import QualityEvaluator
from optima.execution.executor import system_utc_now
from optima.planner import ContextReducerCapability
from optima.providers import ModelProvider, MonotonicClock


def new_run_id() -> str:
    """Create one opaque run identifier."""
    return f"run-{uuid4()}"


def new_correlation_id() -> str:
    """Create one opaque correlation identifier."""
    return f"correlation-{uuid4()}"


@dataclass(frozen=True)
class ExecutionDependencies:
    """Immutable application composition for one API instance."""

    settings: AppSettings
    small_provider: ModelProvider
    strong_provider: ModelProvider
    evaluator: QualityEvaluator
    cost_calculator: CostCalculator
    context_reducer: ContextReducer | None = None
    token_counter: TokenCounter | None = None
    context_reducer_capability: ContextReducerCapability = field(
        default_factory=lambda: ContextReducerCapability(
            available=False,
            task_safe=False,
            approved_for_critical_high_risk=False,
        )
    )
    monotonic_clock: MonotonicClock | None = None
    utc_now: Callable[[], datetime] = system_utc_now
    run_id_factory: Callable[[], str] = new_run_id
    correlation_id_factory: Callable[[], str] = new_correlation_id
