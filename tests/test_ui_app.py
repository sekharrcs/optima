"""Headless startup tests for the three-view Streamlit application."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from optima.api.models import MAX_INPUT_TEXT_CHARACTERS
from optima.domain.quality_contract import OptimizationMode, QualityProfile
from ui.app import PRIMARY_VIEWS

APP_PATH = Path(__file__).parents[1] / "src" / "ui" / "app.py"


def test_streamlit_app_starts_without_network_calls() -> None:
    """Render Execute defaults without requiring a running API or public network."""
    app = AppTest.from_file(str(APP_PATH), default_timeout=10).run()

    assert not app.exception
    assert PRIMARY_VIEWS == ("Execute", "Dashboard", "Run History")
    assert app.title[0].value == "OPTIMA"
    assert app.selectbox[0].value is QualityProfile.HIGH
    assert app.selectbox[1].value is OptimizationMode.COST
    assert app.text_area[2].label == "Reference output (optional benchmark input)"
    assert not app.text_input
    assert app.button[0].label == "Run with OPTIMA"


def test_exact_reference_deployment_marks_reference_as_required() -> None:
    """Explain the benchmark input requirement without adding a settings console."""
    app = AppTest.from_string(
        """
import os
os.environ["OPTIMA_REQUIRE_REFERENCE_OUTPUT"] = "true"
from ui.app import execute_page
execute_page()
"""
    ).run()

    assert not app.exception
    assert (
        app.text_area[2].label
        == "Reference output (required for exact-reference benchmark)"
    )


def test_oversized_execute_input_renders_controlled_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep local UI validation failures inside the display-safe boundary."""
    monkeypatch.setenv("OPTIMA_REQUIRE_REFERENCE_OUTPUT", "false")
    app = AppTest.from_file(str(APP_PATH), default_timeout=10).run()

    app.text_area[0].input("x" * (MAX_INPUT_TEXT_CHARACTERS + 1))
    app.button[0].click().run()

    assert not app.exception
    assert [error.value for error in app.error] == [
        "The supplied request exceeds an API field limit."
    ]


def test_empty_dashboard_and_history_views_start() -> None:
    """Render both non-execution views with honest session-local empty states."""
    dashboard = AppTest.from_string(
        "from ui.app import dashboard_page\ndashboard_page()"
    ).run()
    history = AppTest.from_string(
        "from ui.app import run_history_page\nrun_history_page()"
    ).run()

    assert not dashboard.exception
    assert dashboard.title[0].value == "Dashboard"
    assert "Run OPTIMA" in dashboard.info[0].value
    assert not history.exception
    assert history.title[0].value == "Run History"
    assert "No runs" in history.info[0].value


def test_execute_result_renders_measured_context_reduction_facts() -> None:
    """Render backend counts, ratio, and method through Streamlit AppTest."""
    app = AppTest.from_string(
        """
import httpx
from fastapi.testclient import TestClient
from optima.api.demo import app as demo_app
from ui.api_client import OptimaApiClient
from ui.app import _render_execute_result
from ui.history import HistoryEntry
from ui.models import ExecuteInputs

inputs = ExecuteInputs(
    input_text="Summarize incident requirements",
    context=(
        "Priya Nair owns incident INC-204.\\n"
        "Priya Nair owns incident INC-204."
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
_render_execute_result(HistoryEntry(result=result))
"""
    ).run()

    metrics = {metric.label: metric.value for metric in app.metric}
    assert not app.exception
    assert int(metrics["Original context tokens"]) > int(
        metrics["Effective context tokens"]
    )
    assert metrics["Context reduction"].endswith("%")
    assert metrics["Reduction method"] == "RELEVANCE_AND_FACT_EXTRACTIVE_V1"


