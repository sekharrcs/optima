"""Read-only Redis vector lookup for semantic-cache candidate evidence."""

import asyncio
import math
import struct
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Protocol

from pydantic import ValidationError

from optima.cache.contracts import SemanticCacheLookupRequest
from optima.domain.cache import CacheCandidate
from optima.domain.evaluation import EvaluationResult
from optima.domain.request_binding import RequestBinding

REDIS_CACHE_SCHEMA_VERSION = 1

_RETURN_FIELDS = (
    "schema_version",
    "source_run_id",
    "output_text",
    "request_binding_json",
    "prior_evaluation_json",
    "contract_compatible",
    "safe_to_reuse",
    "vector_distance",
)


class SemanticCacheEmbeddingProvider(Protocol):
    """Produce one provider-independent embedding for a lookup request."""

    async def embed(self, request: SemanticCacheLookupRequest) -> Sequence[float]:
        """Return the vector used only to retrieve candidate evidence."""
        ...


class RedisSearchClient(Protocol):
    """Narrow async Redis command surface used by the cache adapter."""

    async def execute_command(self, *args: object) -> object:
        """Execute one Redis command and return its decoded response."""
        ...


class RedisSemanticCacheInvalidResponseError(Exception):
    """Report malformed or contradictory Redis cache evidence safely."""

    def __init__(self) -> None:
        super().__init__("Redis semantic-cache response is invalid")


class RedisSemanticCache:
    """Retrieve one candidate from a pre-provisioned Redis vector index."""

    def __init__(
        self,
        client: RedisSearchClient,
        embedding_provider: SemanticCacheEmbeddingProvider,
        *,
        index_name: str,
        embedding_dimension: int,
        timeout_seconds: float,
    ) -> None:
        if not index_name:
            raise ValueError("Redis semantic-cache index name must not be empty")
        if embedding_dimension <= 0:
            raise ValueError(
                "Redis semantic-cache embedding dimension must be positive"
            )
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Redis semantic-cache timeout must be positive and finite")
        self._client = client
        self._embedding_provider = embedding_provider
        self._index_name = index_name
        self._embedding_dimension = embedding_dimension
        self._timeout_seconds = timeout_seconds

    async def lookup(
        self,
        request: SemanticCacheLookupRequest,
    ) -> CacheCandidate | None:
        """Run one bounded KNN lookup and return evidence without applying policy."""
        normalized_request = SemanticCacheLookupRequest.model_validate(request)
        async with asyncio.timeout(self._timeout_seconds):
            embedding = await self._embedding_provider.embed(normalized_request)
            query_vector = _encode_float32_vector(
                embedding,
                expected_dimension=self._embedding_dimension,
            )
            response = await self._client.execute_command(
                "FT.SEARCH",
                self._index_name,
                _vector_query(normalized_request),
                "PARAMS",
                2,
                "query_vector",
                query_vector,
                "SORTBY",
                "vector_distance",
                "ASC",
                "RETURN",
                len(_RETURN_FIELDS),
                *_RETURN_FIELDS,
                "LIMIT",
                0,
                1,
                "DIALECT",
                2,
            )
        return _decode_search_response(response)


def _vector_query(request: SemanticCacheLookupRequest) -> str:
    """Scope retrieval by typed profile facts without applying reuse gates."""
    return (
        f"(@task_type:{{{request.request_profile.task_type.value}}} "
        f"@complexity:{{{request.request_profile.complexity.value}}})"
        "=>[KNN 1 @embedding $query_vector AS vector_distance]"
    )


