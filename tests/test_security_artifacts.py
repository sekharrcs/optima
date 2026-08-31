"""Tests for reproducible pre-deployment security evidence."""

import base64
import csv
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from scripts.generate_sbom import generate_sbom
from scripts.prune_runtime_artifacts import PruneError, prune_environment

ROOT = Path(__file__).resolve().parents[1]
PR_SECURITY_WORKFLOW = ROOT / ".github" / "workflows" / "pr-security-containers.yml"
PRUNE_SCRIPT = ROOT / "scripts" / "prune_runtime_artifacts.py"
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


def _named_run_block(content: str, step_name: str) -> str:
    """Return the literal run block belonging to one named workflow step."""
    lines = content.splitlines()
    name_index = lines.index(f"      - name: {step_name}")
    run_index = next(
        index
        for index in range(name_index + 1, len(lines))
        if re.match(r"^\s+run:\s*\|-?\s*$", lines[index]) is not None
    )
    parent_indentation = len(lines[run_index]) - len(lines[run_index].lstrip())
    raw_block: list[str] = []
    for line in lines[run_index + 1 :]:
        indentation = len(line) - len(line.lstrip())
        if line.strip() and indentation <= parent_indentation:
            break
        raw_block.append(line)
    content_indentation = min(
        len(line) - len(line.lstrip()) for line in raw_block if line.strip()
    )
    return "\n".join(
        line[content_indentation:] if line.strip() else "" for line in raw_block
    )


def _locked_packages() -> dict[str, dict[str, Any]]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {package["name"]: package for package in lock["package"]}


def _dependency_names(package: dict[str, Any], key: str) -> set[str]:
    dependencies = package.get(key, [])
    if not isinstance(dependencies, list):
        raise AssertionError(f"{key} must be a dependency list")
    return {dependency["name"] for dependency in dependencies}


def _write_distribution(
    environment_root: Path,
    distribution: str,
    files: dict[str, bytes],
    *,
    metadata_suffix: bytes = b"",
    package_version: str = "1.0.0",
    library_name: str = "lib",
) -> Path:
    site_packages = environment_root / library_name / "python3.12" / "site-packages"
    info_directory = site_packages / f"{distribution}-{package_version}.dist-info"
    info_directory.mkdir(parents=True)
    metadata = (
        f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {package_version}\n"
    ).encode() + metadata_suffix
    wheel = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    installed = {
        f"{distribution}-{package_version}.dist-info/METADATA": metadata,
        f"{distribution}-{package_version}.dist-info/WHEEL": wheel,
        **files,
    }
    for relative_path, content in installed.items():
        target = site_packages / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    record_path = info_directory / "RECORD"
    rows = []
    for relative_path, content in sorted(installed.items()):
        digest = base64.urlsafe_b64encode(sha256(content).digest()).rstrip(b"=")
        rows.append(f"{relative_path},sha256={digest.decode()},{len(content)}\n")
    record_path.write_text(
        "".join(rows) + f"{distribution}-{package_version}.dist-info/RECORD,,\n",
        encoding="utf-8",
        newline="",
    )
    return site_packages


