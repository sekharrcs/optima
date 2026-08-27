"""Tests for deterministic Redis semantic-cache index bootstrap."""

import asyncio
from collections.abc import Mapping

import pytest

from optima.cache import (
    RedisIndexCompatibilityError,
    ensure_redis_semantic_cache_index,
    redis_index_bootstrap_lock_key,
    redis_index_contract_key,
)
from optima.cache.redis import REDIS_CACHE_KEY_PREFIX, REDIS_CACHE_SCHEMA_VERSION
from optima.domain.embedding import EmbeddingProfile


def embedding_profile(
    *, dimension: int = 3, model: str = "embed-model"
) -> EmbeddingProfile:
    """Build one stable cache profile."""
    return EmbeddingProfile(
        model=model,
        deployment="embed-deployment",
        dimension=dimension,
    )


def index_info(*, dimension: int = 3, metric: str = "COSINE") -> list[object]:
    """Build the expected RESP2 FT.INFO shape."""
    attributes: list[list[object]] = [
        [b"identifier", field.encode(), b"attribute", field.encode(), b"type", b"TAG"]
        for field in (
            "schema_version",
            "embedding_profile",
            "task_type",
            "complexity",
        )
    ]
    attributes.append(
        [
            b"identifier",
            b"embedding",
            b"attribute",
            b"embedding",
            b"type",
            b"VECTOR",
            b"algorithm",
            b"FLAT",
            b"data_type",
            b"FLOAT32",
            b"dim",
            dimension,
            b"distance_metric",
            metric.encode(),
        ]
    )
    return [
        b"index_name",
        b"optima-cache-v1",
        b"index_definition",
        [b"key_type", b"HASH", b"prefixes", [REDIS_CACHE_KEY_PREFIX.encode()]],
        b"attributes",
        attributes,
    ]


def contract(profile: EmbeddingProfile) -> list[object]:
    """Build the expected immutable companion hash response."""
    return [
        b"index_name",
        b"optima-cache-v1",
        b"cache_schema_version",
        str(REDIS_CACHE_SCHEMA_VERSION).encode(),
        b"embedding_profile_schema_version",
        profile.schema_version.encode(),
        b"semantic_input_policy_version",
        profile.input_policy.encode(),
        b"embedding_profile",
        profile.identity.encode(),
    ]


class ScriptedRedis:
    """Return command-specific responses and record every mutation attempt."""

    def __init__(self, responses: Mapping[str, list[object]]) -> None:
        self.responses = {name: list(values) for name, values in responses.items()}
        self.calls: list[tuple[object, ...]] = []

    async def execute_command(self, *args: object) -> object:
        self.calls.append(args)
        command = str(args[0])
        values = self.responses.get(command)
        if not values:
            raise AssertionError(f"Unexpected Redis command: {args}")
        return values.pop(0)


def test_absent_index_is_created_with_contract() -> None:
    """Create the exact schema and companion contract when both are absent."""
    profile = embedding_profile()
    redis = ScriptedRedis(
        {
            "FT._LIST": [[], []],
            "HGETALL": [[], []],
            "SET": [True],
            "FT.CREATE": [b"OK"],
            "HSET": [5],
            "EVAL": [1],
        }
    )

    asyncio.run(
        ensure_redis_semantic_cache_index(
            redis,
            index_name="optima-cache-v1",
            embedding_profile=profile,
        )
    )

    assert [call[0] for call in redis.calls] == [
        "FT._LIST",
        "HGETALL",
        "SET",
        "FT._LIST",
        "HGETALL",
        "FT.CREATE",
        "HSET",
        "EVAL",
    ]
    create = redis.calls[5]
    assert create[:8] == (
        "FT.CREATE",
        "optima-cache-v1",
        "ON",
        "HASH",
        "PREFIX",
        1,
        REDIS_CACHE_KEY_PREFIX,
        "SCHEMA",
    )
    assert "FT.DROPINDEX" not in {call[0] for call in redis.calls}
    assert redis.calls[2][1] == redis_index_bootstrap_lock_key("optima-cache-v1")
    assert redis.calls[6][1] == redis_index_contract_key("optima-cache-v1")


def test_compatible_index_is_a_read_only_noop() -> None:
    """Inspect an existing compatible index without writing any data."""
    profile = embedding_profile()
    redis = ScriptedRedis(
        {
            "FT._LIST": [[b"optima-cache-v1"]],
            "FT.INFO": [index_info()],
            "HGETALL": [contract(profile)],
        }
    )

    asyncio.run(
        ensure_redis_semantic_cache_index(
            redis,
            index_name="optima-cache-v1",
            embedding_profile=profile,
        )
    )

    assert [call[0] for call in redis.calls] == ["FT._LIST", "FT.INFO", "HGETALL"]