def _encode_float32_vector(
    values: Sequence[float],
    *,
    expected_dimension: int,
) -> bytes:
    """Encode one finite non-zero little-endian FLOAT32 vector exactly."""
    if isinstance(values, str | bytes | bytearray):
        raise ValueError("semantic-cache embedding must be a numeric sequence")
    if len(values) != expected_dimension:
        raise ValueError("semantic-cache embedding dimension does not match settings")
    converted: list[float] = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError("semantic-cache embedding values must be numeric")
        try:
            converted.append(float(value))
        except (TypeError, ValueError) as error:
            raise ValueError(
                "semantic-cache embedding values must be numeric"
            ) from error
    if any(not math.isfinite(value) for value in converted):
        raise ValueError("semantic-cache embedding values must be finite")
    try:
        encoded = struct.pack(f"<{expected_dimension}f", *converted)
    except (OverflowError, struct.error) as error:
        raise ValueError("semantic-cache embedding values must fit FLOAT32") from error
    float32_values = struct.unpack(f"<{expected_dimension}f", encoded)
    if any(not math.isfinite(value) for value in float32_values):
        raise ValueError("semantic-cache embedding values must fit FLOAT32")
    if math.sqrt(sum(value * value for value in float32_values)) == 0:
        raise ValueError("semantic-cache embedding must have a non-zero norm")
    return encoded


def _decode_search_response(response: object) -> CacheCandidate | None:
    """Decode the exact RESP2 FT.SEARCH shape and fail closed on corruption."""
    if not isinstance(response, list) or not response:
        raise RedisSemanticCacheInvalidResponseError
    total = response[0]
    if type(total) is not int or total < 0:
        raise RedisSemanticCacheInvalidResponseError
    if total == 0:
        if response != [0]:
            raise RedisSemanticCacheInvalidResponseError
        return None
    if total != 1 or len(response) != 3 or not isinstance(response[2], list):
        raise RedisSemanticCacheInvalidResponseError
    fields = _decode_field_pairs(response[2])
    if set(fields) != set(_RETURN_FIELDS):
        raise RedisSemanticCacheInvalidResponseError
    try:
        if _decode_text(fields["schema_version"]) != str(REDIS_CACHE_SCHEMA_VERSION):
            raise ValueError("unsupported schema version")
        distance = Decimal(_decode_text(fields["vector_distance"]))
        if not distance.is_finite() or not Decimal(0) <= distance <= Decimal(2):
            raise ValueError("invalid cosine distance")
        similarity = max(Decimal(0), Decimal(1) - distance)
        return CacheCandidate(
            source_run_id=_decode_text(fields["source_run_id"]),
            output_text=_decode_text(fields["output_text"]),
            request_binding=RequestBinding.model_validate_json(
                _decode_json(fields["request_binding_json"])
            ),
            similarity=float(similarity),
            prior_evaluation=EvaluationResult.model_validate_json(
                _decode_json(fields["prior_evaluation_json"])
            ),
            contract_compatible=_decode_boolean(fields["contract_compatible"]),
            safe_to_reuse=_decode_boolean(fields["safe_to_reuse"]),
        )
    except (
        InvalidOperation,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        ValidationError,
    ) as error:
        raise RedisSemanticCacheInvalidResponseError from error


def _decode_field_pairs(raw_fields: list[object]) -> dict[str, object]:
    """Build unique UTF-8 field pairs from one Redis hash result."""
    if len(raw_fields) % 2 != 0:
        raise RedisSemanticCacheInvalidResponseError
    fields: dict[str, object] = {}
    for index in range(0, len(raw_fields), 2):
        try:
            name = _decode_text(raw_fields[index])
        except (UnicodeDecodeError, TypeError) as error:
            raise RedisSemanticCacheInvalidResponseError from error
        if name in fields:
            raise RedisSemanticCacheInvalidResponseError
        fields[name] = raw_fields[index + 1]
    return fields


def _decode_text(value: object) -> str:
    """Decode one Redis text field without lossy replacement."""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise TypeError("Redis semantic-cache text field has an invalid type")


def _decode_boolean(value: object) -> bool:
    """Decode one canonical persisted boolean."""
    normalized = _decode_text(value)
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError("Redis semantic-cache boolean field is invalid")


def _decode_json(value: object) -> str | bytes | bytearray:
    """Narrow one Redis value to a Pydantic JSON input."""
    if isinstance(value, str | bytes | bytearray):
        return value
    raise TypeError("Redis semantic-cache JSON field has an invalid type")
