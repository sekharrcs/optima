"""Idempotent RediSearch index bootstrap for the semantic cache."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from uuid import uuid4

from optima.cache.redis import (
    REDIS_CACHE_DISTANCE_METRIC,
    REDIS_CACHE_KEY_PREFIX,
    REDIS_CACHE_SCHEMA_VERSION,
    REDIS_CACHE_TAG_FIELDS,
    REDIS_CACHE_VECTOR_ALGORITHM,
    REDIS_CACHE_VECTOR_FIELD,
    REDIS_CACHE_VECTOR_TYPE,
    RedisSearchClient,
)
from optima.domain.embedding import (
    EMBEDDING_PROFILE_SCHEMA_VERSION,
    SEMANTIC_INPUT_POLICY_VERSION,
    EmbeddingProfile,
)


class RedisIndexCompatibilityError(RuntimeError):
    """Report an existing index that is unsafe for the configured cache."""


REDIS_INDEX_BOOTSTRAP_LOCK_SECONDS = 30
REDIS_INDEX_BOOTSTRAP_ATTEMPTS = 20
REDIS_INDEX_BOOTSTRAP_RETRY_SECONDS = 0.1
_RELEASE_LOCK_SCRIPT = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
    "return redis.call('DEL', KEYS[1]) else return 0 end"
)


def redis_index_contract_key(index_name: str) -> str:
    """Return the application-owned metadata key for one search index."""
    return f"optima:semantic-cache-index-contract:{index_name}"


def redis_index_bootstrap_lock_key(index_name: str) -> str:
    """Return the coordination lock key for one search index bootstrap."""
    return f"optima:semantic-cache-index-bootstrap-lock:{index_name}"


async def ensure_redis_semantic_cache_index(
    client: RedisSearchClient,
    *,
    index_name: str,
    embedding_profile: EmbeddingProfile,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Create the expected index when absent or validate it without mutation."""
    contract_key = redis_index_contract_key(index_name)
    lock_key = redis_index_bootstrap_lock_key(index_name)
    lock_value = str(uuid4())
    for _attempt in range(REDIS_INDEX_BOOTSTRAP_ATTEMPTS):
        state = await _inspect_index(
            client,
            index_name=index_name,
            embedding_profile=embedding_profile,
            contract_key=contract_key,
        )
        if state == "compatible":
            return
        acquired = await client.execute_command(
            "SET",
            lock_key,
            lock_value,
            "NX",
            "EX",
            REDIS_INDEX_BOOTSTRAP_LOCK_SECONDS,
        )
        if acquired is None:
            await sleep(REDIS_INDEX_BOOTSTRAP_RETRY_SECONDS)
            continue
        try:
            if acquired is not True and (
                _decode_text(acquired, context="Redis bootstrap lock") != "OK"
            ):
                raise RedisIndexCompatibilityError(
                    "Redis index bootstrap lock response is invalid"
                )
            state = await _inspect_index(
                client,
                index_name=index_name,
                embedding_profile=embedding_profile,
                contract_key=contract_key,
            )
            if state == "incomplete":
                raise RedisIndexCompatibilityError(
                    "Redis index exists without its bootstrap contract"
                )
            if state == "absent":
                await _create_index(
                    client,
                    index_name=index_name,
                    embedding_profile=embedding_profile,
                    contract_key=contract_key,
                )
        except BaseException:
            try:
                await _release_bootstrap_lock(client, lock_key, lock_value)
            except BaseException:
                pass
            raise
        else:
            await _release_bootstrap_lock(client, lock_key, lock_value)
            return
    raise RedisIndexCompatibilityError("Redis index bootstrap did not converge")


async def _release_bootstrap_lock(
    client: RedisSearchClient,
    lock_key: str,
    lock_value: str,
) -> None:
    await client.execute_command(
        "EVAL",
        _RELEASE_LOCK_SCRIPT,
        1,
        lock_key,
        lock_value,
    )


async def _inspect_index(
    client: RedisSearchClient,
    *,
    index_name: str,
    embedding_profile: EmbeddingProfile,
    contract_key: str,
) -> str:
    raw_indexes = await client.execute_command("FT._LIST")
    indexes = _decode_text_sequence(raw_indexes, context="FT._LIST")
    if index_name not in indexes:
        contract = _decode_contract(
            await client.execute_command("HGETALL", contract_key)
        )
        if contract:
            raise RedisIndexCompatibilityError(
                "Redis index is absent but an incompatible bootstrap contract exists"
            )
        return "absent"
    info = _decode_mapping(
        await client.execute_command("FT.INFO", index_name),
        context="FT.INFO",
    )
    _validate_index_info(
        info,
        index_name=index_name,
        embedding_dimension=embedding_profile.dimension,
    )
    contract = _decode_contract(await client.execute_command("HGETALL", contract_key))
    if not contract:
        return "incomplete"
    if contract != _expected_contract(index_name, embedding_profile):
        raise RedisIndexCompatibilityError(
            "Redis index bootstrap contract is incompatible with configuration"
        )
    return "compatible"