def _run_pruner(
    environment_root: Path,
    *,
    verify_only: bool = False,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        sys.executable,
        str(PRUNE_SCRIPT),
        "--environment-root",
        str(environment_root),
    ]
    if verify_only:
        arguments.append("--verify-only")
    else:
        arguments.append("--disposable-build-environment")
    return subprocess.run(
        arguments,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _record_path(site_packages: Path, distribution: str) -> Path:
    return site_packages / f"{distribution}-1.0.0.dist-info" / "RECORD"


def test_runtime_pruner_removes_owned_tests_and_derived_bytecode(
    tmp_path: Path,
) -> None:
    """Remove an owned test source, its bytecode, and its empty cache only."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {
            "numpy/conftest.py": b"collect_ignore = []\n",
            "numpy/runtime.py": b"VALUE = 1\n",
            "numpy/test_attributes.py": b"PUBLIC = True\n",
            "numpy/tests/test_policy.py": b"assert True\n",
        },
    )
    cache_directory = site_packages / "numpy" / "tests" / "__pycache__"
    cache_directory.mkdir()
    bytecode = cache_directory / "test_policy.cpython-312.pyc"
    bytecode.write_bytes(b"derived bytecode")
    empty_conftest_cache = site_packages / "numpy" / "__pycache__"
    empty_conftest_cache.mkdir()

    result = _run_pruner(environment_root)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (site_packages / "numpy" / "tests" / "test_policy.py").exists()
    assert not bytecode.exists()
    assert not cache_directory.exists()
    assert not empty_conftest_cache.exists()
    assert not (site_packages / "numpy" / "conftest.py").exists()
    assert (site_packages / "numpy" / "runtime.py").is_file()
    assert (site_packages / "numpy" / "test_attributes.py").is_file()


def test_runtime_pruner_removes_exact_certifi_executable_test_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove the exact executable test shipped by the certifi 2026.7.22 wheel."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "certifi",
        {
            "certifi/__init__.py": b"from .core import where\n",
            "certifi/cacert.pem": b"runtime trust bundle\n",
            "certifi/core.py": b"def where(): return 'cacert.pem'\n",
            "certifi/tests/__init__.py": b"",
            "certifi/tests/test_certify.py": b"def test_bundle(): assert True\n",
        },
        package_version="2026.7.22",
    )
    test_source = site_packages / "certifi" / "tests" / "test_certify.py"
    record_path = site_packages / "certifi-2026.7.22.dist-info" / "RECORD"
    with record_path.open(encoding="utf-8", newline="") as stream:
        test_rows = {
            row[0]: row[1:]
            for row in csv.reader(stream)
            if row[0].startswith("certifi/tests/")
        }
    assert set(test_rows) == {
        "certifi/tests/__init__.py",
        "certifi/tests/test_certify.py",
    }
    assert all(
        hash_field.startswith("sha256=") and size_field.isdecimal()
        for hash_field, size_field in test_rows.values()
    )
    original_stat = Path.stat

    def wheel_stat(
        path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if path != test_source:
            return result
        values = list(result)
        values[stat.ST_MODE] = int(values[stat.ST_MODE]) | (
            stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", wheel_stat)

    result = prune_environment(environment_root)

    assert result["files_removed"] == 2
    assert not (site_packages / "certifi" / "tests").exists()
    assert (site_packages / "certifi" / "core.py").is_file()
    assert (site_packages / "certifi" / "cacert.pem").is_file()
    record = record_path.read_text(encoding="utf-8")
    assert "certifi/tests/" not in record
    assert "certifi/core.py,sha256=" in record
    assert "certifi/cacert.pem,sha256=" in record


@pytest.mark.parametrize(
    ("package_version", "relative_path"),
    [
        ("2026.7.23", "certifi/tests/test_certify.py"),
        ("2026.7.22", "certifi/tests/test_other.py"),
    ],
)
def test_runtime_pruner_rejects_other_executable_certifi_test_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package_version: str,
    relative_path: str,
) -> None:
    """Keep the certifi executable exception pinned to one version and path."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "certifi",
        {relative_path: b"def test_bundle(): assert True\n"},
        package_version=package_version,
    )
    test_source = site_packages / relative_path
    original_stat = Path.stat

    def wheel_stat(
        path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if path != test_source:
            return result
        values = list(result)
        values[stat.ST_MODE] = int(values[stat.ST_MODE]) | stat.S_IXUSR
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", wheel_stat)

    with pytest.raises(PruneError, match="unsafe file class"):
        prune_environment(environment_root)

    assert test_source.is_file()


@pytest.mark.parametrize(
    "bytecode_name",
    [
        "test_policy.cpython-311.pyc",
        "test_policy.cpython-999.pyc",
        "test_policy.cpython-312.opt-1.pyc",
        "forged.cpython-312.pyc",
    ],
)
def test_runtime_pruner_rejects_unvalidated_derived_bytecode(
    tmp_path: Path,
    bytecode_name: str,
) -> None:
    """Reject wrong-tag, optimized, and forged generated bytecode before mutation."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {"numpy/tests/test_policy.py": b"assert True\n"},
    )
    cache_directory = site_packages / "numpy" / "tests" / "__pycache__"
    cache_directory.mkdir()
    bytecode = cache_directory / bytecode_name
    bytecode.write_bytes(b"untrusted bytecode")

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert "bytecode" in (result.stdout + result.stderr).casefold()
    assert bytecode.is_file()
    assert (site_packages / "numpy" / "tests" / "test_policy.py").is_file()


def test_runtime_pruner_rejects_record_owned_bytecode_from_another_owner(
    tmp_path: Path,
) -> None:
    """Do not treat bytecode listed by another wheel as generated content."""
    environment_root = tmp_path / ".venv"
    bytecode_path = "numpy/tests/__pycache__/test_policy.cpython-312.pyc"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {"numpy/tests/test_policy.py": b"assert True\n"},
    )
    _write_distribution(
        environment_root,
        "certifi",
        {bytecode_path: b"foreign-owned bytecode"},
    )

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert "bytecode" in (result.stdout + result.stderr).casefold()
    assert (site_packages / bytecode_path).is_file()


def test_runtime_pruner_rejects_bytecode_owned_by_optima(tmp_path: Path) -> None:
    """Do not delete derived bytecode when OPTIMA owns its RECORD row."""
    environment_root = tmp_path / ".venv"
    bytecode_path = "numpy/tests/__pycache__/test_policy.cpython-312.pyc"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {"numpy/tests/test_policy.py": b"assert True\n"},
    )
    _write_distribution(
        environment_root,
        "optima",
        {bytecode_path: b"optima-owned bytecode"},
    )

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert (
        "optima distribution artifact is never pruned"
        in (result.stdout + result.stderr).casefold()
    )
    assert (site_packages / bytecode_path).is_file()
    assert (site_packages / "numpy" / "tests" / "test_policy.py").is_file()


def test_runtime_pruner_accepts_same_owner_record_bytecode(tmp_path: Path) -> None:
    """Remove exact active-tag bytecode when its source and RECORD owner agree."""
    environment_root = tmp_path / ".venv"
    bytecode_path = "numpy/tests/__pycache__/test_policy.cpython-312.pyc"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {
            "numpy/tests/test_policy.py": b"assert True\n",
            bytecode_path: b"owned bytecode",
        },
    )

    result = _run_pruner(environment_root)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (site_packages / bytecode_path).exists()
    assert (
        bytecode_path.encode() not in _record_path(site_packages, "numpy").read_bytes()
    )


def test_runtime_pruner_rejects_record_owned_orphan_bytecode(tmp_path: Path) -> None:
    """Reject an active-tag RECORD row without its exact removable source."""
    environment_root = tmp_path / ".venv"
    bytecode_path = "numpy/tests/__pycache__/test_orphan.cpython-312.pyc"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {bytecode_path: b"orphan bytecode"},
    )

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert (
        "lacks a validated removable source"
        in (result.stdout + result.stderr).casefold()
    )
    assert (site_packages / bytecode_path).is_file()


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "numpy/tests/native.so",
        "numpy/tests/.libs/native.so",
        "numpy/tests/licenses/NOTICE",
        "numpy/tests/include/public.h",
        "numpy/tests/LICENSE",
        "numpy/tests/entry_points.txt",
        "numpy/tests/data/runtime.json",
        "numpy/tests/payload.unknown",
    ],
)
def test_runtime_pruner_rejects_unsafe_content_under_reviewed_test_root(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    """Fail closed on protected or unknown classes beneath a reviewed test root."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {
            "numpy/tests/test_policy.py": b"assert True\n",
            unsafe_path: b"protected content",
        },
    )

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert "unsafe" in (result.stdout + result.stderr).casefold()
    assert (site_packages / unsafe_path).is_file()
    assert (site_packages / "numpy" / "tests" / "test_policy.py").is_file()


