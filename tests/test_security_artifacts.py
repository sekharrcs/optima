"""Tests for reproducible pre-deployment security evidence."""

import json
import re
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.generate_sbom import generate_sbom

ROOT = Path(__file__).resolve().parents[1]
PR_SECURITY_WORKFLOW = ROOT / ".github" / "workflows" / "pr-security-containers.yml"
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


def _pr_security_workflow() -> str:
    assert PR_SECURITY_WORKFLOW.is_file(), (
        ".github/workflows/pr-security-containers.yml must exist"
    )
    return PR_SECURITY_WORKFLOW.read_text(encoding="utf-8")


def _top_level_block(content: str, key: str) -> list[str]:
    lines = content.splitlines()
    start = lines.index(f"{key}:") + 1
    block: list[str] = []
    for line in lines[start:]:
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            break
        if line.strip() and not line.lstrip().startswith("#"):
            block.append(line)
    return block


def _run_scripts(content: str) -> str:
    lines = content.splitlines()
    scripts: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:-\s+)?run:\s*(.*)$", line)
        if match is None:
            continue
        inline = match.group(2)
        if inline not in {"", "|", ">", "|-", ">-"}:
            scripts.append(inline)
            continue
        indentation = len(match.group(1))
        for script_line in lines[index + 1 :]:
            if (
                script_line.strip()
                and len(script_line) - len(script_line.lstrip()) <= indentation
            ):
                break
            scripts.append(script_line)
    return "\n".join(scripts)


def _literal_run_blocks(content: str) -> list[str]:
    """Return literal workflow run blocks after YAML indentation removal."""
    lines = content.splitlines()
    blocks: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:-\s+)?run:\s*(\|-?)\s*$", line)
        if match is None:
            continue
        parent_indentation = len(match.group(1))
        raw_block: list[str] = []
        for block_line in lines[index + 1 :]:
            indentation = len(block_line) - len(block_line.lstrip())
            if block_line.strip() and indentation <= parent_indentation:
                break
            raw_block.append(block_line)
        first_content = next(line for line in raw_block if line.strip())
        content_indentation = len(first_content) - len(first_content.lstrip())
        block: list[str] = []
        for block_line in raw_block:
            if not block_line.strip():
                block.append("")
                continue
            indentation = len(block_line) - len(block_line.lstrip())
            assert indentation >= content_indentation
            block.append(block_line[content_indentation:])
        blocks.append("\n".join(block))
    return blocks


def _python_heredocs(content: str) -> list[str]:
    """Extract quoted Python heredocs from dedented workflow run blocks."""
    programs: list[str] = []
    for block in _literal_run_blocks(content):
        lines = block.splitlines()
        for index, line in enumerate(lines):
            if re.search(r"<<'PY'\s*$", line) is None:
                continue
            try:
                end = lines.index("PY", index + 1)
            except ValueError as error:
                raise AssertionError(
                    "Python heredoc is missing its terminator"
                ) from error
            programs.append("\n".join(lines[index + 1 : end]) + "\n")
    return programs


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


def test_pr_security_workflow_uses_only_the_pull_request_main_trigger() -> None:
    """Run unprivileged PR validation without a target-repository trigger."""
    content = _pr_security_workflow()
    trigger = _top_level_block(content, "on")
    trigger_keys = {
        match.group(1)
        for line in trigger
        if (match := re.fullmatch(r"  ([a-z_]+):", line)) is not None
    }

    assert trigger_keys == {"pull_request"}
    assert any(line.strip() == "pull_request:" for line in trigger)
    assert [line.strip() for line in trigger if line.lstrip().startswith("-")] == [
        "- main"
    ]
    assert "pull_request_target" not in content


def test_pr_security_workflow_has_exact_read_only_permissions() -> None:
    """Prevent repository writes at workflow and job scope."""
    content = _pr_security_workflow()

    assert _top_level_block(content, "permissions") == ["  contents: read"]
    assert content.count("permissions:") == 1
    assert re.search(r"(?mi)^\s+[a-z-]+:\s*write\s*$", content) is None


def test_pr_security_workflow_checks_out_the_exact_head_without_credentials() -> None:
    """Bind both jobs to the immutable PR head without retaining credentials."""
    content = _pr_security_workflow()

    assert content.count("uses: actions/checkout@") == 2
    assert content.count('ref: "${{ github.event.pull_request.head.sha }}"') == 2
    assert content.count("persist-credentials: false") == 2


def test_pr_security_workflow_pins_every_action_to_a_full_commit() -> None:
    """Reject mutable tags for every referenced GitHub Action."""
    content = _pr_security_workflow()
    references = re.findall(
        r"(?m)^\s*(?:-\s+)?uses:\s*([^\s#]+)\s*$",
        content,
    )

    assert references
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", ref) for ref in references)


def test_pr_security_workflow_has_no_cloud_or_secret_capability() -> None:
    """Keep PR execution credential-free and unable to mutate Azure or GitHub."""
    content = _pr_security_workflow()
    normalized = content.casefold()
    forbidden = (
        "azure/login",
        "az login",
        "az deployment",
        "azd ",
        "${{ secrets.",
        "pull_request_target",
    )

    assert all(value not in normalized for value in forbidden)
    assert re.search(r"(?mi)^\s*uses:\s*azure/", content) is None
    assert re.search(r"(?mi)^\s*environment:\s*\S+", content) is None
    assert re.search(r"(?mi)(?:^|[;&|]\s*)(?:az|azd)\s+", _run_scripts(content)) is None


