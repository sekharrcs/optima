"""Authoritative pricing identity shared across usage and cost contracts."""

from typing import Annotated

from pydantic import Field

from optima.immutable import ImmutableModel

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]


class PricingProvenance(ImmutableModel):
    """Catalog identity governing one authoritative calculated cost."""

    catalog_version: NonEmptyString
    currency: NonEmptyString
