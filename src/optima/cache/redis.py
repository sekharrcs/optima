"""Read-only Redis vector lookup for semantic-cache candidate evidence."""

import asyncio
import math
import struct
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Protocol

from pydantic import ValidationError

from optima.cache.contracts import (
    EmbeddingProviderResult,
    SemanticCacheEmbeddingProvider,
    SemanticCacheLookupError,
    SemanticCacheLookupRequest,
    SemanticCacheLookupResult,
    SemanticCacheLookupTimeout,
)
from optima.domain.cache import CacheCandidate
from optima.domain.embedding import EmbeddingAttempt, EmbeddingProfile, EmbeddingUsage
from optima.domain.evaluation import EvaluationResult
from optima.domain.request_binding import RequestBinding

REDIS_CACHE_SCHEMA_VERSION = 1
REDIS_CACHE_KEY_PREFIX = "optima:semantic-cache:"
REDIS_CACHE_TAG_FIELDS = (
    "schema_version",
    "embedding_profile",
    "task_type",
    "complexity",
)
REDIS_CACHE_VECTOR_FIELD = "embedding"
REDIS_CACHE_VECTOR_ALGORITHM = "FLAT"
REDIS_CACHE_VECTOR_TYPE = "FLOAT32"
REDIS_CACHE_DISTANCE_METRIC = "COSINE"

_RETURN_FIELDS = (
    "schema_version",
    "source_run_id",
    "output_text",
    "request_binding_json",
    "prior_evaluation_json",
    "contract_compatible",
    "safe_to_reuse",
    "embedding_profile",
    "vector_distance",
)


class RedisSearchClient(Protocol):
    """Narrow async Redis command surface used by the cache adapter."""

    async def execute_command(self, *args: object) -> object:
        """Execute one Redis command and return its decoded response."""
        ...


class RedisSemanticCacheInvalidResponseError(SemanticCacheLookupError):
    """Report malformed or contradictory Redis cache evidence safely."""

    def __init__(self, embedding_attempt: EmbeddingAttempt | None = None) -> None:
        super().__init__(
            embedding_attempt,
            message="Redis semantic-cache response is invalid",
        )