def test_execute_result_renders_actual_strong_direct_evidence() -> None:
    """Render one real HIGH strong-direct result through stable AppTest elements."""
    app = AppTest.from_string(
        """
import httpx
from fastapi.testclient import TestClient
from optima.api.demo import app as demo_app
from optima.domain.request_profile import Complexity
from ui.api_client import OptimaApiClient
from ui.app import _render_execute_result
from ui.history import HistoryEntry
from ui.models import ExecuteInputs

inputs = ExecuteInputs(
    input_text="Design a distributed architecture",
    complexity=Complexity.HIGH,
)
response = TestClient(demo_app).post(
    "/api/v1/runs",
    json=inputs.to_run_request().model_dump(mode="json", exclude_none=True),
)
transport = httpx.MockTransport(
    lambda request: httpx.Response(response.status_code, json=response.json())
)
result = OptimaApiClient(transport=transport).execute(inputs.to_run_request())
_render_execute_result(HistoryEntry(result=result))
"""
    ).run()

    metrics = {metric.label: metric.value for metric in app.metric}
    markdown_values = [element.value for element in app.markdown]
    trace_facts = " ".join(str(element.value) for element in app.json)

    assert not app.exception
    assert "Strong -> Verify" in [element.value for element in app.subheader]
    assert metrics["Generation calls"] == "1"
    assert metrics["Judge calls"] == "0"
    assert metrics["Escalation"] == "Not required"
    assert metrics["Total tokens"] == "772"
    assert metrics["Calculated cost"] == "USD 0.00277 (catalog local-demo-v1)"
    assert metrics["Latency"].endswith(" ms")
    assert int(metrics["Latency"].removesuffix(" ms")) >= 0
    assert metrics["Contract"] == "Contract Met"
    assert metrics["Final quality"] == "0.92"
    assert any(
        "Local demo response from the configured STRONG model role." in value
        for value in markdown_values
    )
    assert any("Model: STRONG_DIRECT" in value for value in markdown_values)
    assert "STRONG" in trace_facts
    assert any("Model Call: Succeeded" in element.value for element in app.success)


def test_execute_result_renders_backend_cache_hit_evidence() -> None:
    """Render the exact local hit, source quality, and zero model execution."""
    app = AppTest.from_string(
        """
import httpx
from fastapi.testclient import TestClient
from optima.api.demo import (
    DEMO_CACHE_CONTEXT,
    DEMO_CACHE_INPUT,
    app as demo_app,
)
from ui.api_client import OptimaApiClient
from ui.app import _render_execute_result
from ui.history import HistoryEntry
from ui.models import ExecuteInputs

inputs = ExecuteInputs(
    input_text=DEMO_CACHE_INPUT,
    context=DEMO_CACHE_CONTEXT,
    cache_eligible=True,
)
response = TestClient(demo_app).post(
    "/api/v1/runs",
    json=inputs.to_run_request().model_dump(mode="json", exclude_none=True),
)
transport = httpx.MockTransport(
    lambda request: httpx.Response(response.status_code, json=response.json())
)
result = OptimaApiClient(transport=transport).execute(inputs.to_run_request())
_render_execute_result(HistoryEntry(result=result))
"""
    ).run()

    metrics = {metric.label: metric.value for metric in app.metric}
    markdown_values = [element.value for element in app.markdown]

    assert not app.exception
    assert "Cached Result" in [element.value for element in app.subheader]
    assert metrics["Generation calls"] == "0"
    assert metrics["Judge calls"] == "0"
    assert metrics["Escalation"] == "Not required"
    assert metrics["Contract"] == "Contract Met"
    assert metrics["Cache outcome"] == "Reused"
    assert metrics["Cache similarity"] == "1.000"
    assert metrics["Cache source run"] == "run-local-cache-source-1"
    assert metrics["Cached quality"] == "0.96"
    assert metrics["Source threshold"] == "0.80"
    assert metrics["Source evaluation"] == "Passed"
    assert metrics["Latency"].endswith(" ms")
    assert any(
        "Incident OPT-9 was resolved after validation." in value
        for value in markdown_values
    )
    assert any("Semantic Cache: Succeeded" in item.value for item in app.success)


def test_run_history_detail_shows_generation_and_judge_call_counts() -> None:
    """Expose both model-call categories before the detailed usage table."""
    app = AppTest.from_string(
        """
from fastapi.testclient import TestClient
from optima.api.demo import app as demo_app
from optima.domain.run import RunResult
from ui.app import _render_run_detail
from ui.history import HistoryEntry
from ui.models import ExecuteInputs

inputs = ExecuteInputs(input_text="Summarize incident requirements")
response = TestClient(demo_app).post(
    "/api/v1/runs",
    json=inputs.to_run_request().model_dump(mode="json", exclude_none=True),
)
payload = response.json()
for field in (
    "total_input_tokens",
    "total_output_tokens",
    "total_tokens",
    "total_calculated_cost",
    "total_cost_provenance",
):
    payload.pop(field)
result = RunResult.model_validate(payload)
_render_run_detail(HistoryEntry(result=result))
""",
        default_timeout=20,
    ).run()

    metrics = {metric.label: metric.value for metric in app.metric}
    assert not app.exception
    assert metrics["Generation calls"] == "1"
    assert metrics["Judge calls"] == "0"
