"""Deterministic in-memory observability for offline tests."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import RLock
from types import TracebackType
from typing import Self

from fastapi import FastAPI

from optima.domain.run import RunResult
from optima.observability.contracts import (
    FailureCategory,
    ObservationStage,
    ObservationStatus,
    StageOutcome,
    StageOutcomeEvidence,
)


@dataclass(frozen=True, slots=True)
class RecordedObservation:
    """One closed run or stage with explicit parent identity."""

    observation_id: int
    parent_observation_id: int | None
    name: str
    outcome: StageOutcomeEvidence | None = None


class InMemoryStageObservation:
    """One close-once in-memory child operation."""

    def __init__(
        self,
        owner: InMemoryObservability,
        stage: ObservationStage,
    ) -> None:
        self._owner = owner
        self._stage = stage
        self._observation_id = owner._allocate_id()
        self._parent_observation_id: int | None = None
        self._token: Token[int | None] | None = None
        self._outcome: StageOutcomeEvidence | None = None
        self._closed = False

    def __enter__(self) -> Self:
        if self._token is None and not self._closed:
            self._parent_observation_id = self._owner._current.get()
            self._token = self._owner._current.set(self._observation_id)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        if self._token is not None:
            self._owner._current.reset(self._token)
        self._owner._append(
            RecordedObservation(
                observation_id=self._observation_id,
                parent_observation_id=self._parent_observation_id,
                name=self._stage.value,
                outcome=self._outcome,
            )
        )

    def finish(self, outcome: StageOutcomeEvidence) -> None:
        if self._outcome is None and not self._closed:
            self._outcome = outcome


class InMemoryRunObservation:
    """One close-once in-memory run root."""

    def __init__(
        self,
        owner: InMemoryObservability,
        *,
        run_id: str,
        correlation_id: str,
    ) -> None:
        self._owner = owner
        self._run_id = run_id
        self._correlation_id = correlation_id
        self._observation_id = owner._allocate_id()
        self._parent_observation_id: int | None = None
        self._token: Token[int | None] | None = None
        self._closed = False
        self._projected = False
        self._failed = False

    def __enter__(self) -> Self:
        if self._token is None and not self._closed:
            self._parent_observation_id = self._owner._current.get()
            self._token = self._owner._current.set(self._observation_id)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        if self._token is not None:
            self._owner._current.reset(self._token)
        self._owner._append(
            RecordedObservation(
                observation_id=self._observation_id,
                parent_observation_id=self._parent_observation_id,
                name="optima.run",
            )
        )

    def start_stage(self, stage: ObservationStage) -> InMemoryStageObservation:
        return InMemoryStageObservation(self._owner, stage)

    def project_result(self, result: RunResult) -> None:
        if self._projected or self._failed or self._closed:
            return
        if (
            result.run_id != self._run_id
            or result.correlation_id != self._correlation_id
        ):
            raise ValueError("terminal result identity does not match observation")
        self._projected = True
        self._owner._append(
            RecordedObservation(
                observation_id=self._owner._allocate_id(),
                parent_observation_id=self._observation_id,
                name=ObservationStage.OUTCOME_PROJECT.value,
                outcome=StageOutcome(status=ObservationStatus.SUCCEEDED),
            )
        )
        self._owner._append_projected_run(result.run_id)

    def record_pre_result_failure(self, category: FailureCategory) -> None:
        if self._failed or self._projected or self._closed:
            return
        self._failed = True
        self._owner._append_failure(self._run_id, category)


class InMemoryObservability:
    """Thread-safe deterministic recorder with context-local parentage."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._next_observation_id = 1
        self._observations: list[RecordedObservation] = []
        self._projected_runs: list[str] = []
        self._pre_result_failures: list[tuple[str, FailureCategory]] = []
        self._closed = False
        self._current: ContextVar[int | None] = ContextVar(
            f"optima_in_memory_observation_{id(self)}",
            default=None,
        )

    @property
    def observations(self) -> tuple[RecordedObservation, ...]:
        """Return a stable snapshot of closed observations."""
        with self._lock:
            return tuple(self._observations)

    @property
    def projected_run_ids(self) -> tuple[str, ...]:
        """Return terminal run IDs in projection order."""
        with self._lock:
            return tuple(self._projected_runs)

    @property
    def pre_result_failures(self) -> tuple[tuple[str, FailureCategory], ...]:
        """Return bounded pre-result failures in observation order."""
        with self._lock:
            return tuple(self._pre_result_failures)

    def start_run(
        self,
        *,
        run_id: str,
        correlation_id: str,
    ) -> InMemoryRunObservation:
        return InMemoryRunObservation(
            self,
            run_id=run_id,
            correlation_id=correlation_id,
        )

    def instrument_fastapi(self, application: FastAPI) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _allocate_id(self) -> int:
        with self._lock:
            observation_id = self._next_observation_id
            self._next_observation_id += 1
            return observation_id

    def _append(self, observation: RecordedObservation) -> None:
        with self._lock:
            if not self._closed:
                self._observations.append(observation)

    def _append_projected_run(self, run_id: str) -> None:
        with self._lock:
            if not self._closed:
                self._projected_runs.append(run_id)

    def _append_failure(self, run_id: str, category: FailureCategory) -> None:
        with self._lock:
            if not self._closed:
                self._pre_result_failures.append((run_id, category))