async def _create_index(
    client: RedisSearchClient,
    *,
    index_name: str,
    embedding_profile: EmbeddingProfile,
    contract_key: str,
) -> None:
    response = await client.execute_command(
        "FT.CREATE",
        index_name,
        "ON",
        "HASH",
        "PREFIX",
        1,
        REDIS_CACHE_KEY_PREFIX,
        "SCHEMA",
        REDIS_CACHE_TAG_FIELDS[0],
        "TAG",
        REDIS_CACHE_TAG_FIELDS[1],
        "TAG",
        REDIS_CACHE_TAG_FIELDS[2],
        "TAG",
        REDIS_CACHE_TAG_FIELDS[3],
        "TAG",
        REDIS_CACHE_VECTOR_FIELD,
        "VECTOR",
        REDIS_CACHE_VECTOR_ALGORITHM,
        6,
        "TYPE",
        REDIS_CACHE_VECTOR_TYPE,
        "DIM",
        embedding_profile.dimension,
        "DISTANCE_METRIC",
        REDIS_CACHE_DISTANCE_METRIC,
    )
    if _decode_text(response, context="FT.CREATE") != "OK":
        raise RedisIndexCompatibilityError("Redis index creation was not acknowledged")
    contract = _expected_contract(index_name, embedding_profile)
    written = await client.execute_command(
        "HSET",
        contract_key,
        *(item for pair in contract.items() for item in pair),
    )
    if type(written) is not int or written != len(contract):
        raise RedisIndexCompatibilityError(
            "Redis index contract creation was not acknowledged"
        )


def _validate_index_info(
    info: Mapping[str, object],
    *,
    index_name: str,
    embedding_dimension: int,
) -> None:
    if _decode_text(info.get("index_name"), context="index name") != index_name:
        raise RedisIndexCompatibilityError("Redis index name is incompatible")
    definition = _decode_mapping(
        info.get("index_definition"),
        context="Redis index definition",
    )
    if _decode_text(definition.get("key_type"), context="index key type") != "HASH":
        raise RedisIndexCompatibilityError("Redis index key type is incompatible")
    prefixes = _decode_text_sequence(
        definition.get("prefixes"),
        context="Redis index prefixes",
    )
    if prefixes != (REDIS_CACHE_KEY_PREFIX,):
        raise RedisIndexCompatibilityError("Redis index prefix is incompatible")

    raw_attributes = info.get("attributes")
    if not isinstance(raw_attributes, Sequence) or isinstance(
        raw_attributes, str | bytes | bytearray
    ):
        raise RedisIndexCompatibilityError("Redis index attributes are invalid")
    attributes: dict[str, Mapping[str, object]] = {}
    for raw_attribute in raw_attributes:
        attribute = _decode_mapping(raw_attribute, context="Redis index attribute")
        identifier = _decode_text(
            attribute.get("identifier"),
            context="Redis index attribute identifier",
        )
        if identifier in attributes:
            raise RedisIndexCompatibilityError("Redis index attributes are ambiguous")
        attributes[identifier] = attribute
    if set(attributes) != {*REDIS_CACHE_TAG_FIELDS, REDIS_CACHE_VECTOR_FIELD}:
        raise RedisIndexCompatibilityError("Redis index fields are incompatible")
    for field_name in REDIS_CACHE_TAG_FIELDS:
        if (
            _decode_text(
                attributes[field_name].get("type"),
                context=f"Redis {field_name} field type",
            )
            != "TAG"
        ):
            raise RedisIndexCompatibilityError(
                f"Redis {field_name} field type is incompatible"
            )
    vector = attributes[REDIS_CACHE_VECTOR_FIELD]
    expected_vector = {
        "type": "VECTOR",
        "algorithm": REDIS_CACHE_VECTOR_ALGORITHM,
        "data_type": REDIS_CACHE_VECTOR_TYPE,
        "dim": str(embedding_dimension),
        "distance_metric": REDIS_CACHE_DISTANCE_METRIC,
    }
    for key, expected in expected_vector.items():
        if _decode_text(vector.get(key), context=f"Redis vector {key}") != expected:
            raise RedisIndexCompatibilityError(f"Redis vector {key} is incompatible")


def _expected_contract(
    index_name: str,
    embedding_profile: EmbeddingProfile,
) -> dict[str, str]:
    return {
        "index_name": index_name,
        "cache_schema_version": str(REDIS_CACHE_SCHEMA_VERSION),
        "embedding_profile_schema_version": EMBEDDING_PROFILE_SCHEMA_VERSION,
        "semantic_input_policy_version": SEMANTIC_INPUT_POLICY_VERSION,
        "embedding_profile": embedding_profile.identity,
    }


def _decode_contract(value: object) -> dict[str, str]:
    contract = _decode_mapping(value, context="Redis index contract")
    return {
        key: _decode_text(item, context="Redis index contract")
        for key, item in contract.items()
    }


def _decode_mapping(value: object, *, context: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {_decode_text(key, context=context): item for key, item in value.items()}
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise RedisIndexCompatibilityError(f"{context} response is invalid")
    if len(value) % 2 != 0:
        raise RedisIndexCompatibilityError(f"{context} response is invalid")
    return {
        _decode_text(value[index], context=context): value[index + 1]
        for index in range(0, len(value), 2)
    }


def _decode_text_sequence(value: object, *, context: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise RedisIndexCompatibilityError(f"{context} response is invalid")
    return tuple(_decode_text(item, context=context) for item in value)


def _decode_text(value: object, *, context: str) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RedisIndexCompatibilityError(
                f"{context} response is invalid"
            ) from error
    if isinstance(value, str):
        return value
    if type(value) is int:
        return str(value)
    raise RedisIndexCompatibilityError(f"{context} response is invalid")
