"""Strict HTTP request and structured error contracts."""

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from optima.domain.quality_contract import OptimizationMode, QualityProfile, RiskTier
from optima.domain.request_profile import RequestProfile

MAX_INPUT_TEXT_CHARACTERS = 32_000
MAX_CONTEXT_CHARACTERS = 128_000
MAX_REFERENCE_OUTPUT_CHARACTERS = 32_000
MAX_CRITERIA_ENTRIES = 20
MAX_CRITERION_CHARACTERS = 2_000
MAX_METADATA_BYTES = 32 * 1024
MAX_METADATA_DEPTH = 16
MAX_LATENCY_MILLISECONDS = 300_000

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
InputText = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=MAX_INPUT_TEXT_CHARACTERS),
]
ContextText = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=MAX_CONTEXT_CHARACTERS),
]
ReferenceOutput = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=MAX_REFERENCE_OUTPUT_CHARACTERS),
]
Criterion = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=MAX_CRITERION_CHARACTERS),
]
BoundedLatencyMilliseconds = Annotated[
    int,
    Field(strict=True, gt=0, le=MAX_LATENCY_MILLISECONDS),
]


class RunRequest(BaseModel):
    """Public input for one planned and measured OPTIMA execution."""

    model_config = ConfigDict(extra="forbid")

    input_text: InputText
    context: ContextText | None = None
    request_profile: RequestProfile
    quality_profile: QualityProfile
    optimization_mode: OptimizationMode
    risk_tier: RiskTier
    grounding_required: Annotated[bool, Field(strict=True)] = False
    max_latency_ms: BoundedLatencyMilliseconds | None = None
    reference_output: ReferenceOutput | None = None
    criteria: Annotated[
        tuple[Criterion, ...], Field(max_length=MAX_CRITERIA_ENTRIES)
    ] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_metadata_size_and_depth(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Bound canonical UTF-8 metadata size and nested container depth."""
        pending: list[tuple[JsonValue, int]] = [(value, 0)]
        while pending:
            current, depth = pending.pop()
            if isinstance(current, (dict, list)):
                if depth > MAX_METADATA_DEPTH:
                    raise ValueError("metadata exceeds the maximum nesting depth")
                children = current.values() if isinstance(current, dict) else current
                pending.extend((child, depth + 1) for child in children)
        serialized = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(serialized) > MAX_METADATA_BYTES:
            raise ValueError("metadata exceeds the maximum serialized size")
        return value


class ApiError(BaseModel):
    """Stable machine-readable API error detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: NonEmptyString
    message: NonEmptyString
    facts: dict[str, JsonValue] = Field(default_factory=dict)