@pytest.mark.parametrize("directory_name", [".libs", "headers", "include", "licenses"])
def test_runtime_pruner_rejects_empty_protected_test_directory(
    tmp_path: Path,
    directory_name: str,
) -> None:
    """Do not erase protected directory classes merely because they are empty."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {"numpy/tests/test_policy.py": b"assert True\n"},
    )
    protected = site_packages / "numpy" / "tests" / directory_name
    protected.mkdir()

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert "unsafe directory" in (result.stdout + result.stderr).casefold()
    assert protected.is_dir()


def test_runtime_pruner_rejects_unknown_distribution_test_root(tmp_path: Path) -> None:
    """Require an exact distribution and test-root policy match."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "unreviewed",
        {"unreviewed/tests/test_policy.py": b"assert True\n"},
    )

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert "unreviewed" in (result.stdout + result.stderr).casefold()
    assert (site_packages / "unreviewed" / "tests" / "test_policy.py").is_file()


def test_runtime_pruner_rejects_unknown_root_for_reviewed_distribution(
    tmp_path: Path,
) -> None:
    """Require an exact reviewed root even when the distribution is approved."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {"numpy/benchmarks/tests/test_policy.py": b"assert True\n"},
    )

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert (
        "unreviewed distribution or test root"
        in (result.stdout + result.stderr).casefold()
    )
    assert (
        site_packages / "numpy" / "benchmarks" / "tests" / "test_policy.py"
    ).is_file()


def test_runtime_pruner_rejects_executable_test_source(tmp_path: Path) -> None:
    """Never delete executable content even when its suffix and root are reviewed."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {"numpy/tests/test_policy.py": b"assert True\n"},
    )
    target = site_packages / "numpy" / "tests" / "test_policy.py"
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    if not target.stat().st_mode & stat.S_IXUSR:
        pytest.skip("executable mode bits are unavailable")

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert "unsafe file class" in (result.stdout + result.stderr).casefold()
    assert target.is_file()