class RedisSemanticCache:
    """Retrieve one candidate from a pre-provisioned Redis vector index."""

    def __init__(
        self,
        client: RedisSearchClient,
        embedding_provider: SemanticCacheEmbeddingProvider,
        *,
        index_name: str,
        embedding_profile: EmbeddingProfile,
        timeout_seconds: float,
    ) -> None:
        if not index_name:
            raise ValueError("Redis semantic-cache index name must not be empty")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Redis semantic-cache timeout must be positive and finite")
        self._client = client
        self._embedding_provider = embedding_provider
        self._index_name = index_name
        self._embedding_profile = embedding_profile
        self._timeout_seconds = timeout_seconds

    async def lookup(
        self,
        request: SemanticCacheLookupRequest,
    ) -> SemanticCacheLookupResult:
        """Run one bounded KNN lookup and return evidence without applying policy."""
        normalized_request = SemanticCacheLookupRequest.model_validate(request)
        embedding_attempt: EmbeddingAttempt | None = None
        try:
            async with asyncio.timeout(self._timeout_seconds):
                started = perf_counter()
                embedding_result = await self._embedding_provider.embed(
                    normalized_request
                )
                embedding_usage = _build_embedding_usage(
                    normalized_request,
                    embedding_result,
                    latency_ms=_elapsed_ms(perf_counter(), started),
                )
                embedding_attempt = EmbeddingAttempt(
                    invoked=True,
                    outbound_attempted=True,
                    usage=embedding_usage,
                )
                if embedding_result.profile != self._embedding_profile:
                    raise SemanticCacheLookupError(
                        embedding_attempt,
                        message="embedding profile does not match configured profile",
                    )
                query_vector = _encode_float32_vector(
                    embedding_result.vector,
                    expected_dimension=self._embedding_profile.dimension,
                )
                response = await self._client.execute_command(
                    "FT.SEARCH",
                    self._index_name,
                    _vector_query(normalized_request, self._embedding_profile),
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
        except SemanticCacheLookupError:
            raise
        except TimeoutError as error:
            raise SemanticCacheLookupTimeout(
                _attempt_after_error(embedding_attempt, error)
            ) from error
        except Exception as error:
            raise SemanticCacheLookupError(
                _attempt_after_error(embedding_attempt, error)
            ) from error
        candidate = _decode_search_response(
            response,
            self._embedding_profile,
            embedding_attempt,
        )
        return SemanticCacheLookupResult(
            candidate=candidate,
            embedding_attempt=embedding_attempt,
        )


def _attempt_after_error(
    existing: EmbeddingAttempt | None,
    error: BaseException,
) -> EmbeddingAttempt | None:
    """Return the embedding attempt to attach to a failed or timed-out lookup.

    A measured attempt is preserved verbatim. Otherwise the embedding never
    returned usage, so the attempt is recorded as possibly-paid unless the
    provider proved the failure happened before any outbound request.
    """
    if existing is not None:
        return existing
    outbound_attempted = bool(getattr(error, "outbound_attempted", True))
    return EmbeddingAttempt(
        invoked=True,
        outbound_attempted=outbound_attempted,
        usage=None,
    )


def _build_embedding_usage(
    request: SemanticCacheLookupRequest,
    result: EmbeddingProviderResult,
    *,
    latency_ms: int,
) -> EmbeddingUsage:
    """Record the measured facts of one completed embedding request."""
    return EmbeddingUsage(
        run_id=request.run_id,
        provider=result.provider,
        deployment=result.profile.deployment,
        embedding_profile=result.profile.identity,
        request_id=result.request_id,
        input_tokens=result.input_tokens,
        latency_ms=latency_ms,
    )


def _elapsed_ms(now: float, started: float) -> int:
    """Return non-negative elapsed milliseconds between two monotonic reads."""
    return max(0, int(round((now - started) * 1000)))


def _vector_query(
    request: SemanticCacheLookupRequest,
    profile: EmbeddingProfile,
) -> str:
    """Scope retrieval by schema, profile facts, and embedding identity."""
    return (
        f"(@schema_version:{{{REDIS_CACHE_SCHEMA_VERSION}}} "
        f"@task_type:{{{request.request_profile.task_type.value}}} "
        f"@complexity:{{{request.request_profile.complexity.value}}} "
        f"@embedding_profile:{{{profile.identity}}})"
        f"=>[KNN 1 @{REDIS_CACHE_VECTOR_FIELD} $query_vector AS vector_distance]"
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


def _decode_search_response(
    response: object,
    profile: EmbeddingProfile,
    embedding_attempt: EmbeddingAttempt | None,
) -> CacheCandidate | None:
    """Decode the exact RESP2 FT.SEARCH shape and fail closed on corruption."""
    if not isinstance(response, list) or not response:
        raise RedisSemanticCacheInvalidResponseError(embedding_attempt)
    total = response[0]
    if type(total) is not int or total < 0:
        raise RedisSemanticCacheInvalidResponseError(embedding_attempt)
    if total == 0:
        if response != [0]:
            raise RedisSemanticCacheInvalidResponseError(embedding_attempt)
        return None
    if total != 1 or len(response) != 3 or not isinstance(response[2], list):
        raise RedisSemanticCacheInvalidResponseError(embedding_attempt)
    fields = _decode_field_pairs(response[2], embedding_attempt)
    if set(fields) != set(_RETURN_FIELDS):
        raise RedisSemanticCacheInvalidResponseError(embedding_attempt)
    try:
        if _decode_text(fields["schema_version"]) != str(REDIS_CACHE_SCHEMA_VERSION):
            raise ValueError("unsupported schema version")
        if _decode_text(fields["embedding_profile"]) != profile.identity:
            raise ValueError("unsupported embedding profile")
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
        raise RedisSemanticCacheInvalidResponseError(embedding_attempt) from error


def _decode_field_pairs(
    raw_fields: list[object],
    embedding_attempt: EmbeddingAttempt | None,
) -> dict[str, object]:
    """Build unique UTF-8 field pairs from one Redis hash result."""
    if len(raw_fields) % 2 != 0:
        raise RedisSemanticCacheInvalidResponseError(embedding_attempt)
    fields: dict[str, object] = {}
    for index in range(0, len(raw_fields), 2):
        try:
            name = _decode_text(raw_fields[index])
        except (UnicodeDecodeError, TypeError) as error:
            raise RedisSemanticCacheInvalidResponseError(embedding_attempt) from error
        if name in fields:
            raise RedisSemanticCacheInvalidResponseError(embedding_attempt)
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
