"""Immutable model-price catalog contracts."""

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeRate = Annotated[
    Decimal,
    Field(ge=Decimal("0"), allow_inf_nan=False),
]


class PriceCatalogEntry(BaseModel):
    """Per-million-token rates for one provider deployment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: NonEmptyString
    deployment: NonEmptyString
    input_rate_per_million_tokens: NonNegativeRate
    output_rate_per_million_tokens: NonNegativeRate
    cached_input_rate_per_million_tokens: NonNegativeRate | None = None


class PriceCatalog(BaseModel):
    """Versioned single-currency catalog of exact deployment rates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: NonEmptyString
    currency: NonEmptyString
    entries: tuple[PriceCatalogEntry, ...] = ()

    @model_validator(mode="after")
    def validate_unique_deployments(self) -> "PriceCatalog":
        """Reject ambiguous provider and deployment price keys."""
        keys = [(entry.provider, entry.deployment) for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("provider and deployment price keys must be unique")
        return self

    def find_entry(
        self,
        *,
        provider: str,
        deployment: str,
    ) -> PriceCatalogEntry | None:
        """Return the exact provider/deployment entry when configured."""
        return next(
            (
                entry
                for entry in self.entries
                if entry.provider == provider and entry.deployment == deployment
            ),
            None,
        )
