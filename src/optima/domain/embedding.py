"""Embedding-profile identity and dedicated embedding usage evidence."""

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from optima.domain.pricing import PricingProvenance
from optima.immutable import ImmutableModel

EMBEDDING_PROFILE_SCHEMA_VERSION: Literal["embedding-profile-v1"] = (
    "embedding-profile-v1"
)

# Injection-safe readable token: alphanumeric start, then a restricted charset
# that excludes every RediSearch tag/query operator.
EmbeddingProfileToken = Annotated[
    str,
    Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
]
BoundedEmbeddingDimension = Annotated[int, Field(strict=True, gt=0, le=32_768)]
NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
NonNegativeMilliseconds = Annotated[int, Field(strict=True, ge=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=Decimal("0"), allow_inf_nan=False)]


class EmbeddingProfile(ImmutableModel):
    """Versioned identity binding an embedding model, deployment, and dimension."""

    schema_version: Literal["embedding-profile-v1"] = EMBEDDING_PROFILE_SCHEMA_VERSION
    model: EmbeddingProfileToken
    deployment: EmbeddingProfileToken
    dimension: BoundedEmbeddingDimension

    @property
    def identity(self) -> str:
        """Return a canonical SHA-256 identity safe as a RediSearch tag value."""
        payload = json.dumps(
            {
                "schema_version": self.schema_version,
                "model": self.model,
                "deployment": self.deployment,
                "dimension": self.dimension,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EmbeddingUsage(ImmutableModel):
    """Measured facts for one embedding request made during a cache lookup."""

    run_id: NonEmptyString
    provider: NonEmptyString
    deployment: NonEmptyString
    embedding_profile: NonEmptyString
    request_id: NonEmptyString | None = None
    input_tokens: NonNegativeCount | None = None
    latency_ms: NonNegativeMilliseconds
    calculated_cost: NonNegativeDecimal | None = None
    pricing_provenance: PricingProvenance | None = None

    @model_validator(mode="after")
    def validate_usage(self) -> "EmbeddingUsage":
        """Require cost and provenance to appear together."""
        if (self.calculated_cost is None) is not (self.pricing_provenance is None):
            raise ValueError(
                "calculated_cost and pricing_provenance must be provided together"
            )
        return self