def test_runtime_pruner_rejects_duplicate_canonical_distribution_identity(
    tmp_path: Path,
) -> None:
    """Reject duplicate canonical name/version identities across site roots."""
    environment_root = tmp_path / ".venv"
    _write_distribution(
        environment_root,
        "numpy",
        {"numpy/runtime.py": b"VALUE = 1\n"},
    )
    _write_distribution(
        environment_root,
        "numpy",
        {"numpy/runtime.py": b"VALUE = 1\n"},
        library_name="lib64",
    )

    result = _run_pruner(environment_root, verify_only=True)

    assert result.returncode == 1
    assert (
        "duplicate distribution identity" in (result.stdout + result.stderr).casefold()
    )


def test_runtime_pruner_rewrites_record_deterministically_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """Sort retained rows deterministically and make a second run a no-op."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {
            "numpy/z_runtime.py": b"Z = True\n",
            "numpy/tests/test_policy.py": b"assert True\n",
            "numpy/a_runtime.py": b"A = True\n",
        },
    )

    first = _run_pruner(environment_root)
    first_record = _record_path(site_packages, "numpy").read_bytes()
    second = _run_pruner(environment_root)

    assert first.returncode == 0, first.stdout + first.stderr
    assert json.loads(first.stdout) == {
        "directories_removed": 1,
        "distributions_verified": 1,
        "files_removed": 1,
        "records_rewritten": 1,
    }
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(second.stdout)["files_removed"] == 0
    assert json.loads(second.stdout)["records_rewritten"] == 0
    assert _record_path(site_packages, "numpy").read_bytes() == first_record
    assert first_record.decode().splitlines() == sorted(
        first_record.decode().splitlines()
    )
    assert b"numpy/tests/test_policy.py" not in first_record

    verification = _run_pruner(environment_root, verify_only=True)
    assert verification.returncode == 0, verification.stdout + verification.stderr


def test_runtime_pruner_accepts_pandas_sized_metadata_and_reviewed_manifest(
    tmp_path: Path,
) -> None:
    """Accept bounded metadata above 64 KiB and remove pandas' reviewed manifest."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "pandas",
        {
            "pandas/__init__.py": b"__version__ = '1.0.0'\n",
            "pandas/pyproject.toml": b"[build-system]\n",
        },
        metadata_suffix=b"Description: " + (b"x" * 90_000) + b"\n",
    )

    result = _run_pruner(environment_root)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (site_packages / "pandas" / "pyproject.toml").exists()
    assert (site_packages / "pandas" / "__init__.py").is_file()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("signed", "signed record"),
        ("traversal", "escapes environment root"),
        ("malformed_csv", "malformed csv"),
        ("duplicate", "duplicate record path"),
        ("unsupported_hash", "unsupported record hash"),
        ("malformed_hash", "malformed sha256 record hash"),
        ("malformed_size", "malformed record size"),
    ],
)
def test_runtime_pruner_fails_closed_on_invalid_wheel_provenance(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    """Reject signatures, unsafe paths, malformed CSV, duplicates, and hashes."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "package",
        {"package/tests/test_policy.py": b"assert True\n"},
    )
    info_directory = site_packages / "package-1.0.0.dist-info"
    record_path = info_directory / "RECORD"
    if mutation == "signed":
        (info_directory / "RECORD.jws").write_bytes(b"signature")
    elif mutation == "traversal":
        record_path.write_text(
            record_path.read_text(encoding="utf-8") + "../../../../escape,,\n",
            encoding="utf-8",
            newline="",
        )
    elif mutation == "malformed_csv":
        record_path.write_text('"unterminated,,\n', encoding="utf-8", newline="")
    elif mutation == "duplicate":
        record_path.write_text(
            record_path.read_text(encoding="utf-8") + "package/tests/test_policy.py,"
            "sha256=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,1\n",
            encoding="utf-8",
            newline="",
        )
    elif mutation == "unsupported_hash":
        existing = next(
            line
            for line in record_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("package/tests/test_policy.py,")
        )
        record_path.write_text(
            record_path.read_text(encoding="utf-8").replace(
                existing,
                "package/tests/test_policy.py,md5=synthetic,12",
            ),
            encoding="utf-8",
            newline="",
        )
    else:
        rows = list(csv.reader(record_path.read_text(encoding="utf-8").splitlines()))
        target_row = next(
            row for row in rows if row[0] == "package/tests/test_policy.py"
        )
        target_row[1 if mutation == "malformed_hash" else 2] = (
            "sha256=not-a-canonical-digest" if mutation == "malformed_hash" else "01"
        )
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        record_path.write_text(output.getvalue(), encoding="utf-8", newline="")

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert reason in (result.stdout + result.stderr).casefold()
    assert (site_packages / "package" / "tests" / "test_policy.py").is_file()


@pytest.mark.parametrize(
    ("relative_path", "field_index"),
    [
        ("numpy/tests/test_policy.py", 1),
        ("numpy/tests/test_policy.py", 2),
        ("numpy-1.0.0.dist-info/METADATA", 1),
        ("numpy-1.0.0.dist-info/WHEEL", 2),
    ],
)
def test_runtime_pruner_requires_hash_and_size_provenance(
    tmp_path: Path,
    relative_path: str,
    field_index: int,
) -> None:
    """Require complete provenance for deletion targets and required metadata."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {"numpy/tests/test_policy.py": b"assert True\n"},
    )
    record_path = _record_path(site_packages, "numpy")
    rows = list(csv.reader(record_path.read_text(encoding="utf-8").splitlines()))
    target_row = next(row for row in rows if row[0] == relative_path)
    target_row[field_index] = ""
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    record_path.write_text(output.getvalue(), encoding="utf-8", newline="")

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert (
        "requires sha256 hash and exact size"
        in (result.stdout + result.stderr).casefold()
    )
    assert (site_packages / "numpy" / "tests" / "test_policy.py").is_file()


