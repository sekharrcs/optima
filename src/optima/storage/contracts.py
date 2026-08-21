"""Provider-independent run-history persistence contracts."""

from enum import StrEnum
from typing import Protocol, runtime_checkable

from optima.domain.run import RunResult

MAX_RUN_HISTORY_LIST_LIMIT = 100


class RunHistoryErrorCode(StrEnum):
    """Stable sanitized categories for run-history failures."""

    NOT_FOUND = "RUN_HISTORY_NOT_FOUND"
    CONFLICT = "RUN_HISTORY_CONFLICT"
    INVALID_DOCUMENT = "RUN_HISTORY_INVALID_DOCUMENT"
    AUTHENTICATION_FAILED = "RUN_HISTORY_AUTHENTICATION_FAILED"
    TIMED_OUT = "RUN_HISTORY_TIMED_OUT"
    THROTTLED = "RUN_HISTORY_THROTTLED"
    SERVICE_UNAVAILABLE = "RUN_HISTORY_SERVICE_UNAVAILABLE"
    DOCUMENT_TOO_LARGE = "RUN_HISTORY_DOCUMENT_TOO_LARGE"


class RunHistoryError(Exception):
    """Base error containing only stable, non-sensitive adapter facts."""

    def __init__(self, code: RunHistoryErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class RunHistoryNotFoundError(RunHistoryError):
    """Raised when one requested run does not exist."""

    def __init__(self) -> None:
        super().__init__(
            RunHistoryErrorCode.NOT_FOUND,
            "Run history entry was not found",
        )


class RunHistoryConflictError(RunHistoryError):
    """Raised when an existing run ID contains different immutable evidence."""

    def __init__(self) -> None:
        super().__init__(
            RunHistoryErrorCode.CONFLICT,
            "Run history already contains different evidence for this run ID",
        )


class RunHistoryInvalidDocumentError(RunHistoryError):
    """Raised when persisted evidence cannot be validated safely."""

    def __init__(self) -> None:
        super().__init__(
            RunHistoryErrorCode.INVALID_DOCUMENT,
            "Persisted run history is invalid",
        )


class RunHistoryAuthenticationError(RunHistoryError):
    """Raised when the configured identity cannot access run history."""

    def __init__(self) -> None:
        super().__init__(
            RunHistoryErrorCode.AUTHENTICATION_FAILED,
            "Run-history authentication or authorization failed",
        )


class RunHistoryTimeoutError(RunHistoryError):
    """Raised when a run-history operation exceeds its time budget."""

    def __init__(self) -> None:
        super().__init__(
            RunHistoryErrorCode.TIMED_OUT,
            "Run-history operation timed out",
        )


class RunHistoryThrottledError(RunHistoryError):
    """Raised when Cosmos DB exhausts its bounded throttle retries."""

    def __init__(self) -> None:
        super().__init__(
            RunHistoryErrorCode.THROTTLED,
            "Run-history service is throttling requests",
        )


class RunHistoryServiceUnavailableError(RunHistoryError):
    """Raised when run-history persistence is temporarily unavailable."""

    def __init__(self) -> None:
        super().__init__(
            RunHistoryErrorCode.SERVICE_UNAVAILABLE,
            "Run-history service is unavailable",
        )


class RunHistoryDocumentTooLargeError(RunHistoryError):
    """Raised when immutable evidence cannot fit in one persisted item."""

    def __init__(self) -> None:
        super().__init__(
            RunHistoryErrorCode.DOCUMENT_TOO_LARGE,
            "Run history evidence exceeds the supported item size",
        )


@runtime_checkable
class RunHistoryStore(Protocol):
    """Persist and retrieve immutable terminal run evidence."""

    async def save(self, result: RunResult) -> None:
        """Create one run record or verify an identical existing record."""
        ...

    async def get(self, run_id: str) -> RunResult:
        """Return one validated authoritative run result."""
        ...

    async def list_recent(self, limit: int) -> tuple[RunResult, ...]:
        """Return a bounded newest-first sequence with deterministic ties."""
        ...
