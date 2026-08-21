"""Provider-independent semantic-cache values shared across runtime layers."""

from typing import Annotated

from pydantic import Field

from optima.domain.evaluation import EvaluationResult
from optima.domain.request_binding import RequestBinding
from optima.immutable import ImmutableModel

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
Rate = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
StrictBoolean = Annotated[bool, Field(strict=True)]


class CacheCandidate(ImmutableModel):
    """One resolved cached output and the evidence Planner V1 assesses."""

    source_run_id: NonEmptyString
    output_text: NonEmptyString
    request_binding: RequestBinding
    similarity: Rate
    prior_evaluation: EvaluationResult
    contract_compatible: StrictBoolean
    safe_to_reuse: StrictBoolean

    def detached_copy(self) -> "CacheCandidate":
        """Return a validated value snapshot detached from mutable caller data."""
        return self.model_copy()


class CacheCandidateAssessment(ImmutableModel):
    """Planner-owned candidate facts safe to expose without the cached output."""

    source_run_id: NonEmptyString
    request_binding: RequestBinding
    similarity: Rate
    prior_evaluation: EvaluationResult
    contract_compatible: StrictBoolean
    safe_to_reuse: StrictBoolean

    @classmethod
    def from_candidate(cls, candidate: CacheCandidate) -> "CacheCandidateAssessment":
        """Detach the exact candidate facts used by Planner V1."""
        return cls(
            source_run_id=candidate.source_run_id,
            request_binding=candidate.request_binding,
            similarity=candidate.similarity,
            prior_evaluation=candidate.prior_evaluation,
            contract_compatible=candidate.contract_compatible,
            safe_to_reuse=candidate.safe_to_reuse,
        )
