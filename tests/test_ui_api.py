"""Tests for UI request construction, API transport, and local demo composition."""

import httpx
import pytest
from fastapi.testclient import TestClient

from optima.api.demo import (
    DEMO_CACHE_CONTEXT,
    DEMO_CACHE_INPUT,
    DEMO_CACHE_OUTPUT,
)
from optima.api.demo import (
    app as demo_app,
)
from optima.domain.execution import (
    ContextReductionOutcome,
    ContextSource,
    ExecutionEventCode,
    ModelPolicy,
    ModelRole,
    SemanticCacheOutcome,
)
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


def test_execute_inputs_preserve_reference_output() -> None:
    """Carry the production evaluator reference into the strict API request."""
    request = ExecuteInputs(
        input_text="Summarize this",
        reference_output="Expected summary",
    ).to_run_request()

    assert request.reference_output == "Expected summary"


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
            "Priya Nair owns incident INC-204.\nPriya Nair owns incident INC-204."
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


def test_api_client_transports_and_parses_measured_strong_direct_result() -> None:
    """Carry real HIGH strong-direct backend evidence through the UI client."""
    payload = ExecuteInputs(
        input_text="Design a distributed architecture",
        complexity=Complexity.HIGH,
    ).to_run_request()

    response = TestClient(demo_app).post(
        "/api/v1/runs", json=payload.model_dump(mode="json", exclude_none=True)
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(response.status_code, json=response.json())
    )

    result = OptimaApiClient(transport=transport).execute(payload)

    assert response.status_code == 200
    assert result.execution_plan.model_policy is ModelPolicy.STRONG_DIRECT
    assert result.execution_plan.human_readable_name == "Strong -> Verify"
    assert result.final_output == (
        "Local demo response from the configured STRONG model role."
    )
    assert result.escalated is False
    assert result.contract_met is True
    assert len(result.model_usages) == 1
    assert result.model_usages[0].model_role is ModelRole.STRONG
    assert result.total_input_tokens == 660
    assert result.total_output_tokens == 112
    assert result.total_tokens == 772
    assert result.total_calculated_cost is not None
    assert str(result.total_calculated_cost) == "0.00277"
    assert result.total_cost_provenance is not None
    assert result.total_cost_provenance.catalog_version == "local-demo-v1"
    assert result.total_cost_provenance.currency == "USD"
    assert result.final_evaluation is not None
    assert result.final_evaluation.evaluator_type == "local-demo-deterministic"
    assert result.final_evaluation.score == 0.92
    assert result.final_evaluation.passed is True
    assert result.steps[0].facts["model_role"] == ModelRole.STRONG
    assert result.steps[0].context_source is ContextSource.ORIGINAL
    assert all(step.step_type.value != "ESCALATION" for step in result.steps)
    runtime_events = {code for step in result.steps for code in step.event_codes}
    assert ExecutionEventCode.ESCALATION_REQUIRED not in runtime_events
    assert ExecutionEventCode.ESCALATED_TO_STRONG not in runtime_events


def test_api_client_transports_exact_local_cache_hit_evidence() -> None:
    """Parse a deterministic hit without converting source evidence to a new result."""
    payload = ExecuteInputs(
        input_text=DEMO_CACHE_INPUT,
        context=DEMO_CACHE_CONTEXT,
        cache_eligible=True,
    ).to_run_request()
    response = TestClient(demo_app).post(
        "/api/v1/runs", json=payload.model_dump(mode="json", exclude_none=True)
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(response.status_code, json=response.json())
    )

    result = OptimaApiClient(transport=transport).execute(payload)

    assert response.status_code == 200
    assert result.execution_plan.human_readable_name == "Cached Result"
    assert result.final_output == DEMO_CACHE_OUTPUT
    assert result.semantic_cache is not None
    assert result.semantic_cache.outcome is SemanticCacheOutcome.REUSED
    assert result.semantic_cache.source_run_id == "run-local-cache-source-1"
    assert result.semantic_cache.similarity == 1.0
    assert result.semantic_cache.prior_evaluation is not None
    assert result.semantic_cache.prior_evaluation.threshold == 0.80
    assert result.evaluations == ()
    assert result.final_evaluation is None
    assert result.model_usages == ()
    assert result.total_tokens == 0
    assert result.total_calculated_cost is None


def test_local_cache_exact_match_includes_complete_request_key() -> None:
    """Treat changed evaluation criteria as a local exact-match cache miss."""
    payload = ExecuteInputs(
        input_text=DEMO_CACHE_INPUT,
        context=DEMO_CACHE_CONTEXT,
        cache_eligible=True,
    ).to_run_request()
    payload.criteria = ("The answer must be JSON.",)

    response = TestClient(demo_app).post(
        "/api/v1/runs", json=payload.model_dump(mode="json", exclude_none=True)
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(response.status_code, json=response.json())
    )
    result = OptimaApiClient(transport=transport).execute(payload)

    assert response.status_code == 200
    assert result.execution_plan.human_readable_name != "Cached Result"
    assert result.semantic_cache is not None
    assert result.semantic_cache.outcome is SemanticCacheOutcome.MISS
    assert result.final_output != DEMO_CACHE_OUTPUT


