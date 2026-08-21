"""Run-history persistence contracts and adapters."""

from optima.storage.contracts import (
    MAX_RUN_HISTORY_LIST_LIMIT,
    RunHistoryAuthenticationError,
    RunHistoryConflictError,
    RunHistoryDocumentTooLargeError,
    RunHistoryError,
    RunHistoryErrorCode,
    RunHistoryInvalidDocumentError,
    RunHistoryNotFoundError,
    RunHistoryServiceUnavailableError,
    RunHistoryStore,
    RunHistoryThrottledError,
    RunHistoryTimeoutError,
)
from optima.storage.cosmos import (
    COSMOS_ITEM_MAX_BYTES,
    COSMOS_RUN_HISTORY_SCHEMA_VERSION,
    CosmosRunHistoryResources,
    CosmosRunHistoryStore,
    build_cosmos_run_history_resources,
)
from optima.storage.memory import InMemoryRunHistoryStore

__all__ = [
    "InMemoryRunHistoryStore",
    "MAX_RUN_HISTORY_LIST_LIMIT",
    "COSMOS_ITEM_MAX_BYTES",
    "COSMOS_RUN_HISTORY_SCHEMA_VERSION",
    "CosmosRunHistoryResources",
    "CosmosRunHistoryStore",
    "RunHistoryAuthenticationError",
    "RunHistoryConflictError",
    "RunHistoryDocumentTooLargeError",
    "RunHistoryError",
    "RunHistoryErrorCode",
    "RunHistoryInvalidDocumentError",
    "RunHistoryNotFoundError",
    "RunHistoryServiceUnavailableError",
    "RunHistoryStore",
    "RunHistoryThrottledError",
    "RunHistoryTimeoutError",
    "build_cosmos_run_history_resources",
]
