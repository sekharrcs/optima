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
