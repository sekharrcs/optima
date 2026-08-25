"""Failure isolation for observational adapters."""

import logging
from types import TracebackType
from typing import Self

from fastapi import FastAPI

from optima.domain.run import RunResult
from optima.observability.contracts import (
    FailureCategory,
    Observability,
    ObservationStage,
    RunObservation,
    StageObservation,
    StageOutcomeEvidence,
)
from optima.observability.noop import NO_OP_RUN, NO_OP_STAGE

_logger = logging.getLogger(__name__)


class FailureIsolatedStageObservation:
    """Contain every adapter failure within one stage observation."""

    def __init__(self, delegate: StageObservation) -> None:
        self._emission_delegate: StageObservation | None = delegate
        self._cleanup_delegate: StageObservation | None = delegate
        self._closed = False

    def __enter__(self) -> Self:
        delegate = self._cleanup_delegate
        if delegate is not None:
            try:
                delegate.__enter__()
            except Exception:
                self._emission_delegate = None
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
        delegate = self._cleanup_delegate
        self._cleanup_delegate = None
        if delegate is not None:
            try:
                delegate.__exit__(None, None, None)
            except Exception:
                return None

    def finish(self, outcome: StageOutcomeEvidence) -> None:
        delegate = self._emission_delegate
        if delegate is not None:
            try:
                delegate.finish(outcome)
            except Exception:
                self._emission_delegate = None


class FailureIsolatedRunObservation:
    """Contain adapter failures without changing the observed run."""

    def __init__(self, delegate: RunObservation) -> None:
        self._emission_delegate: RunObservation | None = delegate
        self._cleanup_delegate: RunObservation | None = delegate
        self._closed = False

    def __enter__(self) -> Self:
        delegate = self._cleanup_delegate
        if delegate is not None:
            try:
                delegate.__enter__()
            except Exception:
                self._emission_delegate = None
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
        delegate = self._cleanup_delegate
        self._cleanup_delegate = None
        if delegate is not None:
            try:
                delegate.__exit__(None, None, None)
            except Exception:
                return None

    def start_stage(self, stage: ObservationStage) -> StageObservation:
        delegate = self._emission_delegate
        if delegate is None:
            return NO_OP_STAGE
        try:
            return FailureIsolatedStageObservation(delegate.start_stage(stage))
        except Exception:
            self._emission_delegate = None
            return NO_OP_STAGE

    def project_result(self, result: RunResult) -> None:
        delegate = self._emission_delegate
        if delegate is not None:
            try:
                delegate.project_result(result)
            except Exception:
                self._emission_delegate = None
                _logger.warning(
                    "Terminal telemetry projection failed; telemetry may be incomplete"
                )

    def record_pre_result_failure(self, category: FailureCategory) -> None:
        delegate = self._emission_delegate
        if delegate is not None:
            try:
                delegate.record_pre_result_failure(category)
            except Exception:
                self._emission_delegate = None


class FailureIsolatedObservability:
    """Ensure telemetry can never change application behavior."""

    def __init__(self, delegate: Observability) -> None:
        self._delegate = delegate

    def start_run(
        self,
        *,
        run_id: str,
        correlation_id: str,
    ) -> RunObservation:
        try:
            delegate = self._delegate.start_run(
                run_id=run_id,
                correlation_id=correlation_id,
            )
        except Exception:
            return NO_OP_RUN
        return FailureIsolatedRunObservation(delegate)

    def instrument_fastapi(self, application: FastAPI) -> None:
        try:
            self._delegate.instrument_fastapi(application)
        except Exception:
            return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            return self._delegate.force_flush(timeout_millis)
        except Exception:
            return False

    def close(self) -> None:
        try:
            self._delegate.close()
        except Exception:
            return None