def test_runtime_pruner_requires_disposable_environment_acknowledgment(
    tmp_path: Path,
) -> None:
    """Guard in-place mutation behind an explicit disposable-build contract."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {"numpy/tests/test_policy.py": b"assert True\n"},
    )

    result = subprocess.run(
        [
            sys.executable,
            str(PRUNE_SCRIPT),
            "--environment-root",
            str(environment_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert (
        "in-place and non-transactional" in (result.stdout + result.stderr).casefold()
    )
    assert (site_packages / "numpy" / "tests" / "test_policy.py").is_file()


def test_runtime_pruner_completes_preflight_before_any_mutation(tmp_path: Path) -> None:
    """Preserve all targets when a later distribution fails validation."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {"numpy/tests/test_policy.py": b"assert True\n"},
    )
    _write_distribution(
        environment_root,
        "tornado",
        {"tornado/runtime.py": b"VALUE = 1\n"},
    )
    (site_packages / "tornado" / "runtime.py").write_bytes(b"CORRUPTED\n")

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert "record sha256 mismatch" in (result.stdout + result.stderr).casefold()
    assert (site_packages / "numpy" / "tests" / "test_policy.py").is_file()
    assert _record_path(site_packages, "numpy").is_file()


def test_runtime_pruner_preserves_runtime_headers_native_data_and_licenses(
    tmp_path: Path,
) -> None:
    """Keep similarly named modules and non-test runtime package content."""
    environment_root = tmp_path / ".venv"
    preserved = {
        "numpy/test_attributes.py": b"PUBLIC = True\n",
        "numpy/include/public.h": b"#define PUBLIC 1\n",
        "numpy/native.dll": b"native dll",
        "numpy/.libs/native.so": b"native so",
        "numpy/data/schema.json": b"{}\n",
        "numpy/LICENSE": b"synthetic license\n",
    }
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {**preserved, "numpy/tests/test_policy.py": b"assert True\n"},
    )

    result = _run_pruner(environment_root)

    assert result.returncode == 0, result.stdout + result.stderr
    for relative_path, content in preserved.items():
        assert (site_packages / relative_path).read_bytes() == content


def test_runtime_pruner_never_prunes_optima_distribution(tmp_path: Path) -> None:
    """Reject rather than exempt or mutate an OPTIMA-owned test path."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "optima",
        {"optima/tests/test_policy.py": b"assert True\n"},
    )

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert "never pruned" in (result.stdout + result.stderr).casefold()
    assert (site_packages / "optima" / "tests" / "test_policy.py").is_file()


def test_runtime_pruner_rejects_unowned_target_content(tmp_path: Path) -> None:
    """Do not delete test content that no validated RECORD owns."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {"numpy/runtime.py": b"VALUE = 1\n"},
    )
    unowned = site_packages / "numpy" / "tests" / "test_unowned.py"
    unowned.parent.mkdir()
    unowned.write_bytes(b"assert True\n")

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert "unowned test tree" in (result.stdout + result.stderr).casefold()
    assert unowned.is_file()


