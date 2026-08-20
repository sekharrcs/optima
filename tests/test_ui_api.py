"""Tests for UI request construction, API transport, and local demo composition."""

import httpx
import pytest
from fastapi.testclient import TestClient

from optima.api.demo import app as demo_app
from optima.domain.execution import ContextReductionOutcome, ContextSource
from optima.domain.quality_contract import OptimizationMode, QualityProfile, RiskTier
from optima.domain.request_profile import Complexity, TaskType
from ui.api_client import ApiClientError, OptimaApiClient
from ui.models import ExecuteInputs


def execute_inputs() -> ExecuteInputs:
    """Build complete explicitly supplied demo inputs."""
    return ExecuteInputs(
        input_text="Summarize the incident",
        context="Supporting evidence",
        quality_profile=QualityProfile.HIGH,
        optimization_mode=OptimizationMode.COST,
        task_type=TaskType.LOG_ANALYSIS,
        complexity=Complexity.MEDIUM,
        input_tokens=725,
        profile_risk_tier=RiskTier.MEDIUM,
        contract_risk_tier=RiskTier.HIGH,
        cache_eligible=False,
        has_large_context=False,
        max_latency_ms=5_000,
    )


def test_execute_inputs_build_exact_advanced_profile_request() -> None:
    """Keep supplied profile facts explicit and separate from contract risk."""
    request = execute_inputs().to_run_request()

    assert request.request_profile.task_type is TaskType.LOG_ANALYSIS
    assert request.request_profile.complexity is Complexity.MEDIUM
    assert request.request_profile.input_tokens == 725
    assert request.request_profile.risk_tier is RiskTier.MEDIUM
    assert request.risk_tier is RiskTier.HIGH
    assert request.metadata == {"request_profile_source": "user_supplied_demo_input"}


def test_api_client_serializes_request_and_parses_run_result() -> None:
    """Parse the actual demo API response through the strict client boundary."""
    with TestClient(demo_app) as test_client:
        response = test_client.post(
            "/api/v1/runs",
            json=ExecuteInputs(input_text="Summarize this")
            .to_run_request()
            .model_dump(mode="json", exclude_none=True),
        )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=response.json())
    )

    result = OptimaApiClient(transport=transport).execute(
        ExecuteInputs(input_text="Summarize this").to_run_request()
    )

    assert result.final_output == (
        "Local demo response from the configured SMALL model role."
    )
    assert result.contract_met is True
    assert result.total_calculated_cost is not None
    assert result.total_cost_provenance is not None
    assert result.total_cost_provenance.catalog_version == "local-demo-v1"


def test_api_client_parses_typed_context_reduction_evidence() -> None:
    """Validate nested measured reduction facts through the real UI transport."""
    inputs = ExecuteInputs(
        input_text="Summarize incident requirements",
        context=(
            "Priya Nair owns incident INC-204.\n"
            "INC-204 affected 37 requests.\n"
            "Unrelated social update for the wider team."
        ),
        input_tokens=4_000,
        has_large_context=True,
    )
    response = TestClient(demo_app).post(
        "/api/v1/runs",
        json=inputs.to_run_request().model_dump(mode="json", exclude_none=True),
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(response.status_code, json=response.json())
    )

    result = OptimaApiClient(transport=transport).execute(inputs.to_run_request())

    assert result.steps[0].context_reduction is not None
    assert result.steps[0].context_reduction.outcome is ContextReductionOutcome.APPLIED
    assert result.steps[0].context_reduction.context_source is ContextSource.REDUCED
    assert result.steps[1].context_source is ContextSource.REDUCED


def test_local_demo_preserves_unsupported_strong_direct_error() -> None:
    """Do not route around Planner V1 when the current executor lacks a plan."""
    payload = ExecuteInputs(
        input_text="Design a distributed architecture",
        complexity=Complexity.HIGH,
    ).to_run_request()

    response = TestClient(demo_app).post(
        "/api/v1/runs", json=payload.model_dump(mode="json", exclude_none=True)
    )

    assert response.status_code == 501
    assert response.json()["detail"]["code"] == "UNSUPPORTED_EXECUTION_PLAN"


@pytest.mark.parametrize(
    ("exception", "expected_code"),
    [
        (httpx.ConnectError("offline"), "API_CONNECTION_FAILED"),
        (httpx.ReadTimeout("slow"), "API_TIMEOUT"),
    ],
)
def test_api_client_handles_transport_failures(
    exception: Exception,
    expected_code: str,
) -> None:
    """Convert connection and timeout failures into stable unsuccessful states."""

    def fail(_: httpx.Request) -> httpx.Response:
        raise exception

    with pytest.raises(ApiClientError) as captured:
        OptimaApiClient(transport=httpx.MockTransport(fail)).execute(
            execute_inputs().to_run_request()
        )

    assert captured.value.code == expected_code


@pytest.mark.parametrize(
    ("status_code", "body", "expected_code"),
    [
        (
            503,
            {
                "detail": {
                    "code": "EXECUTION_NOT_CONFIGURED",
                    "message": "Execution is not configured",
                    "facts": {},
                }
            },
            "EXECUTION_NOT_CONFIGURED",
        ),
        (
            422,
            {"detail": [{"loc": ["body"], "msg": "invalid"}]},
            "REQUEST_VALIDATION_FAILED",
        ),
    ],
)
def test_api_client_handles_structured_api_errors(
    status_code: int,
    body: object,
    expected_code: str,
) -> None:
    """Preserve structured execution and validation failure categories."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, json=body)
    )

    with pytest.raises(ApiClientError) as captured:
        OptimaApiClient(transport=transport).execute(execute_inputs().to_run_request())

    assert captured.value.code == expected_code


def test_api_client_rejects_non_json_success_response() -> None:
    """Do not turn an invalid successful-looking response into run evidence."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="not-json")
    )

    with pytest.raises(ApiClientError) as captured:
        OptimaApiClient(transport=transport).execute(execute_inputs().to_run_request())

    assert captured.value.code == "INVALID_API_RESPONSE"
