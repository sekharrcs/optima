"""Typed user inputs and exact API request construction for the demo UI."""

from pydantic import BaseModel, ConfigDict, Field

from optima.api.models import RunRequest
from optima.domain.quality_contract import OptimizationMode, QualityProfile, RiskTier
from optima.domain.request_profile import Complexity, RequestProfile, TaskType


class ExecuteInputs(BaseModel):
    """User controls, including explicitly supplied advanced profile facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_text: str = Field(min_length=1)
    context: str | None = None
    quality_profile: QualityProfile = QualityProfile.HIGH
    optimization_mode: OptimizationMode = OptimizationMode.COST
    task_type: TaskType = TaskType.SUMMARIZATION
    complexity: Complexity = Complexity.LOW
    input_tokens: int = Field(default=500, ge=0)
    profile_risk_tier: RiskTier = RiskTier.LOW
    contract_risk_tier: RiskTier = RiskTier.LOW
    cache_eligible: bool = False
    has_large_context: bool = False
    max_latency_ms: int | None = Field(default=None, gt=0)

    def to_run_request(self) -> RunRequest:
        """Build the strict backend request without inferring profile facts."""
        return RunRequest(
            input_text=self.input_text,
            context=self.context,
            request_profile=RequestProfile(
                task_type=self.task_type,
                complexity=self.complexity,
                input_tokens=self.input_tokens,
                risk_tier=self.profile_risk_tier,
                cache_eligible=self.cache_eligible,
                has_large_context=self.has_large_context,
            ),
            quality_profile=self.quality_profile,
            optimization_mode=self.optimization_mode,
            risk_tier=self.contract_risk_tier,
            max_latency_ms=self.max_latency_ms,
            metadata={"request_profile_source": "user_supplied_demo_input"},
        )