def test_runtime_pruner_rejects_symlinked_environment_root(tmp_path: Path) -> None:
    """Require the explicit environment root itself to be a real directory."""
    environment_root = tmp_path / "real-venv"
    site_packages = _write_distribution(
        environment_root,
        "package",
        {"package/tests/test_policy.py": b"assert True\n"},
    )
    linked_root = tmp_path / "linked-venv"
    try:
        linked_root.symlink_to(environment_root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    result = _run_pruner(linked_root)

    assert result.returncode == 1
    assert "environment root" in (result.stdout + result.stderr).casefold()
    assert (site_packages / "package" / "tests" / "test_policy.py").is_file()


def test_runtime_pruner_rejects_symlinked_record_target(tmp_path: Path) -> None:
    """Never follow a RECORD-owned target symlink during validation or deletion."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {"numpy/tests/test_policy.py": b"assert True\n"},
    )
    target = site_packages / "numpy" / "tests" / "test_policy.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"PRESERVE = True\n")
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks are unavailable: {error}")

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert "symlink" in (result.stdout + result.stderr).casefold()
    assert outside.read_bytes() == b"PRESERVE = True\n"


def test_runtime_pruner_verify_only_rejects_symlinked_test_root(
    tmp_path: Path,
) -> None:
    """Reject an unowned test-root symlink instead of silently skipping it."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {"numpy/runtime.py": b"VALUE = 1\n"},
    )
    outside = tmp_path / "outside-tests"
    outside.mkdir()
    linked_root = site_packages / "numpy" / "tests"
    try:
        linked_root.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    result = _run_pruner(environment_root, verify_only=True)

    assert result.returncode == 1
    assert "symlink" in (result.stdout + result.stderr).casefold()


def test_runtime_pruner_verifies_retained_sha256_and_size(tmp_path: Path) -> None:
    """Reject corrupted retained runtime files before deleting any target."""
    environment_root = tmp_path / ".venv"
    site_packages = _write_distribution(
        environment_root,
        "numpy",
        {
            "numpy/runtime.py": b"VALUE = 1\n",
            "numpy/tests/test_policy.py": b"assert True\n",
        },
    )
    runtime_path = site_packages / "numpy" / "runtime.py"
    runtime_path.write_bytes(b"CORRUPTED\n")

    result = _run_pruner(environment_root)

    assert result.returncode == 1
    assert "record sha256 mismatch" in (result.stdout + result.stderr).casefold()
    assert (site_packages / "numpy" / "tests" / "test_policy.py").is_file()


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


def test_pr_security_workflow_requires_exact_uv_semantic_version() -> None:
    """Allow official metadata without accepting another uv semantic version."""
    scripts = "\n".join(_literal_run_blocks(_pr_security_workflow()))
    validation = '''uv_version="$(uv --version)"
uv_semver="${uv_version#uv }"
uv_semver="${uv_semver%% *}"
test "$uv_semver" = "0.12.5"'''

    assert validation in scripts

    def extract_semantic_version(output: str) -> str:
        without_prefix = output.removeprefix("uv ")
        return without_prefix.split(" ", maxsplit=1)[0]

    assert extract_semantic_version("uv 0.12.5 (1fd1c40d8 2026-08-26)") == "0.12.5"
    assert extract_semantic_version("uv 0.12.4 (official build metadata)") != "0.12.5"


def test_pr_security_workflow_runs_full_suite_from_repository_root() -> None:
    """Invoke pytest as a module so checkout-local scripts remain importable."""
    scripts = "\n".join(_literal_run_blocks(_pr_security_workflow()))

    assert (
        'uv run --no-sync python -m pytest --junitxml="$evidence/pytest.xml"' in scripts
    )
    assert re.search(r"(?m)^\s*uv run(?: --no-sync)? pytest(?:\s|$)", scripts) is None


def test_pr_security_workflow_compiles_every_dedented_python_heredoc() -> None:
    """Compile every embedded program exactly as YAML passes it to Bash."""
    programs = _python_heredocs(_pr_security_workflow())

    assert len(programs) == 9
    for index, program in enumerate(programs):
        compile(program, f"pr-security-containers.yml:heredoc-{index}", "exec")


