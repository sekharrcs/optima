"""Provider-independent contracts for privacy-safe OPTIMA observability."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self

from fastapi import FastAPI

from optima.domain.execution import (
    CachePolicy,
    ContextPolicy,
    ContextReductionOutcome,
    ModelPolicy,
    ModelRole,
)
from optima.domain.run import RunResult
from optima.storage import RunHistoryErrorCode

TELEMETRY_SCHEMA_VERSION = "1"


class ObservationStage(StrEnum):
    """Stable logical operation names in telemetry schema version 1."""

    QUALITY_CONTRACT_BUILD = "optima.quality_contract.build"
    SEMANTIC_CACHE_LOOKUP = "optima.semantic_cache.lookup"
    PLANNER_SELECT = "optima.planner.select"
    CONTEXT_REDUCTION = "optima.context_reduction"
    MODEL_GENERATE = "optima.model.generate"
    EVALUATION_EVALUATE = "optima.evaluation.evaluate"
    RUN_HISTORY_SAVE = "optima.run_history.save"
    OUTCOME_PROJECT = "optima.outcome.project"


class ObservationStatus(StrEnum):
    """Bounded operation status independent of telemetry-provider types."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class FailureCategory(StrEnum):
    """Safe failure categories that never carry exception messages."""

    CACHE = "CACHE"
    CONFIGURATION = "CONFIGURATION"
    CONTEXT_REDUCTION = "CONTEXT_REDUCTION"
    EVALUATOR = "EVALUATOR"
    MODEL_PROVIDER = "MODEL_PROVIDER"
    PERSISTENCE = "PERSISTENCE"
    PLANNING = "PLANNING"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED_PLAN = "UNSUPPORTED_PLAN"
    VALIDATION = "VALIDATION"


class PlanFamily(StrEnum):
    """Bounded presentation-independent plan families."""

    CACHED_RESULT = "CACHED_RESULT"
    SMALL_FIRST = "SMALL_FIRST"
    STRONG_DIRECT = "STRONG_DIRECT"


class CacheLookupResult(StrEnum):
    """Immediate result of an attempted cache lookup before planner gates."""

    CANDIDATE_FOUND = "CANDIDATE_FOUND"
    MISS = "MISS"


class PersistenceResult(StrEnum):
    """Bounded best-effort run-history persistence outcomes."""

    PERSISTED = "PERSISTED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """Completion evidence for a stage without stage-specific measurements."""

    status: ObservationStatus
    failure_category: FailureCategory | None = None


@dataclass(frozen=True, slots=True)
class CacheStageOutcome:
    """Safe completion evidence for one actual semantic-cache lookup."""

    status: ObservationStatus
    lookup_result: CacheLookupResult | None = None
    failure_category: FailureCategory | None = None


@dataclass(frozen=True, slots=True)
class PlannerStageOutcome:
    """Bounded evidence from one successful Planner V1 selection."""

    status: ObservationStatus
    plan_family: PlanFamily | None = None
    cache_policy: CachePolicy | None = None
    context_policy: ContextPolicy | None = None
    model_policy: ModelPolicy | None = None
    failure_category: FailureCategory | None = None


@dataclass(frozen=True, slots=True)
class ContextStageOutcome:
    """Measured evidence from one attempted context reduction."""

    status: ObservationStatus
    outcome: ContextReductionOutcome
    original_tokens: int
    effective_tokens: int
    latency_ms: int
    failure_category: FailureCategory | None = None


@dataclass(frozen=True, slots=True)
class ModelStageOutcome:
    """Safe measured evidence from one actual model-generation attempt."""

    status: ObservationStatus
    model_role: ModelRole
    latency_ms: int
    provider_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    failure_category: FailureCategory | None = None


@dataclass(frozen=True, slots=True)
class EvaluationStageOutcome:
    """Safe measured evidence from one actual quality-evaluation attempt."""

    status: ObservationStatus
    model_role: ModelRole
    latency_ms: int
    evaluator_type: str | None = None
    judge_model_role: ModelRole | None = None
    judge_deployment: str | None = None
    judge_input_tokens: int | None = None
    judge_output_tokens: int | None = None
    judge_cached_tokens: int | None = None
    evaluator_valid: bool | None = None
    score: float | None = None
    passed: bool | None = None
    failure_category: FailureCategory | None = None


@dataclass(frozen=True, slots=True)
class PersistenceStageOutcome:
    """Safe evidence from one actual run-history save attempt."""

    status: ObservationStatus
    result: PersistenceResult
    error_code: RunHistoryErrorCode | None = None
    failure_category: FailureCategory | None = None


type StageOutcomeEvidence = (
    StageOutcome
    | CacheStageOutcome
    | PlannerStageOutcome
    | ContextStageOutcome
    | ModelStageOutcome
    | EvaluationStageOutcome
    | PersistenceStageOutcome
)


class StageObservation(Protocol):
    """Close-once scoped observation of one logical operation."""

    def __enter__(self) -> Self:
        """Activate the stage as the current observation context."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the stage without inspecting or exporting raw exception data."""
        ...

    def finish(self, outcome: StageOutcomeEvidence) -> None:
        """Record one bounded completion outcome at most once."""
        ...


class RunObservation(Protocol):
    """One async-context-safe observation rooted at ``optima.run``."""

    def __enter__(self) -> Self:
        """Activate the run as the current observation context."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the run without inspecting or exporting raw exception data."""
        ...

    def start_stage(self, stage: ObservationStage) -> StageObservation:
        """Start one child stage for an operation that is actually attempted."""
        ...

    def project_result(self, result: RunResult) -> None:
        """Project one validated authoritative terminal result at most once."""
        ...

    def record_pre_result_failure(self, category: FailureCategory) -> None:
        """Record a bounded failure when no terminal result exists."""
        ...


class Observability(Protocol):
    """Application-scoped observability boundary with explicit ownership."""

    def start_run(self, *, run_id: str, correlation_id: str) -> RunObservation:
        """Start one request-scoped OPTIMA run observation."""
        ...

    def instrument_fastapi(self, application: FastAPI) -> None:
        """Add privacy-safe HTTP server instrumentation to one application."""
        ...

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Request bounded export of pending telemetry."""
        ...

    def close(self) -> None:
        """Close owned telemetry resources at most once."""
        ...


def plan_family(
    *,
    cache_policy: CachePolicy,
    model_policy: ModelPolicy | None,
) -> PlanFamily:
    """Map one validated execution plan to its bounded telemetry family."""
    if cache_policy is CachePolicy.USE_CACHED_RESULT:
        return PlanFamily.CACHED_RESULT
    if model_policy is ModelPolicy.SMALL_FIRST_WITH_FALLBACK:
        return PlanFamily.SMALL_FIRST
    if model_policy is ModelPolicy.STRONG_DIRECT:
        return PlanFamily.STRONG_DIRECT
    raise ValueError("execution plan does not map to a telemetry plan family")
