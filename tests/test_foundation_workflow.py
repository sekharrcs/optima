"""Parsed safety contracts for foundation plan and apply workflow isolation."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "foundation.yml"
PRODUCTION_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-production.yml"
MUTATION_CONCURRENCY_GROUP = "optima-hackathon-azure-mutation"

YAML: Any
try:
    YAML = importlib.import_module("yaml")
except ModuleNotFoundError:
    YAML = None

APPLICATION_TOKENS = (
    "docker build",
    "docker push",
    "az acr login",
    "az containerapp",
    "deployContainerApps=true",
    "deployRuntimeAccess=true",
    "exposePublicUi=true",
    "authConfigs",
    "redisEmbedding",
    "uiAuthClientSecret",
)


def _yaml_scalar(value: str) -> str:
    """Decode the quoted scalar forms used by the repository workflows."""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        parsed = json.loads(value)
        assert isinstance(parsed, str)
        return parsed
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _next_content(lines: list[str], start: int) -> int:
    """Return the next nonblank, noncomment line index."""
    index = start
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#"):
            return index
        index += 1
    return index


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_block_scalar(
    lines: list[str], start: int, parent_indent: int
) -> tuple[str, int]:
    """Parse one literal workflow command block."""
    end = start
    while end < len(lines):
        line = lines[end]
        if line.strip() and _line_indent(line) <= parent_indent:
            break
        end += 1
    content_lines = lines[start:end]
    nonblank = [line for line in content_lines if line.strip()]
    content_indent = min((_line_indent(line) for line in nonblank), default=0)
    content = "\n".join(
        line[content_indent:] if line.strip() else "" for line in content_lines
    )
    return content + "\n", end


def _split_mapping_line(value: str) -> tuple[str, str]:
    key, separator, remainder = value.partition(":")
    if not separator or not key or key != key.strip():
        raise AssertionError("workflow contains unsupported YAML mapping syntax")
    return key, remainder.strip()


def _parse_mapping(
    lines: list[str], start: int, indent: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    index = start
    while index < len(lines):
        index = _next_content(lines, index)
        if index >= len(lines):
            break
        line = lines[index]
        current_indent = _line_indent(line)
        if current_indent < indent:
            break
        if current_indent != indent or line.lstrip().startswith("- "):
            raise AssertionError("workflow contains unsupported YAML indentation")
        key, value = _split_mapping_line(line.strip())
        if key in result:
            raise AssertionError("workflow contains a duplicate YAML key")
        if value in {"|", "|-", ">", ">-"}:
            result[key], index = _parse_block_scalar(lines, index + 1, indent)
            continue
        if value:
            result[key] = _yaml_scalar(value)
            index += 1
            continue
        child_index = _next_content(lines, index + 1)
        if child_index >= len(lines) or _line_indent(lines[child_index]) <= indent:
            result[key] = ""
            index = child_index
            continue
        child_indent = _line_indent(lines[child_index])
        if lines[child_index].lstrip().startswith("- "):
            result[key], index = _parse_sequence(lines, child_index, child_indent)
        else:
            result[key], index = _parse_mapping(lines, child_index, child_indent)
    return result, index


def _parse_sequence(lines: list[str], start: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    index = start
    while index < len(lines):
        index = _next_content(lines, index)
        if index >= len(lines):
            break
        line = lines[index]
        current_indent = _line_indent(line)
        if current_indent < indent:
            break
        if current_indent != indent or not line.lstrip().startswith("- "):
            raise AssertionError("workflow contains unsupported YAML sequence syntax")
        value = line.lstrip()[2:].strip()
        if ":" not in value:
            result.append(_yaml_scalar(value))
            index += 1
            continue
        key, item_value = _split_mapping_line(value)
        item: dict[str, Any] = {}
        if item_value in {"|", "|-", ">", ">-"}:
            item[key], index = _parse_block_scalar(lines, index + 1, indent)
        elif item_value:
            item[key] = _yaml_scalar(item_value)
            index += 1
        else:
            child_index = _next_content(lines, index + 1)
            child_indent = _line_indent(lines[child_index])
            if lines[child_index].lstrip().startswith("- "):
                item[key], index = _parse_sequence(lines, child_index, child_indent)
            else:
                item[key], index = _parse_mapping(lines, child_index, child_indent)
        continuation = _next_content(lines, index)
        if continuation < len(lines) and _line_indent(lines[continuation]) > indent:
            extra_indent = _line_indent(lines[continuation])
            extra, index = _parse_mapping(lines, continuation, extra_indent)
            if set(item).intersection(extra):
                raise AssertionError("workflow contains a duplicate YAML key")
            item.update(extra)
        result.append(item)
    return result, index


def _parse_workflow_without_dependency(content: str) -> dict[str, Any]:
    """Parse the mapping/sequence/scalar YAML subset used by Actions files."""
    document, final_index = _parse_mapping(content.splitlines(), 0, 0)
    if _next_content(content.splitlines(), final_index) != len(content.splitlines()):
        raise AssertionError("workflow YAML was not consumed completely")
    return document


def _load_workflow(path: Path = WORKFLOW) -> dict[str, Any]:
    """Parse a workflow without YAML 1.1 coercion of the ``on`` key."""
    content = path.read_text(encoding="utf-8")
    if YAML is None:
        document = _parse_workflow_without_dependency(content)
    else:
        document = YAML.load(content, Loader=YAML.BaseLoader)
    assert isinstance(document, dict)
    return document


def _workflow_text(path: Path = WORKFLOW) -> str:
    return path.read_text(encoding="utf-8")


def _jobs() -> dict[str, Any]:
    jobs = _load_workflow()["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _job(name: str) -> dict[str, Any]:
    job = _jobs()[name]
    assert isinstance(job, dict)
    return job


def _steps(job_name: str) -> list[dict[str, Any]]:
    steps = _job(job_name)["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _job_commands(job_name: str) -> str:
    return "\n".join(
        str(step.get("run", "")) for step in _steps(job_name) if "run" in step
    )


def _action_references(path: Path) -> list[str]:
    return re.findall(
        r"^\s*- uses: ([^\s]+)$", path.read_text(encoding="utf-8"), re.MULTILINE
    )


def test_workflow_is_manual_named_and_operation_gated() -> None:
    workflow = _load_workflow()
    triggers = workflow["on"]
    assert isinstance(triggers, dict)

    assert set(triggers) == {"workflow_dispatch"}
    assert (
        workflow["run-name"] == "Foundation ${{ inputs.operation }} · ${{ github.sha }}"
    )
    assert triggers["workflow_dispatch"]["inputs"]["operation"] == {
        "description": "Foundation operation to run",
        "required": "true",
        "type": "choice",
        "default": "foundation-plan",
        "options": ["foundation-plan", "foundation-apply"],
    }


def test_standard_library_fallback_parses_both_workflows() -> None:
    foundation = _parse_workflow_without_dependency(
        WORKFLOW.read_text(encoding="utf-8")
    )
    production = _parse_workflow_without_dependency(
        PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
    )

    assert foundation["jobs"]["foundation-plan"]["needs"] == "validate"
    assert production["concurrency"]["group"] == MUTATION_CONCURRENCY_GROUP


def test_validation_job_is_unprivileged_and_both_oidc_jobs_depend_on_it() -> None:
    validate = _job("validate")
    plan = _job("foundation-plan")
    apply = _job("foundation-apply")

    assert validate["permissions"] == {"contents": "read"}
    assert "environment" not in validate
    assert "id-token" not in validate["permissions"]
    assert "needs" not in validate
    assert plan["needs"] == "validate"
    assert apply["needs"] == "validate"
    assert plan["permissions"] == {"contents": "read", "id-token": "write"}
    assert apply["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }


def test_plan_and_apply_conditions_are_mutually_exclusive() -> None:
    assert _job("foundation-plan")["if"] == (
        "${{ inputs.operation == 'foundation-plan' }}"
    )
    assert _job("foundation-apply")["if"] == (
        "${{ inputs.operation == 'foundation-apply' }}"
    )


def test_validation_owns_main_head_quality_and_bicep_gates() -> None:
    validate = _job_commands("validate")
    plan = _job_commands("foundation-plan")
    apply = _job_commands("foundation-apply")
    pytest_command = "uv run --no-sync python -m pytest"

    assert 'test "$GITHUB_REF" = "refs/heads/main"' in validate
    assert 'test "$GITHUB_SHA" = "$(git rev-parse HEAD)"' in validate
    assert "+refs/heads/main:refs/remotes/origin/main" in validate
    assert 'test "$GITHUB_SHA" = "$(git rev-parse origin/main)"' in validate
    assert "foundation-plan)" in validate
    assert "foundation-apply)" in validate
    assert "APPLY-FOUNDATION" in validate
    assert "uv lock --check" in validate
    assert validate.index("uv sync --frozen --all-groups") < validate.index(
        pytest_command
    )
    assert "uv run pytest" not in validate
    assert "PYTHONPATH" not in validate
    assert "az bicep build" in validate
    assert "hackathon.foundation.bicepparam" in validate
    assert pytest_command not in plan
    assert pytest_command not in apply
    assert "az bicep build" not in plan
    assert "az bicep build" not in apply


def _bicep_validation_commands(path: Path) -> str:
    steps = _load_workflow(path)["jobs"]["validate"]["steps"]
    commands = [
        str(step["run"])
        for step in steps
        if "az bicep install" in str(step.get("run", ""))
    ]
    assert len(commands) == 1
    return commands[0]


@pytest.mark.parametrize("path", [WORKFLOW, PRODUCTION_WORKFLOW])
def test_bicep_formatting_compares_files_without_weakening_diff(path: Path) -> None:
    commands = _bicep_validation_commands(path)

    assert commands.startswith("set -euo pipefail\n")
    assert 'formatted_dir="$(mktemp -d)"' in commands
    assert "trap 'rm -rf \"$formatted_dir\"' EXIT" in commands
    comparison = (
        'az bicep format --file "$file" '
        '--outfile "$formatted_dir/$(basename "$file")"\n'
        '  diff --unified "$file" "$formatted_dir/$(basename "$file")"'
    )
    assert commands.count(comparison) == 2
    assert "<(az bicep format" not in commands
    assert "||" not in commands
    assert "set +e" not in commands
    assert "ignore" not in commands
    assert commands.count("diff ") == 2
    assert 'az bicep build --file "$file" --stdout >/dev/null' in commands
    assert 'az bicep build-params --file "$file" --stdout >/dev/null' in commands
    assert _load_workflow(path)["env"]["BICEP_VERSION"] == "0.46.1"


def test_bicep_validation_covers_all_templates_and_parameters() -> None:
    patterns = (
        "infra/main.bicep",
        "infra/resource-group.bicep",
        "infra/modules/*.bicep",
        "infra/environments/*.bicepparam",
    )
    covered = {path for pattern in patterns for path in ROOT.glob(pattern)}
    discovered = set((ROOT / "infra").rglob("*.bicep")) | set(
        (ROOT / "infra").rglob("*.bicepparam")
    )
    assert covered == discovered
    for workflow in (WORKFLOW, PRODUCTION_WORKFLOW):
        commands = _bicep_validation_commands(workflow)
        assert all(pattern in commands for pattern in patterns)


LIVE_BICEP = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("OPTIMA_TEST_BICEP_TOOLCHAIN") != "1",
    reason="requires opt-in Linux Bicep toolchain; no Azure login or paid calls",
)


def _record_block_allocations(commands: str) -> str:
    return (
        'mktemp() { command mktemp "$@" | tee -a "$OPTIMA_ALLOCATION_LOG"; }\n'
        + commands
    )


def _assert_block_cleanup(allocation_log: Path, scratch: Path) -> None:
    allocated = allocation_log.read_text(encoding="utf-8").splitlines()
    assert len(allocated) == 1, "Expected the block's one formatter directory"
    directory = Path(allocated[0])
    assert directory.parent.resolve() == scratch.resolve()
    assert not (directory.exists() or directory.is_symlink()), (
        f"Block-owned formatter directory leaked: {directory}"
    )


def _block_cleanup_commands(path: Path) -> str:
    commands = _bicep_validation_commands(path)
    start = commands.index('formatted_dir="$(mktemp -d)"')
    end = commands.index("for file in", start)
    return "set -euo pipefail\n" + commands[start:end]


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux Bash and mktemp")
@pytest.mark.parametrize("path", [WORKFLOW, PRODUCTION_WORKFLOW])
@pytest.mark.parametrize("cleanup", ["intact", "missing", "broken"])
@pytest.mark.parametrize("fail", [False, True])
def test_block_cleanup_tracks_ownership(
    tmp_path: Path, path: Path, cleanup: str, fail: bool
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    unrelated = scratch / "unrelated-tool-owned-file"
    unrelated.write_text("retained", encoding="utf-8")
    allocation_log = tmp_path / "allocations.txt"
    commands = _block_cleanup_commands(path)
    if cleanup == "missing":
        commands += "trap - EXIT\n"
    elif cleanup == "broken":
        commands += "trap ':' EXIT\n"
    commands += 'printf data > "$formatted_dir/result.bicep"\n'
    commands += "false\n" if fail else "true\n"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _record_block_allocations(commands)],
        env=os.environ
        | {"TMPDIR": str(scratch), "OPTIMA_ALLOCATION_LOG": str(allocation_log)},
        check=False,
    )
    assert result.returncode == (1 if fail else 0)
    assert unrelated.read_text(encoding="utf-8") == "retained"
    if cleanup != "intact":
        with pytest.raises(AssertionError, match="formatter directory leaked"):
            _assert_block_cleanup(allocation_log, scratch)
    else:
        _assert_block_cleanup(allocation_log, scratch)


@LIVE_BICEP
def test_live_bicep_telemetry_temp_ownership(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    allocation_log = tmp_path / "allocations.txt"
    cli_python = Path(os.environ["UV_TOOL_DIR"]) / "azure-cli" / "bin" / "python"
    script = textwrap.dedent(
        """\
        import datetime
        import importlib.metadata
        import inspect
        import json
        import os
        import subprocess
        import sys
        from pathlib import Path
        from unittest.mock import patch
        from azure.cli.telemetry.components import records_collection
        from azure.cli.telemetry.const import TELEMETRY_CACHE_DIR

        assert importlib.metadata.version('azure-cli') == '2.89.1'
        assert importlib.metadata.version('azure-cli-telemetry') == '1.1.0'
        root = Path(sys.argv[1])
        cache = root / TELEMETRY_CACHE_DIR
        cache.mkdir(parents=True)
        (cache / 'cache').write_text('2026-01-01T00:00:00,{}\\n')
        collection = records_collection.RecordsCollection(
            datetime.datetime.min, str(root))
        original = records_collection.tempfile.mkdtemp
        evidence = {}

        def observe_allocation():
            directory = Path(original())
            assert directory.is_dir()
            assert inspect.stack()[1].function == 'snapshot_and_read'
            subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', sys.argv[2]],
                           check=True)
            log = Path(os.environ['OPTIMA_ALLOCATION_LOG'])
            allocated = log.read_text().splitlines()
            assert len(allocated) == 1
            assert not Path(allocated[0]).exists()
            assert directory.is_dir()
            entries = sorted(item.name for item in directory.parent.iterdir())
            evidence.update(creator='RecordsCollection.snapshot_and_read',
                            tool_directory=str(directory), block_directory=allocated[0],
                            shared_entries=entries)
            return str(directory)

        with patch.object(records_collection.tempfile, 'mkdtemp', observe_allocation):
            collection.snapshot_and_read()
        assert not Path(evidence['tool_directory']).exists()
        print(json.dumps(evidence))
        """
    )
    result = subprocess.run(
        [
            str(cli_python),
            "-c",
            script,
            str(tmp_path / "telemetry"),
            _record_block_allocations(_block_cleanup_commands(WORKFLOW)),
        ],
        env=os.environ
        | {"TMPDIR": str(scratch), "OPTIMA_ALLOCATION_LOG": str(allocation_log)},
        capture_output=True,
        text=True,
        check=True,
    )
    evidence = json.loads(result.stdout)
    assert set(evidence["shared_entries"]) - {".bicep"}
    assert evidence["tool_directory"] != evidence["block_directory"]
    _assert_block_cleanup(allocation_log, scratch)
    print(f"Real telemetry allocation rejects old inventory assertion: {result.stdout}")


@LIVE_BICEP
def test_live_bicep_formatter_byte_provenance(tmp_path: Path) -> None:
    direct_bicep = Path(os.environ["AZURE_CONFIG_DIR"]) / "bin" / "bicep"
    version = subprocess.run(
        [str(direct_bicep), "--version"], capture_output=True, check=True
    ).stdout
    assert version.startswith(b"Bicep CLI version 0.46.1 ")
    azure_version = subprocess.run(
        ["az", "version"], capture_output=True, check=True
    ).stdout
    assert json.loads(azure_version)["azure-cli"] == "2.89.1"
    print(azure_version.decode("utf-8"))
    files = sorted((ROOT / "infra").rglob("*.bicep")) + sorted(
        (ROOT / "infra").rglob("*.bicepparam")
    )
    for source in files:
        committed = subprocess.run(
            ["git", "show", f"HEAD:{source.relative_to(ROOT).as_posix()}"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        direct = subprocess.run(
            [str(direct_bicep), "format", str(source), "--stdout"],
            capture_output=True,
            check=True,
        ).stdout
        wrapped = subprocess.run(
            ["az", "bicep", "format", "--file", str(source), "--stdout"],
            capture_output=True,
            check=True,
        ).stdout
        output = tmp_path / source.name
        subprocess.run(
            ["az", "bicep", "format", "--file", str(source), "--outfile", str(output)],
            check=True,
        )
        assert committed == source.read_bytes() == direct == output.read_bytes()
        assert wrapped == direct + b"\n"
        legacy = subprocess.run(
            [
                "bash",
                "-e",
                "-o",
                "pipefail",
                "-c",
                'diff --unified "$1" <(az bicep format --file "$1" --stdout)',
                "bicep-format-repro",
                str(source),
            ],
            capture_output=True,
            check=False,
        )
        assert legacy.returncode == 1
        print(
            json.dumps(
                {
                    "file": source.relative_to(ROOT).as_posix(),
                    "source_bytes": len(committed),
                    "direct_bytes": len(direct),
                    "wrapper_bytes": len(wrapped),
                    "source_direct_outfile_sha256": hashlib.sha256(
                        committed
                    ).hexdigest(),
                    "wrapper_sha256": hashlib.sha256(wrapped).hexdigest(),
                    "legacy_diff_exit": legacy.returncode,
                    "file_comparison": "PASS",
                },
                sort_keys=True,
            )
        )


@LIVE_BICEP
@pytest.mark.parametrize("path", [WORKFLOW, PRODUCTION_WORKFLOW])
def test_live_bicep_workflow_validation(path: Path, tmp_path: Path) -> None:
    shutil.copytree(ROOT / "infra", tmp_path / "infra")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = {
        source: source.read_bytes()
        for source in (tmp_path / "infra").rglob("*")
        if source.is_file()
    }
    allocation_log = tmp_path / "allocations.txt"
    environment = os.environ | {
        "BICEP_VERSION": "0.46.1",
        "TMPDIR": str(scratch),
        "OPTIMA_ALLOCATION_LOG": str(allocation_log),
    }
    subprocess.run(
        [
            "bash",
            "-e",
            "-o",
            "pipefail",
            "-c",
            _record_block_allocations(_bicep_validation_commands(path)),
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
    )
    assert all(source.read_bytes() == content for source, content in before.items())
    _assert_block_cleanup(allocation_log, scratch)
    print(f"Exact {path.name} Bicep validation block: PASS")


@LIVE_BICEP
@pytest.mark.parametrize("suffix", [".bicep", ".bicepparam"])
@pytest.mark.parametrize(
    "drift", ["indentation", "extra_blank_line", "missing_final_lf"]
)
def test_live_bicep_formatting_rejects_drift(
    tmp_path: Path, suffix: str, drift: str
) -> None:
    content = (
        "param name string = 'example'\n"
        if suffix == ".bicep"
        else "using none\n\nparam name = 'example'\n"
    )
    if drift == "indentation":
        content = content.replace("param name", "param  name")
    elif drift == "extra_blank_line":
        content += "\n"
    else:
        content = content[:-1]
    source = tmp_path / f"source{suffix}"
    output = tmp_path / f"formatted{suffix}"
    source.write_bytes(content.encode("utf-8"))
    subprocess.run(
        ["az", "bicep", "format", "--file", str(source), "--outfile", str(output)],
        check=True,
    )
    difference = subprocess.run(
        ["diff", "--unified", str(source), str(output)],
        capture_output=True,
        check=False,
    )
    assert difference.returncode == 1
    assert source.read_bytes() == content.encode("utf-8")


def test_each_oidc_job_reverifies_current_main_after_environment_gating() -> None:
    """Close the delay between validation and each protected environment job."""
    for job_name in ("foundation-plan", "foundation-apply"):
        steps = _steps(job_name)
        reverify = next(
            step
            for step in steps
            if step.get("name") == "Reverify the current protected main head"
        )
        commands = reverify["run"]
        assert 'test "$GITHUB_REF" = "refs/heads/main"' in commands
        assert 'test "$GITHUB_SHA" = "$(git rev-parse HEAD)"' in commands
        assert "+refs/heads/main:refs/remotes/origin/main" in commands
        assert 'test "$GITHUB_SHA" = "$(git rev-parse origin/main)"' in commands


def test_all_actions_are_pinned_to_full_commit_shas() -> None:
    references = [
        *_action_references(WORKFLOW),
        *_action_references(PRODUCTION_WORKFLOW),
    ]

    assert references
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference) for reference in references
    )


def test_plan_job_uses_distinct_read_only_identity_and_has_no_mutation_or_build() -> (
    None
):
    plan_job = _job("foundation-plan")
    plan = _job_commands("foundation-plan")
    login = next(
        step
        for step in _steps("foundation-plan")
        if str(step.get("uses", "")).startswith("azure/login@")
    )

    assert plan_job["environment"] == "hackathon"
    assert login["with"]["client-id"] == ("${{ vars.AZURE_FOUNDATION_PLAN_CLIENT_ID }}")
    assert "AZURE_FOUNDATION_PLAN_IDENTITY_RESOURCE_ID" in plan_job["env"]
    assert "AZURE_FOUNDATION_PLAN_ROLE_DEFINITION_ID" in plan_job["env"]
    assert "AZURE_CLIENT_ID" in plan_job["env"]
    assert "AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID" in plan_job["env"]
    assert "--phase foundation-plan" in plan
    assert plan.count("az deployment group what-if") == 1
    assert "az deployment group create" not in plan
    assert "az deployment sub create" not in plan
    assert "whatif_classification.py classify" in plan
    assert "hackathon.foundation.bicepparam" in plan
    assert "hackathon.runtime.bicepparam" not in plan
    assert "--validation-level ProviderNoRbac" in plan
    assert "--result-format FullResourcePayloads" in plan
    assert "--no-pretty-print" in plan
    for terminal_state in ("Succeeded", "Failed", "Canceled", "Deleted"):
        assert f"properties.provisioningState!='{terminal_state}'" in plan
    for token in APPLICATION_TOKENS:
        assert token not in plan


def test_plan_uploads_only_sanitized_classifier_evidence() -> None:
    upload = next(
        step
        for step in _steps("foundation-plan")
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )

    assert upload["with"]["name"] == "foundation-plan-evidence-${{ github.sha }}"
    assert upload["with"]["path"] == (
        "${{ runner.temp }}/foundation-plan-evidence.json"
    )
    assert upload["with"]["if-no-files-found"] == "error"


def test_failure_diagnostics_are_separate_and_failure_only() -> None:
    steps = _steps("foundation-plan")
    classify = next(step for step in steps if step.get("id") == "classify")
    uploads = [
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 3
    evidence, diagnostics, ciphertext = uploads
    assert "if" not in evidence
    assert "continue-on-error" not in classify
    assert classify["run"].startswith("set -euo pipefail\n")
    assert (
        '--failure-diagnostics "$RUNNER_TEMP/foundation-classification-failure.json"'
        in classify["run"]
    )
    assert (
        diagnostics["if"] == "${{ failure() && steps.classify.outcome == 'failure' }}"
    )
    assert diagnostics["with"] == {
        "name": (
            "foundation-classification-failure-"
            "${{ github.run_id }}-${{ github.run_attempt }}"
        ),
        "path": "${{ runner.temp }}/foundation-classification-failure.json",
        "if-no-files-found": "error",
        "retention-days": "7",
        "compression-level": "0",
    }
    assert evidence["with"]["path"] != diagnostics["with"]["path"]
    assert "foundation-whatif.json" not in diagnostics["with"]["path"]
    assert "--failure-diagnostics" not in _job_commands("foundation-apply")
    assert ciphertext["if"] == (
        "${{ failure() && steps.whatif.outputs.sealed == 'true' }}"
    )
    assert ciphertext["with"] == {
        "name": (
            "foundation-private-diagnostic-"
            "${{ github.run_id }}-${{ github.run_attempt }}"
        ),
        "path": "${{ runner.temp }}/foundation-private-capture.cms",
        "if-no-files-found": "error",
        "retention-days": "1",
        "compression-level": "0",
    }
    assert "continue-on-error" not in ciphertext
    assert "foundation_diagnostic_capture" not in _job_commands("foundation-apply")


def test_recipient_preparation_precedes_login_and_uses_only_public_variables() -> None:
    steps = _steps("foundation-plan")
    preparation = _capture_step(
        "Validate owner-only diagnostic encryption before Azure login"
    )
    login = next(
        step for step in steps if str(step.get("uses", "")).startswith("azure/login@")
    )
    assert steps.index(preparation) < steps.index(login)
    assert preparation["id"] == "recipient"
    assert preparation["run"].startswith("set -euo pipefail\numask 077\n")
    assert (
        "python scripts/foundation_diagnostic_capture.py prepare "
        '--directory "$RUNNER_TEMP"'
    ) in preparation["run"]
    assert (
        'mktemp -d "$RUNNER_TEMP/foundation-private.XXXXXXXXXX"' in preparation["run"]
    )
    for name in (
        "OPTIMA_DIAGNOSTIC_CERTIFICATE_BASE64",
        "OPTIMA_DIAGNOSTIC_CERTIFICATE_SHA256",
    ):
        assert _job("foundation-plan")["env"][name] == "${{ vars." + name + " }}"
        assert name not in _job("foundation-apply")["env"]
    assert _job("foundation-plan")["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }


@pytest.fixture
def capture_bash() -> str:
    if sys.platform == "win32":
        git = shutil.which("git")
        candidate = Path(git).parent.parent / "bin" / "bash.exe" if git else None
        if candidate is not None and candidate.is_file():
            return str(candidate)
        pytest.skip("requires existing Git Bash; never launch WSL")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("requires Bash")
    return bash


def _capture_step(name: str) -> dict[str, Any]:
    return next(step for step in _steps("foundation-plan") if step.get("name") == name)


def _run_capture_block(
    bash: str, commands: str, tmp_path: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    fake_commands = textwrap.dedent(
        """\
        az() {
                    printf '%s\n' "$*" >> "$CAPTURE_AZ_CALLS"
          case "$*" in
            'group show '*) return 0 ;;
            'deployment group list '*) return 0 ;;
            'deployment group what-if '*)
              printf '%s\n' "$@" >> "$CAPTURE_AZ_ARGUMENTS"
              umask > "$CAPTURE_UMASK"
              printf '%s' "$RAW_STDOUT_SENTINEL"
              printf '%s' "$RAW_STDERR_SENTINEL" >&2
              return "$WHATIF_EXIT"
              ;;
            *) return 99 ;;
          esac
        }
        python() {
                    if test "$1" = scripts/foundation_diagnostic_capture.py; then
                        if test "$2" = prepare; then return "${PREPARE_EXIT:-0}"; fi
                        test "$2" = seal || return 91
                        test "$4" = "$RUNNER_TEMP" || return 92
                        test "$6" = "$WHATIF_EXIT" || return 93
                        raw_stdout="$(< "$RUNNER_TEMP/foundation-whatif.json")"
                        raw_stderr="$(< "$RUNNER_TEMP/foundation-whatif.stderr")"
                        test "$raw_stdout" = "$RAW_STDOUT_SENTINEL" || return 94
                        test "$raw_stderr" = "$RAW_STDERR_SENTINEL" || return 95
                        if test "${SEAL_EXIT:-0}" -ne 0; then return "$SEAL_EXIT"; fi
                        printf 'SYNTHETIC_CIPHERTEXT' \
                            > "$RUNNER_TEMP/foundation-private-capture.cms"
                        return 0
                    fi
          test "$1" = scripts/whatif_classification.py
          test "$2" = classify
          printf '%s\n' "$@" > "$CAPTURE_CLASSIFIER_ARGUMENTS"
          test "$(< "$RUNNER_TEMP/foundation-whatif.json")" = "$RAW_STDOUT_SENTINEL"
          test ! -e "$RUNNER_TEMP/foundation-whatif.stderr"
          umask > "$CAPTURE_CLASSIFIER_UMASK"
          case "$CLASSIFIER_MODE" in
            success)
              printf '{"classification":"APPROVED"}\n' \
                > "$RUNNER_TEMP/foundation-plan-evidence.json"
              ;;
            failure)
              printf '{"classification":"FAILED","promotable":false}\n' \
                > "$RUNNER_TEMP/foundation-classification-failure.json"
              ;;
            write_failure) return 73 ;;
            unavailable) return 127 ;;
            *) return 99 ;;
          esac
          return "$CLASSIFIER_EXIT"
        }
        """
    )
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail"],
        input=(fake_commands + commands).encode("utf-8"),
        cwd=tmp_path,
        env=os.environ | environment,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    (
        "whatif_exit",
        "classifier_mode",
        "classifier_exit",
        "seal_exit",
        "ciphertext_collision",
    ),
    [
        (0, "success", 0, 0, False),
        (0, "failure", 1, 0, False),
        (0, "write_failure", 73, 0, False),
        (0, "unavailable", 127, 0, False),
        (7, "success", 0, 0, False),
        (23, "success", 0, 0, False),
        (130, "success", 0, 0, False),
        (143, "success", 0, 0, False),
        (0, "success", 0, 1, False),
        (23, "success", 0, 1, False),
        pytest.param(0, "success", 0, 0, True, id="ciphertext-collision"),
    ],
)
def test_plan_capture_blocks_contain_raw_streams_and_preserve_status(
    tmp_path: Path,
    capture_bash: str,
    whatif_exit: int,
    classifier_mode: str,
    classifier_exit: int,
    seal_exit: int,
    ciphertext_collision: bool,
) -> None:
    scratch = tmp_path / "runner temp"
    scratch.mkdir()
    environment = {
        "RUNNER_TEMP": scratch.as_posix(),
        "GITHUB_STEP_SUMMARY": (tmp_path / "summary").as_posix(),
        "GITHUB_OUTPUT": (tmp_path / "outputs").as_posix(),
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "AZURE_RESOURCE_GROUP": "rg-optima-hackathon",
        "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
        "RAW_STDOUT_SENTINEL": "PRIVATE_STDOUT_SENTINEL\nraw-resource-secret",
        "RAW_STDERR_SENTINEL": "PRIVATE_STDERR_SENTINEL\nraw-provider-secret",
        "CAPTURE_AZ_CALLS": (tmp_path / "az-calls").as_posix(),
        "CAPTURE_AZ_ARGUMENTS": (tmp_path / "az-arguments").as_posix(),
        "CAPTURE_CLASSIFIER_ARGUMENTS": (tmp_path / "classifier-arguments").as_posix(),
        "CAPTURE_UMASK": (tmp_path / "whatif-umask").as_posix(),
        "CAPTURE_CLASSIFIER_UMASK": (tmp_path / "classifier-umask").as_posix(),
        "WHATIF_EXIT": str(whatif_exit),
        "SEAL_EXIT": str(seal_exit),
        "CLASSIFIER_MODE": classifier_mode,
        "CLASSIFIER_EXIT": str(classifier_exit),
    }
    ciphertext = scratch / "foundation-private-capture.cms"
    if ciphertext_collision:
        ciphertext.write_text(environment["RAW_STDOUT_SENTINEL"], encoding="utf-8")
    capture = _capture_step("Run exactly one authoritative foundation what-if")
    result = _run_capture_block(capture_bash, capture["run"], tmp_path, environment)
    if ciphertext_collision:
        assert result.returncode == 1
        assert result.stdout == result.stderr == b""
        assert not (tmp_path / "az-calls").exists()
        assert not (tmp_path / "az-arguments").exists()
        assert not (tmp_path / "classifier-arguments").exists()
        assert not (tmp_path / "outputs").exists()
        assert not (tmp_path / "summary").exists()
        assert list(scratch.iterdir()) == [ciphertext]
        assert (
            ciphertext.read_text(encoding="utf-8") == environment["RAW_STDOUT_SENTINEL"]
        )
        upload = _capture_step(
            "Retain owner-encrypted diagnostics for a failed plan only"
        )
        assert upload["if"] == (
            "${{ failure() && steps.whatif.outputs.sealed == 'true' }}"
        )
        return
    assert result.returncode == (whatif_exit or seal_exit)
    assert result.stdout == result.stderr == b""
    assert (tmp_path / "az-arguments").read_text(encoding="utf-8").splitlines() == [
        "deployment",
        "group",
        "what-if",
        "--name",
        "optima-foundation-promotion-whatif",
        "--mode",
        "Incremental",
        "--resource-group",
        "rg-optima-hackathon",
        "--template-file",
        "infra/resource-group.bicep",
        "--parameters",
        "infra/environments/hackathon.foundation.bicepparam",
        "--parameters",
        f"deploymentCommitSha={'a' * 40}",
        "deploymentWorkflowRunId=123-2",
        "--validation-level",
        "ProviderNoRbac",
        "--result-format",
        "FullResourcePayloads",
        "--no-pretty-print",
        "--only-show-errors",
    ]
    assert (tmp_path / "whatif-umask").read_text().strip() == "0077"
    raw = scratch / "foundation-whatif.json"
    assert not (scratch / "foundation-whatif.stderr").exists()
    expected_files: set[str] = set()
    if whatif_exit == 0 and seal_exit == 0:
        assert raw.read_text(encoding="utf-8") == environment["RAW_STDOUT_SENTINEL"]
        if sys.platform != "win32":
            assert raw.stat().st_mode & 0o777 == 0o600
        classify = _capture_step(
            "Classify the foundation what-if and emit sanitized evidence"
        )
        result = _run_capture_block(
            capture_bash, classify["run"], tmp_path, environment
        )
        assert result.returncode == classifier_exit
        assert result.stdout == result.stderr == b""
        assert (tmp_path / "classifier-umask").read_text().strip() == "0077"
        arguments = (tmp_path / "classifier-arguments").read_text().splitlines()
        assert arguments[arguments.index("--whatif") + 1] == raw.as_posix()
        if classifier_mode == "success":
            expected_files = {"foundation-plan-evidence.json"}
            assert "APPROVED" in (tmp_path / "summary").read_text()
        elif classifier_mode == "failure":
            expected_files = {"foundation-classification-failure.json"}
            assert json.loads(
                (scratch / "foundation-classification-failure.json").read_text()
            ) == {"classification": "FAILED", "promotable": False}
    assert not raw.exists()
    if seal_exit == 0:
        expected_files.add("foundation-private-capture.cms")
        assert (tmp_path / "outputs").read_text().strip() == "sealed=true"
    else:
        assert not (tmp_path / "outputs").exists()
    assert {entry.name for entry in scratch.iterdir()} == expected_files
    if whatif_exit or classifier_exit or seal_exit:
        assert not (tmp_path / "summary").exists()
        assert not (scratch / "foundation-plan-evidence.json").exists()
    for artifact in scratch.iterdir():
        content = artifact.read_text(encoding="utf-8")
        assert "PRIVATE_STDOUT_SENTINEL" not in content
        assert "PRIVATE_STDERR_SENTINEL" not in content
    cleanup = _capture_step("Clean up owner-encrypted diagnostic ciphertext")
    assert cleanup["if"] == "${{ always() }}"
    result = _run_capture_block(capture_bash, cleanup["run"], tmp_path, environment)
    assert result.returncode == 0
    assert not (scratch / "foundation-private-capture.cms").exists()


@pytest.mark.parametrize("prepare_exit", [0, 1])
def test_recipient_prepare_trap_and_cleanup_own_only_allocated_scratch(
    tmp_path: Path, capture_bash: str, prepare_exit: int
) -> None:
    runner_temp = tmp_path / "runner temp"
    runner_temp.mkdir()
    unrelated = runner_temp / "foundation-private.unrelated"
    unrelated.mkdir()
    retained = unrelated / "retained.txt"
    retained.write_text("unrelated", encoding="utf-8")
    outputs = tmp_path / "outputs"
    environment = {
        "RUNNER_TEMP": runner_temp.as_posix(),
        "GITHUB_OUTPUT": outputs.as_posix(),
        "PREPARE_EXIT": str(prepare_exit),
    }
    prepare = _capture_step(
        "Validate owner-only diagnostic encryption before Azure login"
    )
    result = _run_capture_block(capture_bash, prepare["run"], tmp_path, environment)
    assert result.returncode == prepare_exit
    assert result.stdout == result.stderr == b""
    output_lines = outputs.read_text(encoding="utf-8").splitlines()
    assert len(output_lines) == 1
    key, separator, value = output_lines[0].partition("=")
    assert (key, separator) == ("scratch", "=")
    owned = Path(value)
    assert owned.parent == runner_temp
    assert re.fullmatch(r"foundation-private\.[A-Za-z0-9]{10}", owned.name)
    assert owned != unrelated
    if prepare_exit:
        assert not owned.exists()
    else:
        assert owned.is_dir()
        if sys.platform == "linux":
            assert owned.stat().st_mode & 0o777 == 0o700
        (owned / "plaintext.tar").write_text("synthetic plaintext", encoding="utf-8")
    assert retained.read_text(encoding="utf-8") == "unrelated"
    cleanup = _capture_step("Clean up private foundation what-if capture")
    assert cleanup["if"] == "${{ always() }}"
    result = _run_capture_block(
        capture_bash,
        cleanup["run"],
        tmp_path,
        environment | {"CAPTURE_SCRATCH": owned.as_posix()},
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    assert not owned.exists()
    assert list(runner_temp.iterdir()) == [unrelated]
    assert list(unrelated.iterdir()) == [retained]
    assert retained.read_text(encoding="utf-8") == "unrelated"


@pytest.mark.parametrize(
    ("invalid_field", "expected_exit"),
    [
        ("none", 0),
        ("command", 91),
        ("directory", 92),
        ("status", 93),
        ("stdout", 94),
        ("stderr", 95),
    ],
)
def test_fake_seal_assertions_fail_even_in_if_context(
    tmp_path: Path, capture_bash: str, invalid_field: str, expected_exit: int
) -> None:
    environment = {
        "RUNNER_TEMP": tmp_path.as_posix(),
        "WHATIF_EXIT": "23",
        "SEAL_EXIT": "0",
        "RAW_STDOUT_SENTINEL": "PRIVATE_STDOUT_SENTINEL",
        "RAW_STDERR_SENTINEL": "PRIVATE_STDERR_SENTINEL",
        "SEAL_COMMAND": "invalid" if invalid_field == "command" else "seal",
        "SEAL_DIRECTORY": "invalid"
        if invalid_field == "directory"
        else tmp_path.as_posix(),
        "SEAL_STATUS": "0" if invalid_field == "status" else "23",
    }
    for stream, filename in (
        ("stdout", "foundation-whatif.json"),
        ("stderr", "foundation-whatif.stderr"),
    ):
        content = (
            "invalid"
            if invalid_field == stream
            else environment[f"RAW_{stream.upper()}_SENTINEL"]
        )
        (tmp_path / filename).write_text(content, encoding="utf-8")
    result = _run_capture_block(
        capture_bash,
        "set -euo pipefail\n"
        'if python scripts/foundation_diagnostic_capture.py "$SEAL_COMMAND" '
        '--directory "$SEAL_DIRECTORY" --whatif-exit-code "$SEAL_STATUS"; then\n'
        "  exit 0\n"
        "else\n"
        '  exit "$?"\n'
        "fi\n",
        tmp_path,
        environment,
    )
    assert result.returncode == expected_exit
    assert result.stdout == result.stderr == b""
    ciphertext = tmp_path / "foundation-private-capture.cms"
    if expected_exit:
        assert not ciphertext.exists()
    else:
        assert ciphertext.read_text(encoding="utf-8") == "SYNTHETIC_CIPHERTEXT"


def test_plan_capture_cleanup_rejects_unrelated_scratch_path(
    tmp_path: Path, capture_bash: str
) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    retained = unrelated / "retained.txt"
    retained.write_text("unrelated", encoding="utf-8")
    cleanup = _capture_step("Clean up private foundation what-if capture")
    result = _run_capture_block(
        capture_bash,
        cleanup["run"],
        tmp_path,
        {"RUNNER_TEMP": tmp_path.as_posix(), "CAPTURE_SCRATCH": unrelated.as_posix()},
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"Private capture cleanup path rejected\n"
    assert list(tmp_path.iterdir()) == [unrelated]
    assert list(unrelated.iterdir()) == [retained]
    assert retained.read_text(encoding="utf-8") == "unrelated"


@pytest.mark.parametrize(
    "interruption", ["before_capture", "partial_capture", "between_steps"]
)
def test_plan_capture_cancellation_cleanup_owns_only_exact_raw_paths(
    tmp_path: Path, capture_bash: str, interruption: str
) -> None:
    cleanup = _capture_step("Clean up private foundation what-if capture")
    assert cleanup["if"] == "${{ always() }}"
    assert cleanup["env"] == {
        "CAPTURE_SCRATCH": "${{ steps.recipient.outputs.scratch }}"
    }
    assert cleanup["run"].startswith(
        "set -euo pipefail\n"
        'rm -f -- "$RUNNER_TEMP/foundation-whatif.json" \\\n'
        '  "$RUNNER_TEMP/foundation-whatif.stderr"'
    )
    owned = tmp_path / "foundation-private.owned"
    owned.mkdir()
    (owned / "plaintext.tar").write_text("private temporary bundle")
    retained = {
        "foundation-classification-failure.json",
        "foundation-whatif.json.unrelated",
        "foundation-whatif.stderr.unrelated",
    }
    for filename in retained:
        (tmp_path / filename).write_text("retained", encoding="utf-8")
    if interruption != "before_capture":
        (tmp_path / "foundation-whatif.json").write_text("private stdout")
    if interruption == "partial_capture":
        (tmp_path / "foundation-whatif.stderr").write_text("private stderr")
    result = _run_capture_block(
        capture_bash,
        cleanup["run"],
        tmp_path,
        {"RUNNER_TEMP": tmp_path.as_posix(), "CAPTURE_SCRATCH": owned.as_posix()},
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    assert {entry.name for entry in tmp_path.iterdir()} == retained
    assert all((tmp_path / filename).read_text() == "retained" for filename in retained)


def test_apply_authenticates_provenance_before_exact_artifact_download() -> None:
    steps = _steps("foundation-apply")
    commands = _job_commands("foundation-apply")
    provenance_index = next(
        index for index, step in enumerate(steps) if step.get("id") == "provenance"
    )
    download_index = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
    )
    download = steps[download_index]

    assert provenance_index < download_index
    assert "foundation_promotion.py" in commands
    assert "/actions/runs/$PLAN_RUN_ID" in commands
    assert "/jobs?filter=all&per_page=100" in commands
    assert "/artifacts?per_page=100" in commands
    assert "/actions/workflows/foundation.yml/runs?per_page=100" in commands
    assert "--maximum-age-seconds 86400" in commands
    assert "--fail-with-body" not in commands
    assert "--verbose" not in commands
    assert download["with"]["artifact-ids"] == (
        "${{ steps.provenance.outputs.artifact_id }}"
    )
    assert "name" not in download["with"]
    assert download["with"]["run-id"] == "${{ inputs.plan_run_id }}"
    assert "mapfile -d '' entries" in commands
    assert "test ! -L" in commands
    assert "jq -r" not in commands


def test_apply_reclassifies_exact_plan_before_one_foundation_create() -> None:
    apply_job = _job("foundation-apply")
    apply = _job_commands("foundation-apply")

    assert "AZURE_FOUNDATION_PLAN_CLIENT_ID" in apply_job["env"]
    assert "AZURE_FOUNDATION_PLAN_IDENTITY_RESOURCE_ID" in apply_job["env"]
    assert "--phase foundation-apply" in apply
    assert apply.count("az deployment group create") == 1
    assert apply.count("whatif_classification.py classify") == 2
    assert "whatif_classification.py promote-check" in apply
    assert "hackathon.foundation.bicepparam" in apply
    assert "hackathon.runtime.bicepparam" not in apply
    assert "optima-foundation-promotion-whatif" in apply
    assert "--validation-level ProviderNoRbac" in apply
    assert "--result-format FullResourcePayloads" in apply
    assert "deploymentWorkflowRunId=$PLAN_RUN_ID-$SOURCE_RUN_ATTEMPT" in apply
    for terminal_state in ("Succeeded", "Failed", "Canceled", "Deleted"):
        assert f"properties.provisioningState!='{terminal_state}'" in apply
    for token in APPLICATION_TOKENS:
        assert token not in apply


def test_apply_records_and_reconciles_exact_deployment_before_convergence() -> None:
    steps = _steps("foundation-apply")
    deploy = next(step for step in steps if step.get("id") == "deploy")
    reconcile = next(
        step
        for step in steps
        if step.get("name") == "Reconcile the exact foundation deployment"
    )
    upload = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )

    assert (
        'deployment_name="optima-foundation-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"'
        in deploy["run"]
    )
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in deploy["run"]
    assert 'test "$GITHUB_SHA" = "$(git rev-parse HEAD)"' in deploy["run"]
    assert "+refs/heads/main:refs/remotes/origin/main" in deploy["run"]
    assert deploy["run"].rindex(
        'test "$GITHUB_SHA" = "$(git rev-parse origin/main)"'
    ) < deploy["run"].index("az deployment group create")
    assert "launch_began" not in deploy["run"]
    assert reconcile["if"] == "${{ always() && steps.deploy.outcome != 'skipped' }}"
    assert reconcile["env"]["DEPLOYMENT_NAME"] == (
        "optima-foundation-${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert "az deployment group show" in reconcile["run"]
    assert "az deployment group cancel" in reconcile["run"]
    assert "Accepted | Running" in reconcile["run"]
    assert 'test "$DEPLOY_OUTCOME" = "success"' in reconcile["run"]
    assert 'test "$state" = "Succeeded"' in reconcile["run"]
    assert "foundation-reconciliation.json" in upload["with"]["path"]
    assert upload["if"] == "always()"


def test_foundation_and_production_share_one_non_canceling_mutation_group() -> None:
    foundation = _load_workflow()
    production = _load_workflow(PRODUCTION_WORKFLOW)

    assert foundation["concurrency"] == {
        "group": MUTATION_CONCURRENCY_GROUP,
        "cancel-in-progress": "false",
    }
    assert production["concurrency"] == foundation["concurrency"]


def test_production_rejects_active_foundation_deployments_at_both_scopes() -> None:
    """Block orphaned group or subscription foundation work before mutation."""
    steps = _load_workflow(PRODUCTION_WORKFLOW)["jobs"]["deploy"]["steps"]
    guard_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name")
        == "Reject interrupted foundation deployments before mutation"
    )
    foundation_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "What-if and converge the Azure foundation"
    )
    guard = steps[guard_index]["run"]

    assert guard_index < foundation_index
    assert "az deployment sub list" in guard
    assert "az deployment group list" in guard
    assert "az group exists" in guard
    assert 'case "$group_exists" in' in guard
    assert "true)" in guard
    assert "false) ;;" in guard
    assert "Resource-group existence returned invalid evidence" in guard
    assert "az group show" not in guard
    for terminal_state in ("Succeeded", "Failed", "Canceled", "Deleted"):
        assert f"properties.provisioningState!='{terminal_state}'" in guard
    assert 'test -n "$subscription_active" || test -n "$group_active"' in guard

    foundation = steps[foundation_index]["run"]
    assert foundation.count("az group exists") == 1
    assert foundation.count("az deployment sub list") == 1
    assert foundation.count("az deployment group list") == 1
    assert 'case "$group_exists" in' in foundation
    assert 'test -z "$subscription_active"' in foundation
    assert 'test -z "$group_active"' in foundation
    assert 'if test "$group_exists" = "true"; then' in foundation
    assert "az group show" not in foundation


def test_no_run_block_interpolates_untrusted_inputs_or_secrets() -> None:
    for path in (WORKFLOW, PRODUCTION_WORKFLOW):
        workflow = _load_workflow(path)
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                command = step.get("run")
                if command is None:
                    continue
                assert "${{ inputs." not in command
                assert "${{ secrets." not in command