def test_api_client_transports_local_cache_miss_before_model_fallback() -> None:
    """Parse truthful miss evidence followed by actual model execution facts."""
    payload = ExecuteInputs(
        input_text="A deterministic local cache miss",
        cache_eligible=True,
    ).to_run_request()
    response = TestClient(demo_app).post(
        "/api/v1/runs", json=payload.model_dump(mode="json", exclude_none=True)
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(response.status_code, json=response.json())
    )

    result = OptimaApiClient(transport=transport).execute(payload)

    assert response.status_code == 200
    assert result.semantic_cache is not None
    assert result.semantic_cache.outcome is SemanticCacheOutcome.MISS
    assert result.semantic_cache.source_run_id is None
    assert result.semantic_cache.similarity is None
    assert result.steps[0].event_codes == (ExecutionEventCode.CACHE_MISS,)
    assert len(result.model_usages) == 1


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


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.internal",
        "https://user:password@api.internal",
        "https://api.internal/path",
        "https://api.internal?target=other",
        "https://api.internal#fragment",
    ],
)
def test_production_api_client_rejects_insecure_or_ambiguous_roots(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    """Fail closed on non-HTTPS or non-root production destinations."""
    monkeypatch.setenv("OPTIMA_UI_PRODUCTION_MODE", "true")
    monkeypatch.setenv("OPTIMA_API_BASE_URL", base_url)

    with pytest.raises(ValueError, match="absolute HTTPS root URL"):
        OptimaApiClient.from_environment()


def test_production_api_client_requires_explicit_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent production mode from silently falling back to localhost."""
    monkeypatch.setenv("OPTIMA_UI_PRODUCTION_MODE", "true")
    monkeypatch.delenv("OPTIMA_API_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="OPTIMA_API_BASE_URL is required"):
        OptimaApiClient.from_environment()


def test_local_api_client_preserves_trusted_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep an explicit environment-only local development workflow."""
    requested_urls: list[httpx.URL] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested_urls.append(request.url)
        return httpx.Response(503, json={})

    monkeypatch.setenv("OPTIMA_UI_PRODUCTION_MODE", "false")
    monkeypatch.setenv("OPTIMA_API_BASE_URL", "http://localhost:8765")
    client = OptimaApiClient.from_environment(transport=httpx.MockTransport(respond))

    with pytest.raises(ApiClientError):
        client.execute(execute_inputs().to_run_request())

    assert requested_urls == [httpx.URL("http://localhost:8765/api/v1/runs")]


@pytest.mark.parametrize("timeout", ["0", "361", "nan", "not-a-number"])
def test_environment_api_client_rejects_invalid_timeout(
    monkeypatch: pytest.MonkeyPatch,
    timeout: str,
) -> None:
    """Reject non-finite, non-positive, and unreasonably large UI deadlines."""
    monkeypatch.setenv("OPTIMA_API_TIMEOUT_SECONDS", timeout)

    with pytest.raises(ValueError, match="OPTIMA_API_TIMEOUT_SECONDS"):
        OptimaApiClient.from_environment()


def test_environment_api_client_applies_configured_transport_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the trusted production timeout budget for each HTTP client."""
    observed_timeout: list[float] = []

    def timeout_response(request: httpx.Request) -> httpx.Response:
        observed_timeout.append(float(request.extensions["timeout"]["read"]))
        return httpx.Response(503, json={})

    monkeypatch.setenv("OPTIMA_UI_PRODUCTION_MODE", "true")
    monkeypatch.setenv("OPTIMA_API_BASE_URL", "https://api.internal")
    monkeypatch.setenv("OPTIMA_API_TIMEOUT_SECONDS", "315")

    with pytest.raises(ApiClientError):
        OptimaApiClient.from_environment(
            transport=httpx.MockTransport(timeout_response)
        ).execute(execute_inputs().to_run_request())

    assert observed_timeout == [315.0]


def test_api_client_rejects_redirect_without_contacting_target() -> None:
    """Do not follow a configured API redirect into another trust domain."""
    requested_urls: list[httpx.URL] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requested_urls.append(request.url)
        return httpx.Response(
            307,
            headers={"location": "https://untrusted.example/api/v1/runs"},
        )

    with pytest.raises(ApiClientError) as captured:
        OptimaApiClient(transport=httpx.MockTransport(redirect)).execute(
            execute_inputs().to_run_request()
        )

    assert captured.value.code == "API_REDIRECT_REJECTED"
    assert requested_urls == [httpx.URL("http://127.0.0.1:8000/api/v1/runs")]
