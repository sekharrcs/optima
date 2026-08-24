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
SEMANTIC_INPUT_POLICY_VERSION: Literal["semantic-input-v1"] = "semantic-input-v1"

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
StrictBoolean = Annotated[bool, Field(strict=True)]


def build_semantic_input(*, input_text: str, context: str | None) -> str:
    """Return the canonical, versioned semantic-input text for one embedding.

    The policy embeds the generation request (input text plus optional context)
    only. Reference output and criteria are deliberately excluded: they are
    evaluation inputs captured by the authoritative RequestBinding, not part of
    the request's semantic similarity. Canonical JSON avoids delimiter-injection
    ambiguity, and external cache-population tooling must use this same builder.
    """
    return json.dumps(
        {
            "policy": SEMANTIC_INPUT_POLICY_VERSION,
            "input_text": input_text,
            "context": context,
        },
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


class EmbeddingProfile(ImmutableModel):
    """Versioned identity binding an embedding model, deployment, and dimension."""

    schema_version: Literal["embedding-profile-v1"] = EMBEDDING_PROFILE_SCHEMA_VERSION
    input_policy: Literal["semantic-input-v1"] = SEMANTIC_INPUT_POLICY_VERSION
    model: EmbeddingProfileToken
    deployment: EmbeddingProfileToken
    dimension: BoundedEmbeddingDimension

    @property
    def identity(self) -> str:
        """Return a canonical SHA-256 identity safe as a RediSearch tag value."""
        payload = json.dumps(
            {
                "schema_version": self.schema_version,
                "input_policy": self.input_policy,
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


class EmbeddingAttempt(ImmutableModel):
    """Typed evidence of one embedding attempt, distinct from measured usage.

    A failed or timed-out embedding request may already have reached the paid
    provider, so the absence of measured usage is not evidence of zero
    consumption. This evidence lets run totals distinguish an indeterminate
    (possibly paid) attempt from a proven no-consumption outcome.
    """

    invoked: StrictBoolean
    outbound_attempted: StrictBoolean
    usage: EmbeddingUsage | None = None

    @model_validator(mode="after")
    def validate_attempt(self) -> "EmbeddingAttempt":
        """Require measured usage to imply an invoked, outbound request."""
        if self.usage is not None and not (self.invoked and self.outbound_attempted):
            raise ValueError(
                "measured embedding usage requires an invoked outbound request"
            )
        if self.outbound_attempted and not self.invoked:
            raise ValueError("an outbound attempt requires an invocation")
        return self

    @property
    def consumption_indeterminate(self) -> bool:
        """Return whether a possibly paid request left consumption unknown."""
        return self.outbound_attempted and self.usage is None
