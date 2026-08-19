"""Deterministic Quality Contract threshold evaluation."""

from enum import StrEnum

from optima.domain.evaluation import EvaluationResult
from optima.domain.quality_contract import QualityContract
from optima.evaluation.contracts import EvaluationEvidence


class EvaluationReasonCode(StrEnum):
    """Stable reason identifiers emitted by the threshold engine."""

    EVALUATOR_VALID = "EVALUATOR_VALID"
    EVALUATOR_INVALID = "EVALUATOR_INVALID"
    QUALITY_THRESHOLD_MET = "QUALITY_THRESHOLD_MET"
    QUALITY_THRESHOLD_NOT_MET = "QUALITY_THRESHOLD_NOT_MET"
    MANDATORY_CHECKS_PASSED = "MANDATORY_CHECKS_PASSED"
    QUALITY_CONTRACT_MET = "QUALITY_CONTRACT_MET"
    QUALITY_CONTRACT_NOT_MET = "QUALITY_CONTRACT_NOT_MET"


MANDATORY_CHECK_FAILED_PREFIX = "MANDATORY_CHECK_FAILED"


class ThresholdEngine:
    """Apply explicit Quality Contract pass conditions to measured evidence."""

    def evaluate(
        self,
        *,
        evidence: EvaluationEvidence,
        quality_contract: QualityContract,
    ) -> EvaluationResult:
        """Build an EvaluationResult from evidence and the contract threshold."""
        reasons: list[str] = [
            (
                EvaluationReasonCode.EVALUATOR_VALID
                if evidence.evaluator_valid
                else EvaluationReasonCode.EVALUATOR_INVALID
            )
        ]

        score_passed = evidence.score >= quality_contract.minimum_quality_score
        reasons.append(
            EvaluationReasonCode.QUALITY_THRESHOLD_MET
            if score_passed
            else EvaluationReasonCode.QUALITY_THRESHOLD_NOT_MET
        )

        failed_checks = tuple(
            check for check in evidence.mandatory_checks if not check.passed
        )
        mandatory_checks_passed = not failed_checks
        if mandatory_checks_passed:
            reasons.append(EvaluationReasonCode.MANDATORY_CHECKS_PASSED)
        else:
            reasons.extend(
                f"{MANDATORY_CHECK_FAILED_PREFIX}:{check.check_id}"
                for check in failed_checks
            )

        passed = evidence.evaluator_valid and score_passed and mandatory_checks_passed
        reasons.append(
            EvaluationReasonCode.QUALITY_CONTRACT_MET
            if passed
            else EvaluationReasonCode.QUALITY_CONTRACT_NOT_MET
        )

        return EvaluationResult(
            evaluator_type=evidence.evaluator_type,
            evaluator_valid=evidence.evaluator_valid,
            score=evidence.score,
            threshold=quality_contract.minimum_quality_score,
            mandatory_checks_passed=mandatory_checks_passed,
            passed=passed,
            reasons=tuple(reasons),
            metadata=dict(evidence.metadata),
        )
