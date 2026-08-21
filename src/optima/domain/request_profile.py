"""Request Profile domain values consumed by Planner V1."""

from enum import StrEnum
from typing import Annotated

from pydantic import Field

from optima.domain.quality_contract import RiskTier
from optima.immutable import ImmutableModel

TokenCount = Annotated[int, Field(strict=True, ge=0)]


class TaskType(StrEnum):
    """Task types supported by Planner V1."""

    SUMMARIZATION = "SUMMARIZATION"
    EXTRACTION = "EXTRACTION"
    CLASSIFICATION = "CLASSIFICATION"
    Q_AND_A = "Q_AND_A"
    CODE_GENERATION = "CODE_GENERATION"
    LOG_ANALYSIS = "LOG_ANALYSIS"
    GENERAL_REASONING = "GENERAL_REASONING"
    UNKNOWN = "UNKNOWN"


class Complexity(StrEnum):
    """Request complexity values supported by Planner V1."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RequestProfile(ImmutableModel):
    """Descriptive request facts provided to the planner."""

    task_type: TaskType
    complexity: Complexity
    input_tokens: TokenCount
    risk_tier: RiskTier
    cache_eligible: Annotated[bool, Field(strict=True)]
    has_large_context: Annotated[bool, Field(strict=True)]
