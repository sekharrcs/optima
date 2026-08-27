"""Static contracts for the API and UI production container definitions."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def dockerfile(name: str) -> str:
    """Read one repository-root container definition as UTF-8 text."""
    return (ROOT / name).read_text(encoding="utf-8")


def test_api_image_uses_locked_non_root_production_entrypoint() -> None:
    """Package the production factory on port 8000 without development dependencies."""
    content = dockerfile("Dockerfile.api")

    assert "mcr.microsoft.com/azurelinux/base/python:3.12@sha256:" in content
    assert (
        "mcr.microsoft.com/azurelinux/distroless/python:3.12-nonroot@sha256:" in content
    )
    assert "ghcr.io/astral-sh/uv:0.12.5@sha256:" in content
    assert "uv sync --frozen --no-dev --no-editable --no-cache" in content
    assert "--component api --output /app/sbom/api.cdx.json" in content
    assert "/app/sbom ./sbom" in content
    assert "USER nonroot" in content
    assert "groupadd" not in content
    assert "useradd" not in content
    assert "EXPOSE 8000" in content
    assert (
        'CMD ["uvicorn", "optima.api.production:create_production_app", '
        '"--factory", "--host", "0.0.0.0", "--port", "8000"]'
    ) in content


def test_ui_image_uses_locked_non_root_streamlit_entrypoint() -> None:
    """Package Streamlit on port 8501 with environment-based API routing."""
    content = dockerfile("Dockerfile.ui")

    assert "mcr.microsoft.com/azurelinux/base/python:3.12@sha256:" in content
    assert (
        "mcr.microsoft.com/azurelinux/distroless/python:3.12-nonroot@sha256:" in content
    )
    assert "ghcr.io/astral-sh/uv:0.12.5@sha256:" in content
    assert "uv sync --frozen --no-dev --no-editable --no-cache" in content
    assert "--component ui --output /app/sbom/ui.cdx.json" in content
    assert "/app/sbom ./sbom" in content
    assert "STREAMLIT_SERVER_ADDRESS=0.0.0.0" in content
    assert "USER nonroot" in content
    assert "groupadd" not in content
    assert "useradd" not in content
    assert "EXPOSE 8501" in content
    assert 'CMD ["streamlit", "run", "src/ui/app.py"]' in content


def test_docker_context_is_an_explicit_source_allow_list() -> None:
    """Exclude credentials, local environments, tests, and source-control metadata."""
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert patterns == [
        "**",
        "!pyproject.toml",
        "!uv.lock",
        "!src/",
        "!src/**",
        "!scripts/",
        "!scripts/generate_sbom.py",
        "**/__pycache__/",
        "**/*.py[cod]",
        "**/.env",
        "**/.env.*",
        "**/.git",
        "**/.git/**",
    ]


def test_container_base_images_pin_reviewed_manifest_digests() -> None:
    """Require one exact trusted-registry OCI index for every build stage."""
    expected_builder = (
        "mcr.microsoft.com/azurelinux/base/python:3.12@"
        "sha256:0b729c82c0ddc0769248e287d7414f0cc4e42ae4aa5b786aa99883c247e42bdb"
    )
    expected_runtime = (
        "mcr.microsoft.com/azurelinux/distroless/python:3.12-nonroot@"
        "sha256:d921452dba64944bf959f22450bb3740f5b2fff4a59faa64bd6b8eaf4c57b5b8"
    )
    expected_uv = (
        "ghcr.io/astral-sh/uv:0.12.5@"
        "sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1"
    )

    for name in ("Dockerfile.api", "Dockerfile.ui"):
        content = dockerfile(name)
        assert content.count(expected_builder) == 1
        assert content.count(expected_runtime) == 1
        assert content.count(expected_uv) == 1
