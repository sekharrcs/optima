"""Tests for the pre-exposure production smoke command."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import Mock

import httpx
import pytest

from optima.domain.embedding import EmbeddingAttempt, EmbeddingUsage
from optima.domain.execution import (
    ExecutionPlan,
    ExecutionStatus,
    ExecutionStep,
    ExecutionStepType,
    ModelPolicy,
    ModelRole,
    PlannerReasonCode,
    SemanticCacheEvidence,
    SemanticCacheOutcome,
)
from optima.domain.run import ModelUsage, RunResult
from ui import deployment_smoke

TRACEPARENT = "00-11111111111111111111111111111111-2222222222222222-01"


def _result(
    *,
    strong: bool,
    embedding: bool = True,
    semantic_cache_enabled: bool = True,
) -> RunResult:
    roles = (
        (ModelRole.STRONG, ModelRole.JUDGE)
        if strong
        else (ModelRole.SMALL, ModelRole.JUDGE)
    )
    semantic_cache = None
    if strong:
        usage = EmbeddingUsage.model_construct(calculated_cost=Decimal("0.01"))
        attempt = EmbeddingAttempt.model_construct(
            invoked=True,
            outbound_attempted=True,
            usage=usage if embedding else None,
        )
        semantic_cache = SemanticCacheEvidence.model_construct(
            outcome=(
                SemanticCacheOutcome.MISS
                if semantic_cache_enabled
                else SemanticCacheOutcome.DISABLED_BYPASSED
            ),
            lookup_latency_ms=1 if semantic_cache_enabled else 0,
            planner_reason_code=(
                PlannerReasonCode.CACHE_CANDIDATE_NOT_SUPPLIED
                if semantic_cache_enabled
                else PlannerReasonCode.SEMANTIC_CACHE_DISABLED
            ),
            embedding_attempt=attempt if semantic_cache_enabled else None,
        )
    return RunResult.model_construct(
        escalated=False,
        final_output="DEPLOYMENT_SMOKE_OK",
        model_usages=tuple(
            ModelUsage.model_construct(
                model_role=role,
                calculated_cost=Decimal("0.01"),
            )
            for role in roles
        ),
        execution_plan=ExecutionPlan.model_construct(
            model_policy=(
                ModelPolicy.STRONG_DIRECT
                if strong
                else ModelPolicy.SMALL_FIRST_WITH_FALLBACK
            )
        ),
        semantic_cache=semantic_cache,
        steps=(
            ExecutionStep.model_construct(
                step_type=ExecutionStepType.MODEL_CALL,
                status=ExecutionStatus.SUCCEEDED,
            ),
        ),
    )


def test_smoke_exercises_both_generator_roles_judge_and_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require two bounded calls that cover every live provider role."""
    calls: list[tuple[str, bool]] = []

    def execute(
        client: httpx.Client,
        *,
        traceparent: str,
        complexity: str,
        cache_eligible: bool,
        marker: str,
    ) -> RunResult:
        del client, marker
        assert traceparent == TRACEPARENT
        calls.append((complexity, cache_eligible))
        return _result(strong=complexity == "HIGH")

    monkeypatch.setattr(deployment_smoke, "_execute", execute)
    with httpx.Client() as client:
        deployment_smoke.run_smoke(
            client=client,
            traceparent=TRACEPARENT,
            marker="run-1",
            semantic_cache_enabled=True,
        )

    assert calls == [("LOW", False), ("HIGH", True)]


def test_smoke_rejects_missing_embedding_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not expose the UI when live embedding evidence is incomplete."""

    def execute(
        client: httpx.Client,
        *,
        traceparent: str,
        complexity: str,
        cache_eligible: bool,
        marker: str,
    ) -> RunResult:
        del client, traceparent, cache_eligible, marker
        return _result(strong=complexity == "HIGH", embedding=False)

    monkeypatch.setattr(deployment_smoke, "_execute", execute)
    with httpx.Client() as client:
        with pytest.raises(RuntimeError, match="embedding evidence"):
            deployment_smoke.run_smoke(
                client=client,
                traceparent=TRACEPARENT,
                marker="run-1",
                semantic_cache_enabled=True,
            )


def test_smoke_proves_cache_disabled_evidence_and_model_only_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept disabled production only with bypass evidence and no embedding use."""
    calls: list[tuple[str, bool]] = []

    def execute(
        client: httpx.Client,
        *,
        traceparent: str,
        complexity: str,
        cache_eligible: bool,
        marker: str,
    ) -> RunResult:
        del client, traceparent, marker
        calls.append((complexity, cache_eligible))
        return _result(
            strong=complexity == "HIGH",
            semantic_cache_enabled=False,
        )

    monkeypatch.setattr(deployment_smoke, "_execute", execute)
    with httpx.Client() as client:
        deployment_smoke.run_smoke(
            client=client,
            traceparent=TRACEPARENT,
            marker="run-1",
            semantic_cache_enabled=False,
        )

    assert calls == [("LOW", False), ("HIGH", True)]


def test_smoke_rejects_judge_false_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require the literal smoke output even when evaluator evidence passes."""

    def execute(
        client: httpx.Client,
        *,
        traceparent: str,
        complexity: str,
        cache_eligible: bool,
        marker: str,
    ) -> RunResult:
        del client, traceparent, cache_eligible, marker
        result = _result(strong=complexity == "HIGH")
        values = {**result.__dict__, "final_output": "INCORRECT"}
        return RunResult.model_construct(**values)

    monkeypatch.setattr(deployment_smoke, "_execute", execute)
    with httpx.Client() as client:
        with pytest.raises(RuntimeError, match="exact contract"):
            deployment_smoke.run_smoke(
                client=client,
                traceparent=TRACEPARENT,
                marker="run-1",
                semantic_cache_enabled=True,
            )


def test_smoke_rejects_invalid_trace_context() -> None:
    """Require one valid W3C operation identity for telemetry verification."""
    with httpx.Client() as client:
        with pytest.raises(ValueError, match="traceparent"):
            deployment_smoke.run_smoke(
                client=client,
                traceparent="invalid",
                marker="run-1",
                semantic_cache_enabled=True,
            )


@pytest.mark.parametrize(
    "traceparent",
    [
        "00-00000000000000000000000000000000-2222222222222222-01",
        "00-11111111111111111111111111111111-0000000000000000-01",
    ],
)
def test_smoke_rejects_zero_trace_identifiers(traceparent: str) -> None:
    """Reject W3C-forbidden zero identifiers before producing smoke evidence."""
    with httpx.Client() as client:
        with pytest.raises(ValueError, match="zero ID"):
            deployment_smoke.run_smoke(
                client=client,
                traceparent=traceparent,
                marker="run-1",
                semantic_cache_enabled=True,
            )


def test_main_prints_success_marker_only_after_smoke_passes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Give the workflow explicit evidence independent of remote exit propagation."""
    smoke = Mock()
    monkeypatch.setattr(deployment_smoke, "run_smoke", smoke)
    monkeypatch.setenv("OPTIMA_API_BASE_URL", "https://api.example")
    monkeypatch.setenv("OPTIMA_API_TIMEOUT_SECONDS", "315")
    monkeypatch.setenv("OPTIMA_SEMANTIC_CACHE_ENABLED", "false")

    status = deployment_smoke.main(
        ["--traceparent", TRACEPARENT, "--run-marker", "run-1"]
    )

    assert status == 0
    assert capsys.readouterr().out.strip() == deployment_smoke.SUCCESS_MARKER
    smoke.assert_called_once()
    assert smoke.call_args.kwargs["semantic_cache_enabled"] is False
