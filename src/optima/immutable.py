"""Recursively immutable JSON values with ordinary Pydantic serialization."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Annotated, Any, Self, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    JsonValue,
    PlainSerializer,
)

FrozenJsonObject = MappingProxyType


class FrozenJsonArray(tuple[JsonValue, ...]):
    """An immutable JSON array with JSON-compatible equality semantics."""

    __slots__ = ()

    def __new__(cls, values: Sequence[JsonValue]) -> FrozenJsonArray:
        return super().__new__(cls, values)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence) and not isinstance(other, str | bytes):
            return tuple(self) == tuple(other)
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return repr(list(self))


def _freeze_json_value(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return cast(
            JsonValue,
            MappingProxyType(
                {key: _freeze_json_value(item) for key, item in value.items()}
            ),
        )
    if isinstance(value, (list, tuple, FrozenJsonArray)):
        return cast(
            JsonValue,
            FrozenJsonArray(tuple(_freeze_json_value(item) for item in value)),
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    return value


def _freeze_json_object(value: dict[str, JsonValue]) -> Mapping[str, JsonValue]:
    frozen = _freeze_json_value(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("expected a JSON object")
    return frozen


def _thaw_json_value(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, FrozenJsonArray):
        return [_thaw_json_value(item) for item in value]
    return value  # type: ignore[return-value]


def _serialize_json_object(value: object) -> dict[str, JsonValue]:
    thawed = _thaw_json_value(value)
    if not isinstance(thawed, dict):
        raise TypeError("expected a JSON object")
    return thawed


ImmutableJsonObject = Annotated[
    dict[str, JsonValue],
    BeforeValidator(_thaw_json_value),
    AfterValidator(_freeze_json_object),
    PlainSerializer(
        _serialize_json_object,
        return_type=dict[str, JsonValue],
        when_used="always",
    ),
]


class ImmutableModel(BaseModel):
    """Frozen Pydantic model whose copies validate and detach update values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Rebuild the model so updates cannot bypass validation or detachment."""
        values = self.model_dump(
            mode="python",
            round_trip=True,
            exclude_computed_fields=True,
        )
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)
