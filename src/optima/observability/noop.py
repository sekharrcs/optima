"""Inert observability implementation for cloud-free operation."""

from types import TracebackType
from typing import Self

from fastapi import FastAPI

from optima.domain.run import RunResult
from optima.observability.contracts import (
    FailureCategory,
    ObservationStage,
    StageOutcomeEvidence,
)


class NoOpStageObservation:
    """A close-safe stage that performs no work."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def finish(self, outcome: StageOutcomeEvidence) -> None:
        return None


class NoOpRunObservation:
    """A close-safe run observation that performs no work."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def start_stage(self, stage: ObservationStage) -> NoOpStageObservation:
        return NO_OP_STAGE

    def project_result(self, result: RunResult) -> None:
        return None

    def record_pre_result_failure(self, category: FailureCategory) -> None:
        return None


class NoOpObservability:
    """Application-scoped no-op with no imports, I/O, threads, or state."""

    def start_run(
        self,
        *,
        run_id: str,
        correlation_id: str,
    ) -> NoOpRunObservation:
        return NO_OP_RUN

    def instrument_fastapi(self, application: FastAPI) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def close(self) -> None:
        return None


NO_OP_STAGE = NoOpStageObservation()
NO_OP_RUN = NoOpRunObservation()
NO_OP_OBSERVABILITY = NoOpObservability()
