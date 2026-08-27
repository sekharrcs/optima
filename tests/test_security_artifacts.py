"""Tests for reproducible pre-deployment security evidence."""

import json
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.generate_sbom import generate_sbom

ROOT = Path(__file__).resolve().parents[1]
APPROVED_REGISTRY = "https://packagefeedproxy.microsoft.io/pypi/simple"
DEV_PACKAGES = {
    "ast-serialize",
    "iniconfig",
    "librt",
    "mypy",
    "mypy-extensions",
    "pathspec",
    "pluggy",
    "pygments",
    "pytest",
    "ruff",
}


def _locked_packages() -> dict[str, dict[str, Any]]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {package["name"]: package for package in lock["package"]}


def _dependency_names(package: dict[str, Any], key: str) -> set[str]:
    dependencies = package.get(key, [])
    if not isinstance(dependencies, list):
        raise AssertionError(f"{key} must be a dependency list")
    return {dependency["name"] for dependency in dependencies}


def _closure(
    packages: dict[str, dict[str, Any]],
    roots: set[str],
) -> set[str]:
    resolved: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in resolved:
            continue
        package = packages[name]
        resolved.add(name)
        pending.extend(_dependency_names(package, "dependencies") - resolved)
    return resolved


def test_lock_uses_only_reviewed_registry_sources_and_hashed_artifacts() -> None:
    """Reject third-party VCS, URL, path, and unhashed package artifacts."""
    packages = _locked_packages()

    for name, package in packages.items():
        source = package["source"]
        if name == "optima":
            assert source == {"editable": "."}
            continue
        assert source == {"registry": APPROVED_REGISTRY}
        artifacts = [package.get("sdist"), *package.get("wheels", [])]
        present_artifacts = [artifact for artifact in artifacts if artifact is not None]
        assert present_artifacts
        for artifact in present_artifacts:
            assert artifact["hash"].startswith("sha256:")
            hostname = urlparse(artifact["url"]).hostname
            assert hostname is not None
            assert hostname == "packagefeedproxy.microsoft.io" or (
                hostname.startswith("ms-feed-")
                and hostname.endswith(".pkgs.visualstudio.com")
            )


def test_lock_contains_one_explainable_runtime_and_development_graph() -> None:
    """Require every locked package to have a direct or transitive parent."""
    packages = _locked_packages()
    project = packages["optima"]
    runtime = _closure(packages, _dependency_names(project, "dependencies"))
    dev_groups = project["dev-dependencies"]
    dev_roots = {
        dependency["name"]
        for dependencies in dev_groups.values()
        for dependency in dependencies
    }
    development = _closure(packages, dev_roots)

    assert set(packages) == {"optima"} | runtime | development
    assert DEV_PACKAGES.isdisjoint(runtime)
    assert DEV_PACKAGES.issubset(development)


def test_committed_sboms_match_the_patched_production_inventory() -> None:
    """Record both deployed components with no development dependency leakage."""
    documents = [
        json.loads((ROOT / "security" / "sbom" / name).read_text(encoding="utf-8"))
        for name in ("api.cdx.json", "ui.cdx.json")
    ]
    component_names = [
        {component["name"] for component in document["components"]}
        for document in documents
    ]

    assert [document["metadata"]["component"]["name"] for document in documents] == [
        "optima-api",
        "optima-ui",
    ]
    assert component_names[0] == component_names[1]
    assert DEV_PACKAGES.isdisjoint(component_names[0])
    for document in documents:
        versions = {
            component["name"]: component["version"]
            for component in document["components"]
        }
        assert versions["streamlit"] == "1.54.0"
        assert versions["pillow"] == "12.3.0"


def test_sbom_generator_identifies_runtime_and_installed_versions(
    tmp_path: Path,
) -> None:
    """Generate deterministic CycloneDX evidence from the active environment."""
    output = tmp_path / "api.cdx.json"

    first = generate_sbom("api", output)
    first_text = output.read_text(encoding="utf-8")
    second = generate_sbom("api", output)

    assert first == second
    assert output.read_text(encoding="utf-8") == first_text
    assert first["bomFormat"] == "CycloneDX"
    assert first["specVersion"] == "1.6"
    assert first["metadata"]["component"]["name"] == "optima-api"
    assert first["metadata"]["component"]["properties"] == [
        {"name": "optima:runtime-image", "value": "api"},
        {
            "name": "optima:inventory-source",
            "value": "active-python-environment",
        },
    ]
    assert all(component["name"] for component in first["components"])
    assert all(component["version"] for component in first["components"])
    assert "optima" not in {component["name"] for component in first["components"]}
    assert json.loads(first_text) == first
