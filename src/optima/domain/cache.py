"""Provider-independent semantic-cache values shared across runtime layers."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from optima.domain.evaluation import EvaluationResult

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
Rate = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
StrictBoolean = Annotated[bool, Field(strict=True)]


class CacheCandidate(BaseModel):
    """One resolved cached output and the evidence Planner V1 assesses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_run_id: NonEmptyString
    output_text: NonEmptyString
    similarity: Rate
    prior_evaluation: EvaluationResult
    contract_compatible: StrictBoolean
    safe_to_reuse: StrictBoolean

    def detached_copy(self) -> "CacheCandidate":
        """Return a validated value snapshot detached from mutable caller data."""
        return CacheCandidate.model_validate(self.model_dump(mode="python"))