def test_pr_security_workflow_all_shell_blocks_pass_bash_syntax() -> None:
    """Validate every literal workflow command after YAML indentation removal."""
    bash = shutil.which("bash")
    if bash is None and sys.platform == "win32":
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(git_bash) if git_bash.is_file() else None
    assert bash is not None, "bash is required to validate workflow shell syntax"

    blocks = _literal_run_blocks(_pr_security_workflow())
    assert len(blocks) == 24
    for index, block in enumerate(blocks):
        result = subprocess.run(
            [bash, "-n"],
            input=block,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"shell block {index}: {result.stderr}"


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


def test_pr_security_workflow_attests_both_saved_images_and_rootfs_archives() -> None:
    """Require OCI ancestry and reviewed base paths before rootfs exceptions apply."""
    scripts = "\n".join(_literal_run_blocks(_pr_security_workflow()))

    assert scripts.count("docker image save --output") == 2
    assert scripts.count("verify_container_artifacts.py attest-base") == 2
    assert scripts.count("--image-archive") == 2
    assert scripts.count("--rootfs-archive") == 2
    assert scripts.count("--trusted-manifest") == 2
    assert scripts.count("--dockerfile") == 2
    assert scripts.count("--attestation") == 2
    assert "security/config/pinned-runtime-base.json" in scripts
    assert "api-base-attestation-summary.json" in scripts
    assert "ui-base-attestation-summary.json" in scripts
    assert "pinned-runtime-base.json" in scripts
    assert '"reviewed_base_config_digest"' in scripts
    assert '"reviewed_base_manifest_digest"' in scripts
    assert '"base_config_digest"' not in scripts
    assert '"base_manifest_digest"' not in scripts


def test_pr_security_workflow_defers_all_security_failures_to_one_gate() -> None:
    """Collect both images' evidence before applying one fail-closed policy gate."""
    content = _pr_security_workflow()
    scripts = "\n".join(_literal_run_blocks(content))

    assert "continue-on-error" not in content
    assert scripts.count("api_image_save_status=$?") == 1
    assert scripts.count("ui_image_save_status=$?") == 1
    assert scripts.count("api_attestation_status=$?") == 1
    assert scripts.count("ui_attestation_status=$?") == 1
    assert scripts.count("api_rootfs_status=$?") == 1
    assert scripts.count("ui_rootfs_status=$?") == 1
    assert scripts.count("api_syft_status=$?") == 1
    assert scripts.count("ui_syft_status=$?") == 1
    assert scripts.count("api_trivy_status=$?") == 1
    assert scripts.count("ui_trivy_status=$?") == 1
    assert scripts.count("sbom_validation_status=$?") == 1
    assert scripts.count("trivy_policy_status=$?") == 1
    assert content.count("name: Apply aggregate final-image security gate") == 1

    rootfs_api = scripts.index("--component api")
    rootfs_ui = scripts.index("--component ui")
    syft_api = scripts.index("docker:optima-api:pr")
    syft_ui = scripts.index("docker:optima-ui:pr")
    trivy_api = scripts.index("optima-api:pr", syft_ui + 1)
    trivy_ui = scripts.index("optima-ui:pr", trivy_api + 1)
    final_gate = scripts.index("security-gate-summary.json")
    assert (
        rootfs_api < rootfs_ui < syft_api < syft_ui < trivy_api < trivy_ui < final_gate
    )
    assert "api-rootfs-summary.json" in scripts
    assert "ui-rootfs-summary.json" in scripts
    assert "api-base-attestation-summary.json" in scripts
    assert "ui-base-attestation-summary.json" in scripts
    assert "final-image-sbom-summary.json" in scripts
    assert "trivy-summary.json" in scripts


def test_pr_security_collectors_explicitly_disable_inherited_errexit() -> None:
    """Prevent GitHub's Bash -e default from truncating collected evidence."""
    content = _pr_security_workflow()
    collectors = (
        "Export and verify final filesystems without an image shell",
        "Generate and validate final-image CycloneDX SBOMs",
        "Scan both final images with Trivy",
        "Evaluate aggregate Trivy policy",
    )

    for step_name in collectors:
        block = _named_run_block(content, step_name)
        commands = [line.strip() for line in block.splitlines() if line.strip()]
        assert commands[0] == "set +e", step_name
        assert "set -uo pipefail" in commands[:2], step_name

    gate = _named_run_block(content, "Apply aggregate final-image security gate")
    assert gate.splitlines()[0] == "set -euo pipefail"


def test_rootfs_collector_continues_after_first_failure_under_bash_errexit(
    tmp_path: Path,
) -> None:
    """Run the extracted collector with Bash -e and prove both images are recorded."""
    bash = shutil.which("bash")
    if bash is None and sys.platform == "win32":
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(git_bash) if git_bash.is_file() else None
    assert bash is not None, "bash is required to execute the workflow collector"
    block = _named_run_block(
        _pr_security_workflow(),
        "Export and verify final filesystems without an image shell",
    )
    stubs = r"""
docker() {
    if test "$1" = "create" && test "$4" = "optima-api:pr"; then
        return 7
    fi
    if test "$1" = "export"; then
        : > "$3"
    fi
    return 0
}
uv() {
    local component=""
    local output=""
    while test "$#" -gt 0; do
        case "$1" in
            --component) component="$2"; shift 2 ;;
            --output) output="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    printf '{"component":"%s","status":"pass"}\n' "$component" > "$output"
    return 0
}
"""
    (tmp_path / "evidence").mkdir()
    environment = os.environ.copy()
    environment["RUNNER_TEMP"] = tmp_path.as_posix()
    result = subprocess.run(
        [bash, "-e", "-o", "pipefail", "-c", stubs + block],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    statuses = (tmp_path / "evidence" / "rootfs-command-statuses.txt").read_text(
        encoding="utf-8"
    )
    ui_summary = json.loads(
        (tmp_path / "evidence" / "ui-rootfs-summary.json").read_text(encoding="utf-8")
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert statuses.split() == [
        "0",
        "7",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
    ]
    assert ui_summary == {"component": "ui", "status": "pass"}


@pytest.mark.parametrize(
    ("status_file", "status_index", "status_name"),
    [
        ("rootfs-command-statuses.txt", 0, "api_image_save"),
        ("rootfs-command-statuses.txt", 1, "api_create"),
        ("rootfs-command-statuses.txt", 2, "api_export"),
        ("rootfs-command-statuses.txt", 3, "api_remove"),
        ("rootfs-command-statuses.txt", 4, "api_attestation"),
        ("rootfs-command-statuses.txt", 5, "api_rootfs"),
        ("rootfs-command-statuses.txt", 6, "ui_image_save"),
        ("rootfs-command-statuses.txt", 7, "ui_create"),
        ("rootfs-command-statuses.txt", 8, "ui_export"),
        ("rootfs-command-statuses.txt", 9, "ui_remove"),
        ("rootfs-command-statuses.txt", 10, "ui_attestation"),
        ("rootfs-command-statuses.txt", 11, "ui_rootfs"),
        ("sbom-command-statuses.txt", 0, "api_syft"),
        ("sbom-command-statuses.txt", 1, "ui_syft"),
        ("sbom-command-statuses.txt", 2, "sbom_validation"),
        ("trivy-command-statuses.txt", 0, "api_trivy"),
        ("trivy-command-statuses.txt", 1, "ui_trivy"),
        ("trivy-policy-status.txt", 0, "trivy_policy"),
    ],
)
def test_pr_security_aggregate_gate_enforces_every_collector_status(
    tmp_path: Path,
    status_file: str,
    status_index: int,
    status_name: str,
) -> None:
    """Fail on every captured command error even when policy summaries pass."""
    status_values = {
        "rootfs-command-statuses.txt": [0] * 12,
        "sbom-command-statuses.txt": [0] * 3,
        "trivy-command-statuses.txt": [0] * 2,
        "trivy-policy-status.txt": [0],
    }
    status_values[status_file][status_index] = 9
    for name, values in status_values.items():
        (tmp_path / name).write_text(
            " ".join(str(value) for value in values) + "\n",
            encoding="utf-8",
        )
    for component in ("api", "ui"):
        (tmp_path / f"{component}-base-attestation-summary.json").write_text(
            json.dumps({"component": component, "status": "pass"}),
            encoding="utf-8",
        )
        (tmp_path / f"{component}-rootfs-summary.json").write_text(
            json.dumps({"status": "pass"}),
            encoding="utf-8",
        )
    (tmp_path / "final-image-sbom-summary.json").write_text(
        json.dumps(
            {
                "api": {"status": "pass"},
                "ui": {"status": "pass"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "trivy-summary.json").write_text(
        json.dumps({"status": "pass"}),
        encoding="utf-8",
    )
    gate_program = next(
        program
        for program in _python_heredocs(_pr_security_workflow())
        if "security-gate-summary.json" in program
    )

    result = subprocess.run(
        [sys.executable, "-c", gate_program, str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    gate = json.loads(
        (tmp_path / "security-gate-summary.json").read_text(encoding="utf-8")
    )

    assert result.returncode == 1
    assert gate["status"] == "fail"
    assert gate["command_statuses"][status_name] == 9
    assert any(status_name in finding for finding in gate["findings"])
