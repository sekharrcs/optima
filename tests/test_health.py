"""Tests for the lightweight API health contract."""

from fastapi.testclient import TestClient

from optima.api.app import create_app


def test_health_endpoint_returns_stable_response() -> None:
    """Return the documented health payload as JSON."""
    application = create_app()
    response = TestClient(application).get("/api/v1/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_application_exposes_only_versioned_health_and_run_routes() -> None:
    """Keep the Slice 5 API surface minimal and versioned."""
    application = create_app()
    application_routes = set(application.openapi()["paths"])

    assert application_routes == {
        "/api/v1/health",
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
    }
