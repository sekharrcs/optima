"""Azure Cosmos DB for NoSQL run-history adapter and resource composition."""

import json
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import (
    ClientAuthenticationError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import (
    CosmosClientTimeoutError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, ValidationError

from optima.config import AppSettings, CosmosAuthMode
from optima.domain.run import RunResult
from optima.storage.contracts import (
    MAX_RUN_HISTORY_LIST_LIMIT,
    RunHistoryAuthenticationError,
    RunHistoryConflictError,
    RunHistoryDocumentTooLargeError,
    RunHistoryError,
    RunHistoryInvalidDocumentError,
    RunHistoryNotFoundError,
    RunHistoryServiceUnavailableError,
    RunHistoryThrottledError,
    RunHistoryTimeoutError,
)

COSMOS_RUN_HISTORY_SCHEMA_VERSION = 1
COSMOS_ITEM_MAX_BYTES = 2 * 1024 * 1024
RECENT_RUNS_QUERY = (
    "SELECT TOP @limit c.id, c.schema_version, c.created_at, "
    "c.run_result_json FROM c "
    "ORDER BY c.created_at DESC, c.id ASC"
)


class CosmosContainer(Protocol):
    """Narrow async container surface required by run-history persistence."""

    async def create_item(
        self,
        body: dict[str, Any],
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        """Create one item without replacement semantics."""
        ...

    async def read_item(
        self,
        item: str,
        partition_key: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        """Point-read one item by ID and partition key."""
        ...

    def query_items(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterable[Mapping[str, Any]]:
        """Return an async iterable for one bounded parameterized query."""
        ...


class _RunHistoryDocument(BaseModel):
    """Versioned application-owned fields in one Cosmos item."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: StrictStr
    schema_version: StrictInt
    created_at: StrictStr
    run_result_json: StrictStr


class _AsyncCloseable(Protocol):
    async def close(self) -> None:
        """Release owned asynchronous resources."""
        ...


class CosmosRunHistoryStore:
    """Persist immutable RunResult evidence in a `/id` partitioned container."""

    def __init__(self, container: CosmosContainer) -> None:
        self._container = container

    async def save(self, result: RunResult) -> None:
        """Create one item, reconciling only an identical duplicate write."""
        normalized = RunResult.model_validate(result)
        document = _build_document(normalized)
        if _document_size_bytes(document) > COSMOS_ITEM_MAX_BYTES:
            raise RunHistoryDocumentTooLargeError
        try:
            await self._container.create_item(body=document)
        except CosmosResourceExistsError:
            await self._reconcile_duplicate(normalized)
        except RunHistoryError:
            raise
        except Exception as error:
            raise _translate_cosmos_error(error, not_found_is_missing=False) from error

    async def get(self, run_id: str) -> RunResult:
        """Point-read by exact ID/partition key and validate authoritative evidence."""
        try:
            document = await self._container.read_item(
                item=run_id,
                partition_key=run_id,
            )
        except RunHistoryError:
            raise
        except Exception as error:
            raise _translate_cosmos_error(error, not_found_is_missing=True) from error
        return _decode_document(document, expected_run_id=run_id)

    async def list_recent(self, limit: int) -> tuple[RunResult, ...]:
        """Run one bounded cross-partition query and validate every returned item."""
        if not 1 <= limit <= MAX_RUN_HISTORY_LIST_LIMIT:
            raise ValueError("run-history limit must be between 1 and 100")
        try:
            items = self._container.query_items(
                query=RECENT_RUNS_QUERY,
                parameters=[{"name": "@limit", "value": limit}],
                max_item_count=limit,
            )
            results: list[RunResult] = []
            async for document in items:
                if len(results) == limit:
                    break
                results.append(_decode_document(document))
            return tuple(results)
        except RunHistoryError:
            raise
        except Exception as error:
            raise _translate_cosmos_error(error, not_found_is_missing=False) from error

    async def _reconcile_duplicate(self, result: RunResult) -> None:
        """Treat a duplicate create as idempotent only for equal full evidence."""
        try:
            existing = await self.get(result.run_id)
        except RunHistoryNotFoundError as error:
            raise RunHistoryServiceUnavailableError from error
        if existing != result:
            raise RunHistoryConflictError


@dataclass
class CosmosRunHistoryResources:
    """Own one application-lifetime Cosmos client and selected credential."""

    store: CosmosRunHistoryStore
    client: _AsyncCloseable = field(repr=False)
    credential: AsyncTokenCredential | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def aclose(self) -> None:
        """Close the client and owned credential at most once."""
        if self._closed:
            return
        self._closed = True
        try:
            await self.client.close()
        finally:
            if self.credential is not None:
                await self.credential.close()


def build_cosmos_run_history_resources(
    settings: AppSettings,
) -> CosmosRunHistoryResources:
    """Compose one client from complete settings without credential fallback."""
    configuration = settings.cosmos_run_history_configuration()
    if configuration is None:
        raise ValueError("Cosmos run-history settings are not configured")

    credential: str | AsyncTokenCredential
    owned_credential: AsyncTokenCredential | None = None
    if configuration.auth_mode is CosmosAuthMode.ACCOUNT_KEY:
        if configuration.account_key is None:
            raise AssertionError("validated account-key configuration requires a key")
        credential = configuration.account_key.get_secret_value()
    elif configuration.auth_mode is CosmosAuthMode.AZURE_CLI:
        owned_credential = AzureCliCredential()
        credential = owned_credential
    else:
        owned_credential = ManagedIdentityCredential(
            client_id=configuration.managed_identity_client_id
        )
        credential = owned_credential

    client = CosmosClient(
        configuration.endpoint,
        credential=credential,
        timeout=configuration.timeout_seconds,
        retry_total=configuration.retry_total,
        retry_throttle_total=configuration.retry_total,
    )
    container = client.get_database_client(
        configuration.database_name
    ).get_container_client(configuration.container_name)
    store = CosmosRunHistoryStore(cast(CosmosContainer, container))
    return CosmosRunHistoryResources(
        store=store,
        client=cast(_AsyncCloseable, client),
        credential=owned_credential,
    )


def _build_document(result: RunResult) -> dict[str, Any]:
    """Build query metadata from the same authoritative validated result."""
    return {
        "id": result.run_id,
        "schema_version": COSMOS_RUN_HISTORY_SCHEMA_VERSION,
        "created_at": _canonical_utc(result.created_at),
        "run_result_json": result.model_dump_json(exclude_computed_fields=True),
    }


def _decode_document(
    raw_document: Mapping[str, Any],
    *,
    expected_run_id: str | None = None,
) -> RunResult:
    """Fail closed on unsupported, malformed, or contradictory persisted data."""
    try:
        document = _RunHistoryDocument.model_validate(raw_document)
        if document.schema_version != COSMOS_RUN_HISTORY_SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        result = RunResult.model_validate_json(document.run_result_json)
        if document.id != result.run_id:
            raise ValueError("document identity contradicts payload")
        if expected_run_id is not None and document.id != expected_run_id:
            raise ValueError("point-read identity contradicts requested run")
        if document.created_at != _canonical_utc(result.created_at):
            raise ValueError("ordering metadata contradicts payload")
        return result
    except (ValidationError, ValueError, TypeError) as error:
        raise RunHistoryInvalidDocumentError from error


def _canonical_utc(value: datetime) -> str:
    """Render timestamps in one lexically sortable UTC representation."""
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _document_size_bytes(document: Mapping[str, Any]) -> int:
    """Measure the complete compact SDK-compatible UTF-8 JSON representation."""
    return len(
        json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _translate_cosmos_error(
    error: Exception,
    *,
    not_found_is_missing: bool,
) -> RunHistoryError:
    """Translate Azure failures without preserving raw exception text or payloads."""
    if isinstance(error, CosmosResourceNotFoundError):
        if not_found_is_missing:
            return RunHistoryNotFoundError()
        return RunHistoryServiceUnavailableError()
    if isinstance(error, (CosmosClientTimeoutError, TimeoutError)):
        return RunHistoryTimeoutError()
    if isinstance(error, ClientAuthenticationError):
        return RunHistoryAuthenticationError()
    if isinstance(error, CosmosHttpResponseError):
        if error.status_code in {401, 403}:
            return RunHistoryAuthenticationError()
        if error.status_code in {408, 504}:
            return RunHistoryTimeoutError()
        if error.status_code == 429:
            return RunHistoryThrottledError()
        if error.status_code == 413:
            return RunHistoryDocumentTooLargeError()
        return RunHistoryServiceUnavailableError()
    if isinstance(error, (ServiceRequestError, ServiceResponseError)):
        return RunHistoryServiceUnavailableError()
    return RunHistoryServiceUnavailableError()