def test_pr_security_workflow_keeps_event_data_out_of_shell_scripts() -> None:
    """Pass the trusted head SHA through a quoted environment variable only."""
    content = _pr_security_workflow()
    scripts = _run_scripts(content)
    head_environment = re.search(
        r"(?m)^\s*([A-Z][A-Z0-9_]*)\s*:\s*"
        r"(['\"])\$\{\{\s*github\.event\.pull_request\.head\.sha\s*\}\}\2\s*$",
        content,
    )

    assert head_environment is not None
    head_variable = head_environment.group(1)
    assert "${{" not in scripts
    assert "git rev-parse HEAD" in scripts
    assert re.search(
        rf'"(?:\$\{{{head_variable}\}}|\${head_variable})"',
        scripts,
    )
    unsafe_values = (
        "github.event.pull_request.title",
        "github.event.pull_request.body",
        "github.event.pull_request.head.ref",
        "github.event.pull_request.base.ref",
        "github.event.pull_request.labels",
        "github.actor",
        "github.head_ref",
        "github.ref_name",
    )
    assert all(value not in scripts for value in unsafe_values)


def test_pr_security_workflow_scopes_concurrency_to_numeric_pr_number() -> None:
    """Cancel stale runs only within one GitHub-controlled pull request."""
    content = _pr_security_workflow()

    assert _top_level_block(content, "concurrency") == [
        "  group: pr-security-containers-${{ github.event.pull_request.number }}",
        "  cancel-in-progress: true",
    ]
    assert "github.event.pull_request.number" not in _run_scripts(content)


def test_pr_security_workflow_top_level_env_uses_available_contexts() -> None:
    """Reject job-only contexts before GitHub can create a workflow run."""
    environment = "\n".join(_top_level_block(_pr_security_workflow(), "env"))

    assert "${{ runner." not in environment
    assert "${{ job." not in environment
    assert "${{ steps." not in environment
    assert "${{ needs." not in environment


def test_pr_security_workflow_compiles_every_dedented_python_heredoc() -> None:
    """Compile every embedded program exactly as YAML passes it to Bash."""
    programs = _python_heredocs(_pr_security_workflow())

    assert len(programs) == 8
    for index, program in enumerate(programs):
        compile(program, f"pr-security-containers.yml:heredoc-{index}", "exec")


def test_pr_security_workflow_isolates_scanners_from_checkout_configuration() -> None:
    """Bind scanner policy to trusted files outside the pull-request checkout."""
    scripts = "\n".join(_literal_run_blocks(_pr_security_workflow()))

    assert 'scanner_workspace="$RUNNER_TEMP/scanner-workspace"' in scripts
    assert 'scanner_home="$RUNNER_TEMP/scanner-home"' in scripts
    assert 'scanner_policy="$RUNNER_TEMP/scanner-policy"' in scripts
    assert 'cd "$scanner_workspace"' in scripts
    assert scripts.count('env -i HOME="$scanner_home" PATH="/usr/bin:/bin"') == 6
    assert '--config "$scanner_policy/syft.yaml"' in scripts
    assert '--config "$scanner_policy/trivy.yaml"' in scripts
    assert '--ignorefile "$scanner_policy/.trivyignore"' in scripts
    assert '--secret-config "$scanner_policy/trivy-secret.yaml"' in scripts
    trusted_trivy_config = """cat > "$scanner_policy/trivy.yaml" <<'YAML'
vulnerability:
  vex: []
  ignore-status: []
scan:
  skip-dirs: []
  skip-files: []
YAML"""
    assert trusted_trivy_config in scripts
    assert (
        re.search(
            r"(?m)^(?:vex|ignore-status|skip-dirs|skip-files):",
            scripts,
        )
        is None
    )
    syft_invocations = re.findall(
        r'(?m)^\s*(?:"\$tools/syft"|syft)\s+(.+)$',
        scripts,
    )
    trivy_invocations = re.findall(
        r'(?m)^\s*(?:"\$tools/trivy"|trivy)\s+(.+)$',
        scripts,
    )
    assert len(syft_invocations) == 3
    assert len(trivy_invocations) == 3
    assert all(
        '--config "$scanner_policy/syft.yaml"' in line for line in syft_invocations
    )
    assert all(
        '--config "$scanner_policy/trivy.yaml"' in line for line in trivy_invocations
    )
    assert '--expected-artifact "api=optima-api:pr"' in scripts
    assert '--expected-artifact "ui=optima-ui:pr"' in scripts
    assert scripts.count("--expected-image-id") == 2
    assert scripts.count("--list-all-pkgs") == 2


def test_pr_security_workflow_policy_probes_reject_python_optimization() -> None:
    """Keep policy checks active when a runtime sets PYTHONOPTIMIZE."""
    scripts = _run_scripts(_pr_security_workflow())

    assert re.search(r"(?m)^\s*assert(?:\s|\()", scripts) is None
    assert scripts.count("if sys.flags.optimize != 0:") >= 6


def test_pr_security_workflow_uploads_untracked_final_image_evidence() -> None:
    """Publish generated reports as Actions evidence, never tracked evidence."""
    content = _pr_security_workflow()

    assert "actions/upload-artifact@" in content
    assert re.search(r"(?m)^\s*path:\s*\S+", content)
    assert "security/sbom/" not in content
    assert "security/reports/" not in content
