"""Canonical request identity for safe semantic-cache reuse."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from optima.domain.request_profile import Complexity, TaskType
from optima.immutable import ImmutableJsonObject, ImmutableModel

REQUEST_BINDING_SCHEMA_VERSION: Literal["request-evaluation-v1"] = (
    "request-evaluation-v1"
)

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
Sha256Digest = Annotated[
    str,
    Field(strict=True, pattern=r"^[0-9a-f]{64}$"),
]


class RequestBinding(ImmutableModel):
    """Non-sensitive versioned fingerprint of generation and evaluation inputs."""

    schema_version: Literal["request-evaluation-v1"] = REQUEST_BINDING_SCHEMA_VERSION
    algorithm: Literal["sha256"] = "sha256"
    task_type: TaskType
    complexity: Complexity
    digest: Sha256Digest


class _CanonicalRequestInputs(ImmutableModel):
    """Validated ephemeral payload used only to derive a request binding."""

    schema_version: Literal["request-evaluation-v1"] = REQUEST_BINDING_SCHEMA_VERSION
    input_text: NonEmptyString
    context: NonEmptyString | None = None
    reference_output: NonEmptyString | None = None
    criteria: tuple[NonEmptyString, ...] = ()
    metadata: ImmutableJsonObject = Field(default_factory=dict)
    task_type: TaskType
    complexity: Complexity


def build_request_binding(
    *,
    input_text: str,
    context: str | None,
    reference_output: str | None,
    criteria: tuple[str, ...],
    metadata: Mapping[str, JsonValue],
    task_type: TaskType,
    complexity: Complexity,
) -> RequestBinding:
    """Hash the exact V1 request/evaluation facts using canonical JSON."""
    payload = _CanonicalRequestInputs(
        input_text=input_text,
        context=context,
        reference_output=reference_output,
        criteria=criteria,
        metadata=dict(metadata),
        task_type=task_type,
        complexity=complexity,
    )
    canonical_json = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return RequestBinding(
        task_type=task_type,
        complexity=complexity,
        digest=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    )
