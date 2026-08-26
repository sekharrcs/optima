"""Static contracts for the API and UI production container definitions."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def dockerfile(name: str) -> str:
    """Read one repository-root container definition as UTF-8 text."""
    return (ROOT / name).read_text(encoding="utf-8")


def test_api_image_uses_locked_non_root_production_entrypoint() -> None:
    """Package the production factory on port 8000 without development dependencies."""
    content = dockerfile("Dockerfile.api")

    assert "FROM python:3.12.12-slim-bookworm" in content
    assert "uv sync --frozen --no-dev --no-editable --no-cache" in content
    assert "USER 10001:10001" in content
    assert "EXPOSE 8000" in content
    assert (
        'CMD ["uvicorn", "optima.api.production:create_production_app", '
        '"--factory", "--host", "0.0.0.0", "--port", "8000"]'
    ) in content


def test_ui_image_uses_locked_non_root_streamlit_entrypoint() -> None:
    """Package Streamlit on port 8501 with environment-based API routing."""
    content = dockerfile("Dockerfile.ui")

    assert "FROM python:3.12.12-slim-bookworm" in content
    assert "uv sync --frozen --no-dev --no-editable --no-cache" in content
    assert "STREAMLIT_SERVER_ADDRESS=0.0.0.0" in content
    assert "USER 10001:10001" in content
    assert "EXPOSE 8501" in content
    assert 'CMD ["streamlit", "run", "src/ui/app.py"]' in content


def test_docker_context_is_an_explicit_source_allow_list() -> None:
    """Exclude credentials, local environments, tests, and source-control metadata."""
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert patterns == ["**", "!pyproject.toml", "!uv.lock", "!src/", "!src/**"]
