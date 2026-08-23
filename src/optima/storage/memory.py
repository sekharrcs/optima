"""Deterministic in-memory run history for local use and tests."""

from pydantic import ValidationError

from optima.domain.run import RunResult
from optima.storage.contracts import (
    MAX_RUN_HISTORY_LIST_LIMIT,
    RunHistoryConflictError,
    RunHistoryInvalidDocumentError,
    RunHistoryNotFoundError,
)


class InMemoryRunHistoryStore:
    """Store detached authoritative JSON rather than mutable model references."""

    def __init__(self) -> None:
        self._payloads: dict[str, str] = {}

    async def save(self, result: RunResult) -> None:
        """Create immutable evidence and permit only identical duplicate saves."""
        normalized = RunResult.model_validate(result)
        payload = normalized.model_dump_json(exclude_computed_fields=True)
        existing_payload = self._payloads.get(normalized.run_id)
        if existing_payload is None:
            self._payloads[normalized.run_id] = payload
            return
        if self._load(existing_payload) != normalized:
            raise RunHistoryConflictError

    async def get(self, run_id: str) -> RunResult:
        """Load a detached result and revalidate every persisted field."""
        payload = self._payloads.get(run_id)
        if payload is None:
            raise RunHistoryNotFoundError
        return self._load(payload)

    async def list_recent(self, limit: int) -> tuple[RunResult, ...]:
        """List at most ``limit`` newest-first results, breaking ties by run ID.

        Ties resolve on descending run ID to match the Cosmos ``sort_key`` order.
        """
        if not 1 <= limit <= MAX_RUN_HISTORY_LIST_LIMIT:
            raise ValueError("run-history limit must be between 1 and 100")
        results = [self._load(payload) for payload in self._payloads.values()]
        results.sort(
            key=lambda result: (result.created_at, result.run_id),
            reverse=True,
        )
        return tuple(results[:limit])

    @staticmethod
    def _load(payload: str) -> RunResult:
        """Validate one authoritative payload without leaking its contents."""
        try:
            return RunResult.model_validate_json(payload)
        except (ValidationError, ValueError, TypeError) as error:
            raise RunHistoryInvalidDocumentError from error
