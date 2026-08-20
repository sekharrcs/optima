"""Headless startup tests for the three-view Streamlit application."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

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
    assert app.button[0].label == "Run with OPTIMA"


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
        "INC-204 affected 37 requests.\\n"
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
