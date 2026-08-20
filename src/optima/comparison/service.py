"""Pure construction of measured baseline-versus-OPTIMA comparisons."""

from decimal import Decimal

from optima.comparison.models import (
    BaselineComparison,
    BaselineComparisonRequest,
    ComparisonArm,
    ExecutionMetrics,
)
from optima.domain.execution import ExecutionStatus, ExecutionStepType
from optima.domain.run import RunResult

ONE_HUNDRED = Decimal("100")


class BaselineComparisonService:
    """Compare two compatible measured runs without external state."""

    def compare(self, request: BaselineComparisonRequest) -> BaselineComparison:
        """Preserve arm metrics and calculate OPTIMA-relative differences."""
        baseline = self._metrics(
            ComparisonArm.BASELINE,
            request.baseline.run_result,
        )
        optima = self._metrics(ComparisonArm.OPTIMA, request.optima.run_result)

        return BaselineComparison(
            identity=request.baseline.identity,
            baseline=baseline,
            optima=optima,
            model_calls_delta=optima.model_calls - baseline.model_calls,
            input_tokens_delta=self._integer_delta(
                baseline.input_tokens,
                optima.input_tokens,
            ),
            output_tokens_delta=self._integer_delta(
                baseline.output_tokens,
                optima.output_tokens,
            ),
            total_tokens_delta=self._integer_delta(
                baseline.total_tokens,
                optima.total_tokens,
            ),
            cost_delta=self._decimal_delta(baseline.cost, optima.cost),
            latency_ms_delta=optima.latency_ms - baseline.latency_ms,
            quality_score_delta=self._quality_delta(
                baseline.quality_score,
                optima.quality_score,
            ),
            token_reduction_percentage=self._reduction_percentage(
                baseline.total_tokens,
                optima.total_tokens,
            ),
            cost_reduction_percentage=self._reduction_percentage(
                baseline.cost,
                optima.cost,
            ),
            latency_percentage_change=self._change_percentage(
                baseline.latency_ms,
                optima.latency_ms,
            ),
        )

    @staticmethod
    def _metrics(arm: ComparisonArm, run_result: RunResult) -> ExecutionMetrics:
        final_evaluation = run_result.final_evaluation
        valid_evaluation = (
            final_evaluation
            if final_evaluation is not None and final_evaluation.evaluator_valid
            else None
        )
        model_calls = sum(
            step.step_type is ExecutionStepType.MODEL_CALL
            and step.status is not ExecutionStatus.SKIPPED
            for step in run_result.steps
        )
        return ExecutionMetrics(
            arm=arm,
            run_id=run_result.run_id,
            model_calls=model_calls,
            input_tokens=run_result.total_input_tokens,
            output_tokens=run_result.total_output_tokens,
            total_tokens=run_result.total_tokens,
            cost=run_result.total_calculated_cost,
            cost_provenance=run_result.total_cost_provenance,
            latency_ms=run_result.latency_ms,
            evaluator_type=(
                valid_evaluation.evaluator_type
                if valid_evaluation is not None
                else None
            ),
            quality_score=(
                valid_evaluation.score if valid_evaluation is not None else None
            ),
            contract_met=(
                run_result.contract_met if valid_evaluation is not None else None
            ),
        )

    @staticmethod
    def _integer_delta(baseline: int | None, optima: int | None) -> int | None:
        if baseline is None or optima is None:
            return None
        return optima - baseline

    @staticmethod
    def _decimal_delta(
        baseline: Decimal | None,
        optima: Decimal | None,
    ) -> Decimal | None:
        if baseline is None or optima is None:
            return None
        return optima - baseline

    @staticmethod
    def _quality_delta(
        baseline: float | None,
        optima: float | None,
    ) -> float | None:
        if baseline is None or optima is None:
            return None
        return optima - baseline

    @staticmethod
    def _reduction_percentage(
        baseline: int | Decimal | None,
        optima: int | Decimal | None,
    ) -> Decimal | None:
        if baseline is None or optima is None or baseline == 0:
            return None
        baseline_decimal = Decimal(baseline)
        return (baseline_decimal - Decimal(optima)) / baseline_decimal * ONE_HUNDRED

    @staticmethod
    def _change_percentage(
        baseline: int | Decimal | None,
        optima: int | Decimal | None,
    ) -> Decimal | None:
        if baseline is None or optima is None or baseline == 0:
            return None
        baseline_decimal = Decimal(baseline)
        return (Decimal(optima) - baseline_decimal) / baseline_decimal * ONE_HUNDRED
