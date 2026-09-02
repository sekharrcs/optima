"""Offline tests for provider-independent and Cosmos DB run history."""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.cosmos.exceptions import (
    CosmosClientTimeoutError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_runs_api import (
    RaisingSmallProvider,
    cache_eligible_profile,
    dependencies,
    request_payload,
    with_semantic_cache,
)

from optima.api.app import create_app
from optima.config import AppSettings, CosmosAuthMode
from optima.domain.run import RunResult
from optima.storage import (
    COSMOS_ITEM_MAX_BYTES,
    COSMOS_RUN_HISTORY_SCHEMA_VERSION,
    CosmosRunHistoryResources,
    CosmosRunHistoryStore,
    InMemoryRunHistoryStore,
    RunHistoryAuthenticationError,
    RunHistoryConflictError,
    RunHistoryDocumentTooLargeError,
    RunHistoryInvalidDocumentError,
    RunHistoryNotFoundError,
    RunHistoryServiceUnavailableError,
    RunHistoryStore,
    RunHistoryThrottledError,
    RunHistoryTimeoutError,
    build_cosmos_run_history_resources,
)
from optima.storage.cosmos import RECENT_RUNS_QUERY


class AsyncItems:
    """Deterministic async query result sequence."""

    def __init__(self, items: list[Mapping[str, Any]]) -> None:
        self._items = items

    async def __aiter__(self) -> AsyncIterator[Mapping[str, Any]]:
        for item in self._items:
            yield item


class FakeContainer:
    """Record the exact Cosmos container operations used by the adapter."""

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.read_calls: list[tuple[str, str]] = []
        self.query_calls: list[dict[str, Any]] = []
        self.read_document: Mapping[str, Any] | None = None
        self.query_documents: list[Mapping[str, Any]] = []
        self.create_error: Exception | None = None
        self.read_error: Exception | None = None
        self.query_error: Exception | None = None

    async def create_item(
        self,
        body: dict[str, Any],
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        self.create_calls.append(body)
        if self.create_error is not None:
            raise self.create_error
        return body

    async def read_item(
        self,
        item: str,
        partition_key: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        self.read_calls.append((item, partition_key))
        if self.read_error is not None:
            raise self.read_error
        if self.read_document is None:
            raise AssertionError("test must configure a read document")
        return self.read_document

    def query_items(self, *args: Any, **kwargs: Any) -> AsyncItems:
        self.query_calls.append({"args": args, **kwargs})
        if self.query_error is not None:
            raise self.query_error
        return AsyncItems(self.query_documents)


class RecordingRunHistoryStore(InMemoryRunHistoryStore):
    """Count API writes while retaining normal immutable storage behavior."""

    def __init__(self) -> None:
        super().__init__()
        self.save_calls: list[RunResult] = []

    async def save(self, result: RunResult) -> None:
        self.save_calls.append(result)
        await super().save(result)


class FailingRunHistoryStore:
    """Expose one sanitized configured failure for API error tests."""

    async def save(self, result: RunResult) -> None:
        raise RunHistoryServiceUnavailableError

    async def get(self, run_id: str) -> RunResult:
        raise RunHistoryInvalidDocumentError

    async def list_recent(self, limit: int) -> tuple[RunResult, ...]:
        raise RunHistoryInvalidDocumentError


class SaveErrorRunHistoryStore:
    """Raise one configured save error while counting persistence attempts."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.save_calls = 0

    async def save(self, result: RunResult) -> None:
        self.save_calls += 1
        raise self._error

    async def get(self, run_id: str) -> RunResult:
        raise RunHistoryNotFoundError

    async def list_recent(self, limit: int) -> tuple[RunResult, ...]:
        return ()


class BlockingRunHistoryStore:
    """Block persistence until its independent timeout cancels the attempt."""

    def __init__(self) -> None:
        self.save_calls = 0

    async def save(self, result: RunResult) -> None:
        self.save_calls += 1
        await asyncio.Event().wait()

    async def get(self, run_id: str) -> RunResult:
        raise RunHistoryNotFoundError

    async def list_recent(self, limit: int) -> tuple[RunResult, ...]:
        return ()


class TimeoutSmallProvider:
    """Produce a truthful terminal timed-out RunResult through the executor."""

    provider_name = "timeout-provider"
    deployment_name = "small"
    model_role = RaisingSmallProvider.model_role

    async def generate(self, request: object) -> object:
        raise TimeoutError("sensitive timeout detail")


class FakeCloseable:
    """Count asynchronous close calls."""

    def __init__(self, *, fail: bool = False) -> None:
        self.close_calls = 0
        self._fail = fail

    async def close(self) -> None:
        self.close_calls += 1
        if self._fail:
            raise RuntimeError("client close failed")


class FakeDatabase:
    """Return one configured fake container proxy."""

    def __init__(self, container: FakeContainer, name: str) -> None:
        self.container = container
        self.name = name
        self.container_name: str | None = None

    def get_container_client(self, container_name: str) -> FakeContainer:
        self.container_name = container_name
        return self.container


class FakeCosmosClient(FakeCloseable):
    """Capture client composition without opening a network transport."""

    instances: list["FakeCosmosClient"] = []

    def __init__(self, url: str, credential: object, **kwargs: object) -> None:
        super().__init__()
        self.url = url
        self.credential = credential
        self.kwargs = kwargs
        self.database = FakeDatabase(FakeContainer(), "")
        self.instances.append(self)

    def get_database_client(self, database_name: str) -> FakeDatabase:
        self.database.name = database_name
        return self.database


def completed_run(
    run_id: str = "run-history-1",
    *,
    created_at: datetime = datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
) -> RunResult:
    """Build one valid result through the real API execution path."""
    configured, _, _, _ = dependencies(0.93)
    store = InMemoryRunHistoryStore()
    configured = replace(
        configured,
        run_history_store=store,
        run_id_factory=lambda: run_id,
        correlation_id_factory=lambda: f"correlation-{run_id}",
        utc_now=lambda: created_at,
    )
    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(),
    )
    assert response.status_code == 200
    return asyncio.run(store.get(run_id))


def document_for(result: RunResult) -> dict[str, Any]:
    """Capture the exact document emitted by the Cosmos adapter."""
    container = FakeContainer()
    asyncio.run(CosmosRunHistoryStore(container).save(result))
    return container.create_calls[0]


def with_exact_cost(result: RunResult, cost: Decimal) -> RunResult:
    """Replace one valid model cost while retaining matching provenance."""
    usage = result.model_usages[0].model_copy(update={"calculated_cost": cost})
    return result.model_copy(update={"model_usages": (usage,)})


def test_in_memory_save_get_round_trip_preserves_exact_evidence() -> None:
    """Return a detached result with exact Decimal and optional measurements."""
    original = with_exact_cost(
        completed_run(),
        Decimal("0.1234567890123456789012345678"),
    )
    usage = original.model_usages[0].model_copy(
        update={
            "input_tokens": None,
            "output_tokens": None,
            "provider_total_tokens": None,
            "cached_tokens": None,
        }
    )
    original = original.model_copy(update={"model_usages": (usage,)})
    store = InMemoryRunHistoryStore()

    asyncio.run(store.save(original))
    restored = asyncio.run(store.get(original.run_id))

    assert restored == original
    assert restored is not original
    assert restored.model_usages[0].calculated_cost == Decimal(
        "0.1234567890123456789012345678"
    )
    assert restored.model_usages[0].input_tokens is None
    assert restored.model_usages[0].output_tokens is None
    assert restored.total_tokens is None


def test_in_memory_listing_is_bounded_newest_first_with_run_id_ties() -> None:
    """Use deterministic descending run IDs when timestamps are equal."""
    newest = completed_run("run-c", created_at=datetime(2026, 8, 21, tzinfo=UTC))
    tied_first = completed_run("run-a", created_at=datetime(2026, 8, 20, tzinfo=UTC))
    tied_second = completed_run("run-b", created_at=datetime(2026, 8, 20, tzinfo=UTC))
    store = InMemoryRunHistoryStore()
    for result in (tied_second, newest, tied_first):
        asyncio.run(store.save(result))

    recent = asyncio.run(store.list_recent(2))

    assert [result.run_id for result in recent] == ["run-c", "run-b"]


@pytest.mark.parametrize("limit", [0, 101])
def test_run_history_implementations_reject_unbounded_limits(limit: int) -> None:
    """Enforce the same hard list bound below the HTTP layer."""
    with pytest.raises(ValueError, match="between 1 and 100"):
        asyncio.run(InMemoryRunHistoryStore().list_recent(limit))
    with pytest.raises(ValueError, match="between 1 and 100"):
        asyncio.run(CosmosRunHistoryStore(FakeContainer()).list_recent(limit))


def test_in_memory_missing_duplicate_and_detachment_semantics() -> None:
    """Allow identical retries, reject changed evidence, and retain stored content."""
    original = completed_run()
    store = InMemoryRunHistoryStore()
    asyncio.run(store.save(original))
    asyncio.run(store.save(original.model_copy()))

    with pytest.raises(RunHistoryConflictError):
        asyncio.run(
            store.save(original.model_copy(update={"final_output": "changed output"}))
        )
    with pytest.raises(RunHistoryNotFoundError):
        asyncio.run(store.get("missing-run"))
    with pytest.raises(ValidationError):
        original.final_output = "mutation"
    assert asyncio.run(store.get(original.run_id)).final_output == "small output"


def test_cosmos_document_shape_identity_and_exact_decimal_payload() -> None:
    """Keep exact cost evidence in the authoritative JSON string payload."""
    result = with_exact_cost(
        completed_run(),
        Decimal("0.1234567890123456789012345678"),
    )

    document = document_for(result)

    assert set(document) == {
        "id",
        "schema_version",
        "created_at",
        "sort_key",
        "run_result_json",
    }
    assert document["id"] == result.run_id
    assert document["schema_version"] == COSMOS_RUN_HISTORY_SCHEMA_VERSION
    payload = json.loads(document["run_result_json"])
    assert payload["model_usages"][0]["calculated_cost"] == (
        "0.1234567890123456789012345678"
    )
    assert "total_calculated_cost" not in payload


def test_cosmos_create_only_duplicate_recovery_and_conflict() -> None:
    """Reconcile a create conflict without replacing persisted evidence."""
    result = completed_run()
    existing = document_for(result)
    container = FakeContainer()
    container.create_error = CosmosResourceExistsError(  # type: ignore[no-untyped-call]
        status_code=409,
        message="secret service detail",
    )
    container.read_document = existing
    store = CosmosRunHistoryStore(container)

    asyncio.run(store.save(result))

    assert len(container.create_calls) == 1
    assert container.read_calls == [(result.run_id, result.run_id)]
    conflicting = result.model_copy(update={"final_output": "different"})
    with pytest.raises(RunHistoryConflictError) as captured:
        asyncio.run(store.save(conflicting))
    assert "secret service detail" not in str(captured.value)


def test_cosmos_point_read_uses_exact_id_and_partition_key() -> None:
    """Perform a point read rather than an ID-only cross-partition query."""
    result = completed_run()
    container = FakeContainer()
    container.read_document = document_for(result)

    restored = asyncio.run(CosmosRunHistoryStore(container).get(result.run_id))

    assert restored == result
    assert container.read_calls == [(result.run_id, result.run_id)]
    assert container.query_calls == []


def test_cosmos_recent_query_is_parameterized_bounded_and_ordered() -> None:
    """Use fixed SQL, a TOP parameter, bounded pages, and deterministic ties."""
    first = completed_run("run-new")
    second = completed_run(
        "run-old",
        created_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    container = FakeContainer()
    container.query_documents = [document_for(first), document_for(second)]

    results = asyncio.run(CosmosRunHistoryStore(container).list_recent(2))

    assert results == (first, second)
    assert container.query_calls == [
        {
            "args": (),
            "query": (
                "SELECT TOP @limit c.id, c.schema_version, c.created_at, "
                "c.sort_key, c.run_result_json FROM c "
                "ORDER BY c.sort_key DESC"
            ),
            "parameters": [{"name": "@limit", "value": 2}],
            "max_item_count": 2,
        }
    ]


def test_cosmos_recent_query_orders_by_single_indexed_property() -> None:
    """Keep recent-history ordering runnable on a default `/id` container index."""
    order_by = RECENT_RUNS_QUERY.split("ORDER BY", 1)[1]
    assert order_by.count(",") == 0
    assert "c.sort_key" in order_by


def test_cosmos_sort_key_orders_newest_first_with_descending_run_id_ties() -> None:
    """Sort one descending key like Cosmos, matching the in-memory fake order."""
    newest = document_for(
        completed_run("run-a", created_at=datetime(2026, 8, 21, tzinfo=UTC))
    )
    tie_high = document_for(
        completed_run("run-z", created_at=datetime(2026, 8, 20, tzinfo=UTC))
    )
    tie_low = document_for(
        completed_run("run-a2", created_at=datetime(2026, 8, 20, tzinfo=UTC))
    )

    ordered = sorted(
        (tie_low, tie_high, newest),
        key=lambda document: document["sort_key"],
        reverse=True,
    )

    assert [document["id"] for document in ordered] == ["run-a", "run-z", "run-a2"]


def test_cosmos_list_recent_bounds_results_even_when_query_overreturns() -> None:
    """Bound the adapter result so a page size is never treated as a total limit."""
    runs = [completed_run(f"run-{index}") for index in range(3)]
    container = FakeContainer()
    container.query_documents = [document_for(run) for run in runs]

    results = asyncio.run(CosmosRunHistoryStore(container).list_recent(2))

    assert results == tuple(runs[:2])


def test_cosmos_create_timeout_maps_to_timeout_and_is_attempted_once() -> None:
    """Report an ambiguous create timeout without retrying the non-idempotent write."""
    result = completed_run()
    container = FakeContainer()
    container.create_error = CosmosClientTimeoutError()  # type: ignore[no-untyped-call]

    with pytest.raises(RunHistoryTimeoutError):
        asyncio.run(CosmosRunHistoryStore(container).save(result))

    assert len(container.create_calls) == 1


def test_cosmos_conflict_reconcile_fails_closed_on_corrupted_existing() -> None:
    """Never accept malformed existing evidence as an identical duplicate."""
    result = completed_run()
    corrupted = document_for(result)
    corrupted["run_result_json"] = "not-json"
    container = FakeContainer()
    container.create_error = CosmosResourceExistsError(  # type: ignore[no-untyped-call]
        status_code=409,
        message="conflict",
    )
    container.read_document = corrupted
    store = CosmosRunHistoryStore(container)

    with pytest.raises(RunHistoryInvalidDocumentError):
        asyncio.run(store.save(result))


def test_cosmos_read_accepts_valid_sort_key() -> None:
    """Return evidence when the persisted sort key reproduces the payload."""
    result = completed_run()
    container = FakeContainer()
    container.read_document = document_for(result)

    restored = asyncio.run(CosmosRunHistoryStore(container).get(result.run_id))

    assert restored == result


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.pop("sort_key"),
        lambda document: document.update(sort_key=1234),
        lambda document: document.update(
            sort_key=document["sort_key"].split("|", 1)[0] + "|run-tampered"
        ),
        lambda document: document.update(
            sort_key="2000-01-01T00:00:00.000000Z|" + document["id"]
        ),
    ],
    ids=["missing", "non-string", "run-id-corrupted", "timestamp-corrupted"],
)
def test_cosmos_point_read_rejects_invalid_sort_key(mutate: Any) -> None:
    """Fail closed when query-driving sort metadata cannot be reproduced."""
    result = completed_run()
    document = document_for(result)
    mutate(document)
    container = FakeContainer()
    container.read_document = document

    with pytest.raises(RunHistoryInvalidDocumentError):
        asyncio.run(CosmosRunHistoryStore(container).get(result.run_id))


def test_cosmos_list_rejects_contradictory_sort_key() -> None:
    """Reject list evidence whose sort key disagrees with the authoritative payload."""
    result = completed_run()
    document = document_for(result)
    document.update(sort_key="2000-01-01T00:00:00.000000Z|" + document["id"])
    container = FakeContainer()
    container.query_documents = [document]

    with pytest.raises(RunHistoryInvalidDocumentError):
        asyncio.run(CosmosRunHistoryStore(container).list_recent(5))


def test_cosmos_reconcile_rejects_existing_corrupted_sort_key() -> None:
    """Never accept a duplicate whose existing sort metadata is corrupted."""
    result = completed_run()
    corrupted = document_for(result)
    corrupted.update(sort_key="2000-01-01T00:00:00.000000Z|" + corrupted["id"])
    container = FakeContainer()
    container.create_error = CosmosResourceExistsError(  # type: ignore[no-untyped-call]
        status_code=409,
        message="conflict",
    )
    container.read_document = corrupted
    store = CosmosRunHistoryStore(container)

    with pytest.raises(RunHistoryInvalidDocumentError):
        asyncio.run(store.save(result))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.update(schema_version=999),
        lambda document: document.update(run_result_json="not-json"),
        lambda document: document.update(id="contradictory-id"),
        lambda document: document.update(created_at="2020-01-01T00:00:00.000000Z"),
    ],
)
def test_cosmos_rejects_unsupported_malformed_or_contradictory_documents(
    mutate: Any,
) -> None:
    """Fail closed before returning any untrusted stored evidence."""
    result = completed_run()
    document = document_for(result)
    mutate(document)
    container = FakeContainer()
    container.read_document = document

    with pytest.raises(RunHistoryInvalidDocumentError):
        asyncio.run(CosmosRunHistoryStore(container).get(result.run_id))


def test_cosmos_rejects_oversized_document_before_create() -> None:
    """Reject the complete UTF-8 JSON item instead of truncating evidence."""
    result = completed_run().model_copy(
        update={"final_output": "x" * COSMOS_ITEM_MAX_BYTES}
    )
    container = FakeContainer()

    with pytest.raises(RunHistoryDocumentTooLargeError):
        asyncio.run(CosmosRunHistoryStore(container).save(result))

    assert container.create_calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            CosmosResourceNotFoundError(  # type: ignore[no-untyped-call]
                status_code=404,
                message="raw payload",
            ),
            RunHistoryNotFoundError,
        ),
        (
            ClientAuthenticationError(message="credential secret"),
            RunHistoryAuthenticationError,
        ),
        (
            CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=403,
                message="account key",
            ),
            RunHistoryAuthenticationError,
        ),
        (TimeoutError("raw timeout"), RunHistoryTimeoutError),
        (
            CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=408,
                message="raw timeout",
            ),
            RunHistoryTimeoutError,
        ),
        (
            CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=429,
                message="raw output",
            ),
            RunHistoryThrottledError,
        ),
        (
            CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=503,
                message="raw output",
            ),
            RunHistoryServiceUnavailableError,
        ),
        (ServiceRequestError("raw endpoint"), RunHistoryServiceUnavailableError),
        (ServiceResponseError("raw response"), RunHistoryServiceUnavailableError),
    ],
)
def test_cosmos_maps_failures_without_raw_details(
    error: Exception,
    expected: type[Exception],
) -> None:
    """Expose stable categories without credentials, SDK text, or payloads."""
    container = FakeContainer()
    container.read_error = error

    with pytest.raises(expected) as captured:
        asyncio.run(CosmosRunHistoryStore(container).get("run-secret"))

    message = str(captured.value)
    assert "credential secret" not in message
    assert "account key" not in message
    assert "raw" not in message
    assert "run-secret" not in message


def cosmos_settings(auth_mode: CosmosAuthMode, **updates: object) -> AppSettings:
    """Build complete explicit Cosmos settings for one authentication mode."""
    values: dict[str, object] = {
        "cosmos_endpoint": "https://optima.documents.azure.com:443/",
        "cosmos_database_name": "optima",
        "cosmos_container_name": "runs",
        "cosmos_auth_mode": auth_mode,
    }
    values.update(updates)
    return AppSettings.model_validate(values)


def test_cosmos_configuration_is_optional_and_supports_explicit_modes() -> None:
    """Allow complete absence and each selected credential without fallback."""
    assert AppSettings().cosmos_run_history_configuration() is None
    configurations = (
        cosmos_settings(
            CosmosAuthMode.ACCOUNT_KEY,
            cosmos_account_key="fake-account-key",
        ).cosmos_run_history_configuration(),
        cosmos_settings(CosmosAuthMode.AZURE_CLI).cosmos_run_history_configuration(),
        cosmos_settings(
            CosmosAuthMode.MANAGED_IDENTITY,
            cosmos_managed_identity_client_id="managed-client-id",
        ).cosmos_run_history_configuration(),
    )

    assert [
        configuration.auth_mode for configuration in configurations if configuration
    ]
    assert configurations[0] is not None
    assert configurations[0].account_key is not None
    assert "fake-account-key" not in repr(configurations[0])


@pytest.mark.parametrize(
    "updates",
    [
        {"cosmos_endpoint": "https://optima.documents.azure.com/"},
        {
            "cosmos_endpoint": "http://optima.documents.azure.com/",
            "cosmos_database_name": "optima",
            "cosmos_container_name": "runs",
            "cosmos_auth_mode": "AZURE_CLI",
        },
        {
            "cosmos_endpoint": "https://user:key@optima.documents.azure.com/",
            "cosmos_database_name": "optima",
            "cosmos_container_name": "runs",
            "cosmos_auth_mode": "AZURE_CLI",
        },
        {
            "cosmos_endpoint": "https://optima.documents.azure.com/",
            "cosmos_database_name": "optima",
            "cosmos_container_name": "runs",
            "cosmos_auth_mode": "ACCOUNT_KEY",
        },
        {
            "cosmos_endpoint": "https://optima.documents.azure.com/",
            "cosmos_database_name": "optima",
            "cosmos_container_name": "runs",
            "cosmos_auth_mode": "AZURE_CLI",
            "cosmos_account_key": "mixed-secret",
        },
        {
            "cosmos_endpoint": "https://optima.documents.azure.com/",
            "cosmos_database_name": "optima",
            "cosmos_container_name": "runs",
            "cosmos_auth_mode": "AZURE_CLI",
            "cosmos_managed_identity_client_id": "forbidden-client-id",
        },
        {
            "cosmos_endpoint": "https://optima.documents.azure.com/",
            "cosmos_database_name": "optima",
            "cosmos_container_name": "runs",
            "cosmos_auth_mode": "AZURE_CLI",
            "cosmos_history_list_limit": 101,
        },
        {
            "cosmos_endpoint": "https://optima.documents.azure.com/",
            "cosmos_database_name": "optima",
            "cosmos_container_name": "runs",
            "cosmos_auth_mode": "AZURE_CLI",
            "cosmos_retry_total": 0,
        },
    ],
)
def test_cosmos_configuration_rejects_partial_unsafe_or_mixed_values(
    updates: dict[str, object],
) -> None:
    """Fail closed for ambiguous credentials, endpoints, and operational bounds."""
    with pytest.raises(ValidationError):
        AppSettings.model_validate(updates)


def test_cosmos_resources_close_client_and_credential_exactly_once() -> None:
    """Release each application-owned async resource once."""
    container = FakeContainer()
    client = FakeCloseable()
    credential = FakeCloseable()
    resources = CosmosRunHistoryResources(
        store=CosmosRunHistoryStore(container),
        client=client,
        credential=credential,  # type: ignore[arg-type]
    )

    asyncio.run(resources.aclose())
    asyncio.run(resources.aclose())

    assert client.close_calls == 1
    assert credential.close_calls == 1


def test_cosmos_resources_close_credential_when_client_close_fails() -> None:
    """Release identity transport even when Cosmos transport cleanup fails."""
    client = FakeCloseable(fail=True)
    credential = FakeCloseable()
    resources = CosmosRunHistoryResources(
        store=CosmosRunHistoryStore(FakeContainer()),
        client=client,
        credential=credential,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="client close failed"):
        asyncio.run(resources.aclose())

    assert client.close_calls == 1
    assert credential.close_calls == 1


@pytest.mark.parametrize(
    ("auth_mode", "updates", "expected_credential", "expected_client_id"),
    [
        (
            CosmosAuthMode.ACCOUNT_KEY,
            {"cosmos_account_key": "fake-account-key"},
            "ACCOUNT_KEY",
            None,
        ),
        (CosmosAuthMode.AZURE_CLI, {}, "AZURE_CLI", None),
        (CosmosAuthMode.MANAGED_IDENTITY, {}, "MANAGED_IDENTITY", None),
        (
            CosmosAuthMode.MANAGED_IDENTITY,
            {"cosmos_managed_identity_client_id": "managed-client-id"},
            "MANAGED_IDENTITY",
            "managed-client-id",
        ),
    ],
)
def test_cosmos_composition_uses_only_selected_authentication(
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: CosmosAuthMode,
    updates: dict[str, object],
    expected_credential: str,
    expected_client_id: str | None,
) -> None:
    """Create one explicit identity or key without probing a credential chain."""
    created: list[tuple[str, str | None, FakeCloseable]] = []

    def cli_credential() -> FakeCloseable:
        credential = FakeCloseable()
        created.append(("AZURE_CLI", None, credential))
        return credential

    def managed_credential(*, client_id: str | None) -> FakeCloseable:
        credential = FakeCloseable()
        created.append(("MANAGED_IDENTITY", client_id, credential))
        return credential

    FakeCosmosClient.instances.clear()
    monkeypatch.setattr("optima.storage.cosmos.CosmosClient", FakeCosmosClient)
    monkeypatch.setattr("optima.storage.cosmos.AzureCliCredential", cli_credential)
    monkeypatch.setattr(
        "optima.storage.cosmos.ManagedIdentityCredential",
        managed_credential,
    )

    resources = build_cosmos_run_history_resources(
        cosmos_settings(auth_mode, **updates)
    )

    assert len(FakeCosmosClient.instances) == 1
    client = FakeCosmosClient.instances[0]
    assert client.url == "https://optima.documents.azure.com:443/"
    assert client.database.name == "optima"
    assert client.database.container_name == "runs"
    assert client.kwargs == {
        "timeout": 10.0,
        "retry_total": 3,
        "retry_throttle_total": 3,
    }
    if expected_credential == "ACCOUNT_KEY":
        assert client.credential == "fake-account-key"
        assert created == []
    else:
        assert [(kind, client_id) for kind, client_id, _ in created] == [
            (expected_credential, expected_client_id)
        ]
        assert client.credential is created[0][2]

    asyncio.run(resources.aclose())
    assert client.close_calls == 1
    if created:
        assert created[0][2].close_calls == 1


def test_cosmos_composition_requires_explicit_configuration() -> None:
    """Keep default local composition free from Azure credential discovery."""
    with pytest.raises(ValueError, match="not configured"):
        build_cosmos_run_history_resources(AppSettings())


def test_api_persists_completed_result_once_without_changing_execution() -> None:
    """Save the exact returned result after unchanged provider/evaluator calls."""
    configured, small, strong, evaluator = dependencies(0.93)
    store = RecordingRunHistoryStore()
    configured = replace(configured, run_history_store=store)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(),
    )

    assert response.status_code == 200
    assert response.headers["X-OPTIMA-Run-History"] == "PERSISTED"
    assert "X-OPTIMA-Run-History-Error" not in response.headers
    assert len(store.save_calls) == 1
    persisted = asyncio.run(store.get("run-api-1"))
    assert response.json() == persisted.model_dump(mode="json")
    assert len(small.calls) == 1
    assert len(strong.calls) == 0
    assert len(evaluator.calls) == 1


def test_api_persists_cache_disabled_evidence_without_embedding_usage() -> None:
    """Store the exact disabled planner and runtime evidence without cache claims."""
    configured, small, strong, evaluator = dependencies(0.93)
    store = RecordingRunHistoryStore()
    configured = replace(configured, run_history_store=store)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(request_profile=cache_eligible_profile()),
    )

    assert response.status_code == 200
    assert response.headers["X-OPTIMA-Run-History"] == "PERSISTED"
    persisted = asyncio.run(store.get("run-api-1"))
    assert persisted.semantic_cache is not None
    assert persisted.semantic_cache.outcome.value == "DISABLED_BYPASSED"
    assert persisted.semantic_cache.embedding_attempt is None
    assert (
        persisted.execution_plan.decision_evidence.module_states.semantic_cache_enabled
        is False
    )
    assert all(step.step_type.value != "SEMANTIC_CACHE" for step in persisted.steps)
    assert persisted.total_calculated_cost == Decimal("0.001000")
    assert len(small.calls) == 1
    assert len(strong.calls) == 0
    assert len(evaluator.calls) == 1


@pytest.mark.parametrize(
    ("provider", "expected_status"),
    [
        (RaisingSmallProvider(), "FAILED"),
        (TimeoutSmallProvider(), "TIMED_OUT"),
    ],
)
def test_api_persists_truthful_terminal_failures(
    provider: Any,
    expected_status: str,
) -> None:
    """Persist failed and timed-out results only after the executor constructs them."""
    configured, _, _, _ = dependencies(0.93)
    store = RecordingRunHistoryStore()
    configured = replace(
        configured,
        small_provider=provider,
        run_history_store=store,
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == expected_status
    assert response.headers["X-OPTIMA-Run-History"] == "PERSISTED"
    assert len(store.save_calls) == 1
    persisted = asyncio.run(store.get("run-api-1"))
    assert persisted.status.value == expected_status
    assert persisted.final_output is None
    assert persisted.contract_met is None


def test_api_returns_completed_result_when_persistence_unavailable() -> None:
    """Return the exact completed result with a sanitized FAILED persistence header."""
    configured, small, strong, evaluator = dependencies(0.93)
    store = SaveErrorRunHistoryStore(RunHistoryServiceUnavailableError())
    configured = replace(configured, run_history_store=store)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run-api-1"
    assert body["status"] == "COMPLETED"
    assert "persistence_error" not in body
    assert response.headers["X-OPTIMA-Run-History"] == "FAILED"
    assert (
        response.headers["X-OPTIMA-Run-History-Error"]
        == "RUN_HISTORY_SERVICE_UNAVAILABLE"
    )
    assert store.save_calls == 1
    assert len(small.calls) == 1
    assert len(strong.calls) == 0
    assert len(evaluator.calls) == 1


def test_api_returns_completed_result_when_persistence_exceeds_its_timeout() -> None:
    """Never convert completed paid work into a retryable execution timeout."""
    configured, small, strong, evaluator = dependencies(0.93)
    store = BlockingRunHistoryStore()
    configured = replace(
        configured,
        settings=configured.settings.model_copy(
            update={"cosmos_timeout_seconds": 0.01}
        ),
        run_history_store=store,
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.headers["X-OPTIMA-Run-History"] == "FAILED"
    assert response.headers["X-OPTIMA-Run-History-Error"] == "RUN_HISTORY_TIMED_OUT"
    assert store.save_calls == 1
    assert len(small.calls) == 1
    assert strong.calls == ()
    assert len(evaluator.calls) == 1


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (RunHistoryTimeoutError(), "RUN_HISTORY_TIMED_OUT"),
        (RunHistoryThrottledError(), "RUN_HISTORY_THROTTLED"),
        (RunHistoryAuthenticationError(), "RUN_HISTORY_AUTHENTICATION_FAILED"),
        (RunHistoryConflictError(), "RUN_HISTORY_CONFLICT"),
        (RunHistoryInvalidDocumentError(), "RUN_HISTORY_INVALID_DOCUMENT"),
        (RunHistoryDocumentTooLargeError(), "RUN_HISTORY_DOCUMENT_TOO_LARGE"),
    ],
)
def test_api_reports_sanitized_persistence_error_headers(
    error: Exception,
    expected_code: str,
) -> None:
    """Surface each sanitized persistence error code without failing the response."""
    configured, small, _, evaluator = dependencies(0.93)
    store = SaveErrorRunHistoryStore(error)
    configured = replace(configured, run_history_store=store)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(),
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-api-1"
    assert response.headers["X-OPTIMA-Run-History"] == "FAILED"
    assert response.headers["X-OPTIMA-Run-History-Error"] == expected_code
    assert store.save_calls == 1
    assert len(small.calls) == 1
    assert len(evaluator.calls) == 1


def test_api_maps_unexpected_persistence_exception_without_leaking_detail() -> None:
    """Convert an unexpected adapter error to a safe service-unavailable code."""
    configured, small, _, _ = dependencies(0.93)
    store = SaveErrorRunHistoryStore(RuntimeError("sensitive adapter detail"))
    configured = replace(configured, run_history_store=store)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(),
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-api-1"
    assert response.headers["X-OPTIMA-Run-History"] == "FAILED"
    assert (
        response.headers["X-OPTIMA-Run-History-Error"]
        == "RUN_HISTORY_SERVICE_UNAVAILABLE"
    )
    assert "sensitive adapter detail" not in str(dict(response.headers))
    assert store.save_calls == 1


def test_api_reports_not_configured_persistence_header() -> None:
    """Report NOT_CONFIGURED and still return the completed result cloud-free."""
    configured, _, _, _ = dependencies(0.93)

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(),
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-api-1"
    assert response.headers["X-OPTIMA-Run-History"] == "NOT_CONFIGURED"
    assert "X-OPTIMA-Run-History-Error" not in response.headers


@pytest.mark.parametrize(
    ("provider", "expected_status"),
    [
        (RaisingSmallProvider(), "FAILED"),
        (TimeoutSmallProvider(), "TIMED_OUT"),
    ],
)
def test_api_returns_terminal_failure_result_even_when_persistence_fails(
    provider: Any,
    expected_status: str,
) -> None:
    """Return the unchanged terminal result with a FAILED persistence header."""
    configured, _, _, _ = dependencies(0.93)
    store = SaveErrorRunHistoryStore(RunHistoryServiceUnavailableError())
    configured = replace(
        configured,
        small_provider=provider,
        run_history_store=store,
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == expected_status
    assert body["final_output"] is None
    assert body["contract_met"] is None
    assert response.headers["X-OPTIMA-Run-History"] == "FAILED"
    assert store.save_calls == 1


def test_api_skips_persistence_when_execution_fails_before_result() -> None:
    """Do not save or report persistence when execution fails before a result."""
    configured, small, _, evaluator = dependencies(0.93)
    store = RecordingRunHistoryStore()
    configured = replace(
        with_semantic_cache(configured, None),
        run_history_store=store,
    )

    response = TestClient(create_app(execution_dependencies=configured)).post(
        "/api/v1/runs",
        json=request_payload(request_profile=cache_eligible_profile()),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SEMANTIC_CACHE_NOT_CONFIGURED"
    assert store.save_calls == []
    assert "X-OPTIMA-Run-History" not in response.headers
    assert small.calls == ()
    assert evaluator.calls == ()


def test_api_history_point_read_list_and_missing_behavior() -> None:
    """Expose validated point reads and a strictly bounded recent list."""
    configured, _, _, _ = dependencies(0.93)
    store = InMemoryRunHistoryStore()
    older = completed_run(
        "run-old",
        created_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    newer = completed_run("run-new")
    asyncio.run(store.save(older))
    asyncio.run(store.save(newer))
    client = TestClient(
        create_app(execution_dependencies=replace(configured, run_history_store=store))
    )

    point = client.get("/api/v1/runs/run-old")
    recent = client.get("/api/v1/runs", params={"limit": 1})
    missing = client.get("/api/v1/runs/missing")

    assert point.status_code == 200
    assert point.json() == older.model_dump(mode="json")
    assert recent.status_code == 200
    assert [result["run_id"] for result in recent.json()] == ["run-new"]
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "RUN_NOT_FOUND"


def test_api_omitted_limit_uses_configured_maximum_below_default() -> None:
    """Do not reject an omitted limit when deployment policy is below 50."""
    configured, _, _, _ = dependencies(0.93)
    store = InMemoryRunHistoryStore()
    asyncio.run(store.save(completed_run("run-one")))
    configured = replace(
        configured,
        settings=AppSettings(
            semantic_cache_enabled=False,
            context_reduction_enabled=False,
            historical_policy_enabled=False,
            cosmos_history_list_limit=1,
        ),
        run_history_store=store,
    )

    response = TestClient(create_app(execution_dependencies=configured)).get(
        "/api/v1/runs"
    )

    assert response.status_code == 200
    assert [result["run_id"] for result in response.json()] == ["run-one"]


def test_api_history_unconfigured_limit_and_corruption_fail_closed() -> None:
    """Use clear 503/422/500 responses without fabricating run evidence."""
    configured, _, _, _ = dependencies(0.93)
    unconfigured = TestClient(create_app(execution_dependencies=configured))
    corrupt = TestClient(
        create_app(
            execution_dependencies=replace(
                configured,
                run_history_store=FailingRunHistoryStore(),
            )
        )
    )

    unavailable = unconfigured.get("/api/v1/runs")
    excessive = corrupt.get("/api/v1/runs", params={"limit": 51})
    invalid = corrupt.get("/api/v1/runs/run-corrupt")

    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "RUN_HISTORY_NOT_CONFIGURED"
    assert excessive.status_code == 422
    assert excessive.json()["detail"]["code"] == "RUN_HISTORY_LIMIT_EXCEEDED"
    assert invalid.status_code == 500
    assert invalid.json()["detail"] == {
        "code": "RUN_HISTORY_INVALID_DOCUMENT",
        "message": "Persisted run history is invalid",
        "facts": {},
    }


def test_run_history_store_protocol_accepts_both_implementations() -> None:
    """Keep local and Cosmos implementations substitutable at the API boundary."""
    assert isinstance(InMemoryRunHistoryStore(), RunHistoryStore)
    assert isinstance(CosmosRunHistoryStore(FakeContainer()), RunHistoryStore)


def test_in_memory_listing_uses_true_datetime_order_not_insert_order() -> None:
    """Keep ordering correct across offsets and out-of-order saves."""
    store = InMemoryRunHistoryStore()
    later = completed_run(
        "run-later",
        created_at=datetime(2026, 8, 21, 0, 0, tzinfo=UTC),
    )
    earlier = completed_run(
        "run-earlier",
        created_at=later.created_at - timedelta(microseconds=1),
    )
    asyncio.run(store.save(later))
    asyncio.run(store.save(earlier))

    assert [item.run_id for item in asyncio.run(store.list_recent(2))] == [
        "run-later",
        "run-earlier",
    ]