def test_follower_waits_for_creator_contract_and_converges() -> None:
    """Treat an index-visible contract-pending window as bounded transient state."""
    profile = embedding_profile()
    redis = ScriptedRedis(
        {
            "FT._LIST": [[b"optima-cache-v1"], [b"optima-cache-v1"]],
            "FT.INFO": [index_info(), index_info()],
            "HGETALL": [[], contract(profile)],
            "SET": [None],
        }
    )
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    asyncio.run(
        ensure_redis_semantic_cache_index(
            redis,
            index_name="optima-cache-v1",
            embedding_profile=profile,
            sleep=record_sleep,
        )
    )

    assert [call[0] for call in redis.calls] == [
        "FT._LIST",
        "FT.INFO",
        "HGETALL",
        "SET",
        "FT._LIST",
        "FT.INFO",
        "HGETALL",
    ]
    assert sleeps == [0.1]


@pytest.mark.parametrize(
    ("info", "profile", "message"),
    [
        (index_info(dimension=4), embedding_profile(), "vector dim"),
        (index_info(metric="L2"), embedding_profile(), "distance_metric"),
    ],
)
def test_incompatible_vector_contract_fails_without_mutation(
    info: list[object],
    profile: EmbeddingProfile,
    message: str,
) -> None:
    """Reject incompatible immutable vector facts without replacement."""
    redis = ScriptedRedis(
        {
            "FT._LIST": [[b"optima-cache-v1"]],
            "FT.INFO": [info],
        }
    )

    with pytest.raises(RedisIndexCompatibilityError, match=message):
        asyncio.run(
            ensure_redis_semantic_cache_index(
                redis,
                index_name="optima-cache-v1",
                embedding_profile=profile,
            )
        )

    assert all(
        call[0] not in {"FT.CREATE", "FT.DROPINDEX", "HSET"} for call in redis.calls
    )


def test_incompatible_profile_contract_fails_without_mutation() -> None:
    """Reject an index bound to a different embedding profile."""
    configured = embedding_profile()
    existing = embedding_profile(model="other-model")
    redis = ScriptedRedis(
        {
            "FT._LIST": [[b"optima-cache-v1"]],
            "FT.INFO": [index_info()],
            "HGETALL": [contract(existing)],
        }
    )

    with pytest.raises(RedisIndexCompatibilityError, match="bootstrap contract"):
        asyncio.run(
            ensure_redis_semantic_cache_index(
                redis,
                index_name="optima-cache-v1",
                embedding_profile=configured,
            )
        )

    assert all(
        call[0] not in {"FT.CREATE", "FT.DROPINDEX", "HSET"} for call in redis.calls
    )


def test_missing_contract_for_existing_index_fails_closed() -> None:
    """Reject a manually created index whose profile identity is unknown."""
    redis = ScriptedRedis(
        {
            "FT._LIST": [[b"optima-cache-v1"], [b"optima-cache-v1"]],
            "FT.INFO": [index_info(), index_info()],
            "HGETALL": [[], []],
            "SET": [b"OK"],
            "EVAL": [1],
        }
    )

    with pytest.raises(RedisIndexCompatibilityError, match="bootstrap contract"):
        asyncio.run(
            ensure_redis_semantic_cache_index(
                redis,
                index_name="optima-cache-v1",
                embedding_profile=embedding_profile(),
            )
        )

    assert [call[0] for call in redis.calls] == [
        "FT._LIST",
        "FT.INFO",
        "HGETALL",
        "SET",
        "FT._LIST",
        "FT.INFO",
        "HGETALL",
        "EVAL",
    ]


def test_lock_release_failure_does_not_mask_original_bootstrap_error() -> None:
    """Preserve index incompatibility when compare-and-delete also fails."""
    redis = ScriptedRedis(
        {
            "FT._LIST": [[b"optima-cache-v1"], [b"optima-cache-v1"]],
            "FT.INFO": [index_info(), index_info()],
            "HGETALL": [[], []],
            "SET": [True],
            "EVAL": [RuntimeError("release failed")],
        }
    )

    with pytest.raises(
        RedisIndexCompatibilityError,
        match="exists without its bootstrap contract",
    ):
        asyncio.run(
            ensure_redis_semantic_cache_index(
                redis,
                index_name="optima-cache-v1",
                embedding_profile=embedding_profile(),
            )
        )
