"""Verify final-image root filesystems and aggregate Trivy reports."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import tarfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_ERROR = 2
EVIDENCE_SCHEMA_VERSION = 1
TRIVY_SCHEMA_VERSION = 2
MAX_FINDINGS = 64
MAX_TEXT_LENGTH = 240
PRIVATE_KEY_SCAN_CHUNK_BYTES = 64 * 1024
MAX_PEM_LINE_BYTES = 4096
MIN_PRIVATE_KEY_ENCODED_BYTES = 16
MAX_DISTRIBUTION_METADATA_BYTES = 64 * 1024
MAX_DISTRIBUTION_RECORD_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_SBOM_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 250_000

Component = Literal["api", "ui"]

REQUIRED_ANCHORS: dict[Component, tuple[str, ...]] = {
    "api": (
        "app/src/optima/api/app.py",
        "app/src/optima/api/production.py",
    ),
    "ui": (
        "app/src/ui/app.py",
        "app/src/ui/api_client.py",
    ),
}
PRIVATE_KEY_HEADERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
)
PRIVATE_KEY_BOUNDARIES = {
    header: header.replace(b"BEGIN", b"END", 1) for header in PRIVATE_KEY_HEADERS
}
PRIVATE_KEY_END_MARKERS = frozenset(PRIVATE_KEY_BOUNDARIES.values())
BASE64_ALPHABET = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
)
PACKAGE_MANAGERS = {
    "apk",
    "apt",
    "apt-get",
    "dnf",
    "npm",
    "pip",
    "pip3",
    "rpm",
    "uv",
    "yum",
}
DOWNLOAD_TOOLS = {"curl", "git", "wget"}
COMPILERS = {"c++", "cc", "clang", "clang++", "cmake", "g++", "gcc", "make"}
SHELLS = {"ash", "bash", "busybox", "csh", "dash", "fish", "ksh", "sh", "tcsh", "zsh"}
EXECUTABLE_DIRECTORIES = {
    "bin",
    "sbin",
    "usr/bin",
    "usr/sbin",
    "usr/local/bin",
    "usr/local/sbin",
    "app/.venv/bin",
}


def _bounded_text(value: object) -> str:
    """Return a single-line bounded representation safe for evidence output."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return f"{text[: MAX_TEXT_LENGTH - 3]}..."


def _finding(code: str, message: str, *, path: str | None = None) -> dict[str, str]:
    """Build one bounded policy finding without artifact contents."""
    finding = {"code": code, "message": _bounded_text(message)}
    if path is not None:
        finding["path"] = _bounded_text(path)
    return finding


def _append_finding(
    findings: list[dict[str, str]],
    finding: dict[str, str],
    total: list[int],
) -> None:
    """Count every finding while retaining only a bounded evidence sample."""
    total[0] += 1
    if len(findings) < MAX_FINDINGS:
        findings.append(finding)


def _normalize_archive_path(name: str) -> tuple[str | None, str | None]:
    """Normalize one POSIX tar path or return its safety error."""
    if not name or "\x00" in name:
        return None, "empty or NUL-containing archive path"
    if "\\" in name:
        return None, "archive path contains a non-POSIX separator"
    path = PurePosixPath(name)
    if path.is_absolute():
        return None, "absolute archive path"

    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            return None, "parent-traversal archive path"
        parts.append(part)
    if not parts:
        return ".", None
    return "/".join(parts), None


def _link_target_is_safe(path: str, linkname: str, *, symbolic: bool) -> bool:
    """Return whether an archive link resolves within the exported rootfs."""
    if not linkname or "\x00" in linkname or "\\" in linkname:
        return False
    target = PurePosixPath(linkname)
    base = (
        list(PurePosixPath(path).parent.parts)
        if symbolic and not target.is_absolute()
        else []
    )
    for part in target.parts:
        if part in {"", ".", "/"}:
            continue
        if part == "..":
            if not base:
                return False
            base.pop()
        else:
            base.append(part)
    return bool(base)


def _starts_with(path: str, prefix: str) -> bool:
    """Return whether a normalized path equals or descends from a prefix."""
    return path == prefix or path.startswith(f"{prefix}/")


def _executable_parent(path: str) -> str:
    """Return the normalized parent path for executable policy checks."""
    return str(PurePosixPath(path).parent)


def _site_packages_root(path: str) -> str | None:
    """Return the supported virtual-environment site-packages root for a path."""
    parts = PurePosixPath(path).parts
    if (
        len(parts) >= 5
        and parts[0] == "app"
        and parts[1] == ".venv"
        and parts[2] in {"lib", "lib64"}
        and parts[3].startswith("python")
        and parts[4] == "site-packages"
    ):
        return "/".join(parts[:5])
    return None


def _canonical_distribution_name(value: str) -> str:
    """Return the normalized Python distribution name used for comparisons."""
    return re.sub(r"[-_.]+", "-", value).casefold()


def _distribution_directory_name(info_directory: str) -> str | None:
    """Return the distribution portion of a valid wheel metadata directory."""
    if not info_directory.endswith(".dist-info"):
        return None
    stem = info_directory[: -len(".dist-info")]
    distribution, separator, version = stem.rpartition("-")
    if not separator or not distribution or not version:
        return None
    return distribution


def _metadata_distribution_identity(extracted: IO[bytes]) -> tuple[str, str] | None:
    """Read bounded core-metadata Name and Version fields."""
    content = extracted.read(MAX_DISTRIBUTION_METADATA_BYTES + 1)
    if len(content) > MAX_DISTRIBUTION_METADATA_BYTES:
        return None
    fields: dict[str, str] = {}
    for line in content.splitlines():
        name, separator, value = line.partition(b":")
        field = name.lower()
        if separator and field in {b"name", b"version"}:
            try:
                decoded = value.strip().decode("utf-8")
            except UnicodeDecodeError:
                return None
            if not decoded or field.decode() in fields:
                return None
            fields[field.decode()] = decoded
        if not line:
            break
    if fields.keys() != {"name", "version"}:
        return None
    return fields["name"], fields["version"]


def _runtime_sbom_distributions(extracted: IO[bytes]) -> set[tuple[str, str]]:
    """Return bounded non-OPTIMA Python distribution identities from an SBOM."""
    content = extracted.read(MAX_RUNTIME_SBOM_BYTES + 1)
    if len(content) > MAX_RUNTIME_SBOM_BYTES:
        return set()
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return set()
    if not isinstance(document, dict) or document.get("bomFormat") != "CycloneDX":
        return set()
    components = document.get("components")
    if not isinstance(components, list):
        return set()
    distributions: set[tuple[str, str]] = set()
    for component in components:
        if not isinstance(component, dict) or component.get("type") != "library":
            continue
        name = component.get("name")
        version = component.get("version")
        purl = component.get("purl")
        if (
            isinstance(name, str)
            and isinstance(version, str)
            and isinstance(purl, str)
            and purl.startswith("pkg:pypi/")
            and _canonical_distribution_name(name) != "optima"
        ):
            distributions.add((_canonical_distribution_name(name), version))
    return distributions


def _distribution_record_paths(
    extracted: IO[bytes],
    *,
    site_packages_root: str,
) -> set[str] | None:
    """Read bounded wheel RECORD paths that remain inside site-packages."""
    content = extracted.read(MAX_DISTRIBUTION_RECORD_BYTES + 1)
    if len(content) > MAX_DISTRIBUTION_RECORD_BYTES:
        return None
    try:
        rows = csv.reader(io.StringIO(content.decode("utf-8")), strict=True)
        paths: set[str] = set()
        for row in rows:
            if not row:
                continue
            relative, error = _normalize_archive_path(row[0])
            if (
                error is not None
                or relative in {None, "."}
                or PurePosixPath(row[0]).is_absolute()
            ):
                continue
            paths.add(f"{site_packages_root}/{relative}")
    except (csv.Error, UnicodeDecodeError):
        return None
    return paths


def _validated_third_party_distribution_paths(
    archive: tarfile.TarFile,
    component: Component,
) -> set[str]:
    """Return files and directories proven to belong to non-OPTIMA wheels."""
    regular_members: dict[str, tarfile.TarInfo] = {}
    record_candidates: list[tuple[str, str, tarfile.TarInfo]] = []
    for index, member in enumerate(archive):
        if index >= MAX_ARCHIVE_ENTRIES:
            return set()
        normalized, error = _normalize_archive_path(member.name)
        if error is not None or normalized is None or not member.isreg():
            continue
        regular_members[normalized] = member
        root = _site_packages_root(normalized)
        if root is None:
            continue
        relative_parts = PurePosixPath(normalized).parts[5:]
        if len(relative_parts) == 2 and relative_parts[1] == "RECORD":
            record_candidates.append((root, relative_parts[0], member))

    owned_paths: set[str] = set()
    sbom_member = regular_members.get(f"app/sbom/{component}.cdx.json")
    if sbom_member is None:
        return owned_paths
    sbom = archive.extractfile(sbom_member)
    if sbom is None:
        return owned_paths
    runtime_distributions = _runtime_sbom_distributions(sbom)
    for root, info_directory, record_member in record_candidates:
        directory_distribution = _distribution_directory_name(info_directory)
        if directory_distribution is None:
            continue
        metadata_path = f"{root}/{info_directory}/METADATA"
        wheel_path = f"{root}/{info_directory}/WHEEL"
        record_path = f"{root}/{info_directory}/RECORD"
        metadata_member = regular_members.get(metadata_path)
        if metadata_member is None or wheel_path not in regular_members:
            continue
        metadata = archive.extractfile(metadata_member)
        record = archive.extractfile(record_member)
        if metadata is None or record is None:
            continue
        metadata_identity = _metadata_distribution_identity(metadata)
        if metadata_identity is None:
            continue
        metadata_name, metadata_version = metadata_identity
        canonical_name = _canonical_distribution_name(metadata_name)
        if (
            canonical_name == "optima"
            or canonical_name != _canonical_distribution_name(directory_distribution)
            or (canonical_name, metadata_version) not in runtime_distributions
        ):
            continue
        record_paths = _distribution_record_paths(
            record,
            site_packages_root=root,
        )
        required_metadata = {metadata_path, wheel_path, record_path}
        if record_paths is None or not required_metadata <= record_paths:
            continue
        distribution_files = record_paths & regular_members.keys()
        owned_paths.update(distribution_files)
        for distribution_file in distribution_files:
            parent = PurePosixPath(distribution_file).parent
            while str(parent) != root and _starts_with(str(parent), root):
                owned_paths.add(str(parent))
                parent = parent.parent
    return owned_paths


def _path_policy_finding(
    path: str,
    *,
    executable: bool,
    third_party_distribution_paths: set[str],
    link: bool,
) -> dict[str, str] | None:
    """Return the first applicable final-image path policy violation."""
    pure_path = PurePosixPath(path)
    parts = tuple(part.casefold() for part in pure_path.parts)
    filename = pure_path.name.casefold()
    parent = _executable_parent(path).casefold()
    third_party_site_package = not link and path in third_party_distribution_paths

    if ".git" in parts:
        return _finding("git_metadata", "unexpected .git metadata", path=path)
    if filename == ".env" or filename.startswith(".env."):
        return _finding(
            "credential_env", f"unexpected {filename} credential file", path=path
        )
    if not third_party_site_package and (
        _starts_with(path.casefold(), "app/tests")
        or (_starts_with(path.casefold(), "app") and "tests" in parts)
        or (
            _starts_with(path.casefold(), "app")
            and filename.startswith("test_")
            and filename.endswith(".py")
        )
    ):
        return _finding("repository_tests", "unexpected repository tests", path=path)
    if (
        not third_party_site_package
        and _starts_with(path.casefold(), "app")
        and "fixtures" in parts
    ):
        return _finding(
            "repository_fixtures", "unexpected repository fixtures", path=path
        )
    if _starts_with(path.casefold(), "app/scripts"):
        return _finding("build_scripts", "unexpected application scripts", path=path)
    if not third_party_site_package and (
        filename in {"pyproject.toml", "uv.lock"}
        or filename == "dockerfile"
        or filename.startswith("dockerfile.")
    ):
        return _finding("build_source", "unexpected build-only source", path=path)
    if parts and parts[0] in {"docs", "infra", ".copilot-tracking"}:
        return _finding("repository_source", "unexpected repository source", path=path)
    if (
        len(parts) > 1
        and parts[0] == "app"
        and parts[1]
        in {
            "docs",
            "infra",
            ".copilot-tracking",
        }
    ):
        return _finding("repository_source", "unexpected repository source", path=path)

    if ".cache" in parts and "uv" in parts:
        return _finding("uv_cache", "unexpected uv cache", path=path)
    if ".cache" in parts and "pip" in parts:
        return _finding("pip_cache", "unexpected pip cache", path=path)
    if _starts_with(path.casefold(), "root/.cache"):
        return _finding("root_cache", "unexpected root cache", path=path)
    if _starts_with(path.casefold(), "app/.cache"):
        return _finding("application_cache", "unexpected application cache", path=path)
    if _starts_with(path.casefold(), "var/cache") or _starts_with(
        path.casefold(), "var/lib/apt/lists"
    ):
        return _finding(
            "package_manager_cache",
            "unexpected package-manager cache",
            path=path,
        )
    if any(part in {".ccache", "ccache"} for part in parts):
        return _finding("compiler_cache", "unexpected compiler cache", path=path)

    if executable or parent in EXECUTABLE_DIRECTORIES:
        if filename in PACKAGE_MANAGERS:
            return _finding("package_manager", "unexpected package manager", path=path)
        if filename in DOWNLOAD_TOOLS:
            return _finding(
                "download_tool", "unexpected download or source-control tool", path=path
            )
        if filename in COMPILERS:
            return _finding("compiler", "unexpected compiler or build tool", path=path)
        if filename in SHELLS:
            return _finding("shell", "unexpected shell binary", path=path)
    if (
        not third_party_site_package
        and ("include" in parts or _starts_with(path.casefold(), "app"))
        and pure_path.suffix.casefold()
        in {
            ".h",
            ".hh",
            ".hpp",
            ".hxx",
        }
    ):
        return _finding("build_header", "unexpected build header", path=path)
    return None


def _bounded_binary_lines(extracted: IO[bytes]) -> Iterator[tuple[bytes, bool]]:
    """Read bounded binary lines from one file chunk without retaining file contents."""
    line = bytearray()
    overflow = False
    while chunk := extracted.read(PRIVATE_KEY_SCAN_CHUNK_BYTES):
        for value in chunk:
            if value == 0x0A:
                yield bytes(line), overflow
                line.clear()
                overflow = False
            elif len(line) < MAX_PEM_LINE_BYTES:
                line.append(value)
            else:
                overflow = True
    if line or overflow:
        yield bytes(line), overflow


def _contains_structured_private_key(extracted: IO[bytes]) -> bool:
    """Stream one file and detect complete, plausibly encoded private-key PEMs."""
    expected_end: bytes | None = None
    encoded_length = 0
    encoded_data_length = 0
    padding_length = 0
    encoded_data_started = False

    for raw_line, overflow in _bounded_binary_lines(extracted):
        line = raw_line.strip(b" \t\r")
        if overflow:
            expected_end = None
            continue

        replacement_end = PRIVATE_KEY_BOUNDARIES.get(line)
        if replacement_end is not None:
            expected_end = replacement_end
            encoded_length = 0
            encoded_data_length = 0
            padding_length = 0
            encoded_data_started = False
            continue
        if expected_end is None:
            continue
        if line == expected_end:
            if (
                encoded_data_length >= MIN_PRIVATE_KEY_ENCODED_BYTES
                and encoded_length % 4 == 0
                and padding_length <= 2
            ):
                return True
            expected_end = None
            continue
        if line in PRIVATE_KEY_END_MARKERS:
            expected_end = None
            continue
        if not line:
            continue
        if not encoded_data_started and b":" in line:
            header_name, separator, header_value = line.partition(b":")
            if (
                separator
                and header_name
                and header_value.strip()
                and all(0x20 <= value <= 0x7E for value in line)
            ):
                continue
            expected_end = None
            continue

        for value in line:
            if value in BASE64_ALPHABET and padding_length == 0:
                encoded_data_started = True
                encoded_data_length += 1
                encoded_length += 1
            elif value == ord("=") and encoded_data_length > 0:
                padding_length += 1
                encoded_length += 1
            else:
                expected_end = None
                break
    return False


def _required_inventory_findings(
    component: Component,
    paths: set[str],
    file_entries: set[str],
    regular_files: set[str],
) -> list[dict[str, str]]:
    """Return missing or conflicting required runtime inventory findings."""
    findings: list[dict[str, str]] = []
    for prefix, label in (
        ("app/src", "application source"),
        ("app/sbom", "sbom inventory"),
    ):
        if not any(_starts_with(path, prefix) for path in paths):
            findings.append(
                _finding(
                    "missing_runtime_inventory",
                    f"missing required {label}",
                    path=prefix,
                )
            )

    python_runtime = "app/.venv/bin/python"
    if python_runtime not in file_entries:
        findings.append(
            _finding(
                "missing_python_runtime",
                "missing required virtual environment Python runtime",
                path=python_runtime,
            )
        )

    expected_sbom = f"app/sbom/{component}.cdx.json"
    sboms = sorted(
        path
        for path in regular_files
        if _starts_with(path, "app/sbom") and path.endswith(".cdx.json")
    )
    if expected_sbom not in regular_files:
        findings.append(
            _finding(
                "missing_component_sbom",
                "missing required component sbom",
                path=expected_sbom,
            )
        )
    if sboms != [expected_sbom]:
        findings.append(
            _finding(
                "component_sbom_inventory",
                "component must contain exactly one matching sbom",
                path="app/sbom",
            )
        )

    for anchor in REQUIRED_ANCHORS[component]:
        if anchor not in regular_files:
            findings.append(
                _finding(
                    "missing_application_source",
                    "missing required application source",
                    path=anchor,
                )
            )
    return findings


def inspect_rootfs(component: Component, archive_path: Path) -> dict[str, object]:
    """Inspect one exported rootfs tar without extracting archive members."""
    findings: list[dict[str, str]] = []
    finding_total = [0]
    paths: set[str] = set()
    file_entries: set[str] = set()
    regular_files: set[str] = set()
    entry_counts: Counter[str] = Counter()
    seen_paths: dict[str, tuple[bytes, int, str]] = {}

    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            third_party_distribution_paths = _validated_third_party_distribution_paths(
                archive,
                component,
            )
        with tarfile.open(archive_path, mode="r:*") as archive:
            for index, member in enumerate(archive):
                if index >= MAX_ARCHIVE_ENTRIES:
                    _append_finding(
                        findings,
                        _finding(
                            "archive_entry_limit",
                            "archive exceeds the entry safety limit",
                        ),
                        finding_total,
                    )
                    break
                entry_counts["entries"] += 1
                normalized, path_error = _normalize_archive_path(member.name)
                if path_error is not None or normalized is None:
                    _append_finding(
                        findings,
                        _finding(
                            "unsafe_archive_path", path_error or "unsafe archive path"
                        ),
                        finding_total,
                    )
                    continue

                signature = (member.type, member.size, member.linkname)
                prior_signature = seen_paths.get(normalized)
                if prior_signature is not None and prior_signature != signature:
                    _append_finding(
                        findings,
                        _finding(
                            "conflicting_archive_entry",
                            "duplicate archive path has conflicting entries",
                            path=normalized,
                        ),
                        finding_total,
                    )
                    continue
                if prior_signature is not None:
                    _append_finding(
                        findings,
                        _finding(
                            "duplicate_archive_entry",
                            "duplicate archive path is not allowed",
                            path=normalized,
                        ),
                        finding_total,
                    )
                    continue
                seen_paths[normalized] = signature
                paths.add(normalized)

                if member.isreg():
                    entry_counts["regular_files"] += 1
                    file_entries.add(normalized)
                    regular_files.add(normalized)
                elif member.isdir():
                    entry_counts["directories"] += 1
                elif member.issym() or member.islnk():
                    entry_counts["links"] += 1
                    link_is_safe = _link_target_is_safe(
                        normalized,
                        member.linkname,
                        symbolic=member.issym(),
                    )
                    if link_is_safe:
                        file_entries.add(normalized)
                    else:
                        _append_finding(
                            findings,
                            _finding(
                                "unsafe_archive_link",
                                "archive link escapes or ambiguously targets "
                                "the rootfs",
                                path=normalized,
                            ),
                            finding_total,
                        )
                else:
                    entry_counts["unsafe_entry_types"] += 1
                    _append_finding(
                        findings,
                        _finding(
                            "unsafe_archive_entry_type",
                            "device, FIFO, or unsupported archive entry",
                            path=normalized,
                        ),
                        finding_total,
                    )

                path_finding = _path_policy_finding(
                    normalized,
                    executable=(member.issym() or member.islnk())
                    or ((member.mode & 0o111) != 0 and member.isreg()),
                    third_party_distribution_paths=third_party_distribution_paths,
                    link=member.issym() or member.islnk(),
                )
                if path_finding is not None:
                    _append_finding(findings, path_finding, finding_total)

                if member.isreg():
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        _append_finding(
                            findings,
                            _finding(
                                "archive_read_error",
                                "regular archive file could not be inspected",
                                path=normalized,
                            ),
                            finding_total,
                        )
                        continue
                    if _contains_structured_private_key(extracted):
                        _append_finding(
                            findings,
                            _finding(
                                "private_key_material",
                                "unexpected private key material",
                                path=normalized,
                            ),
                            finding_total,
                        )
    except FileNotFoundError:
        _append_finding(
            findings,
            _finding(
                "missing_archive", "rootfs archive is missing", path=archive_path.name
            ),
            finding_total,
        )
    except (OSError, tarfile.TarError) as error:
        _append_finding(
            findings,
            _finding(
                "malformed_archive",
                f"rootfs archive is malformed: {type(error).__name__}",
            ),
            finding_total,
        )

    for inventory_finding in _required_inventory_findings(
        component,
        paths,
        file_entries,
        regular_files,
    ):
        _append_finding(findings, inventory_finding, finding_total)

    findings.sort(
        key=lambda item: (item["code"], item.get("path", ""), item["message"])
    )
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "policy": "optima-final-rootfs-v1",
        "component": component,
        "archive": archive_path.name,
        "status": "pass" if finding_total[0] == 0 else "fail",
        "counts": {
            "directories": entry_counts["directories"],
            "entries": entry_counts["entries"],
            "findings": finding_total[0],
            "links": entry_counts["links"],
            "regular_files": entry_counts["regular_files"],
            "retained_findings": len(findings),
            "unsafe_entry_types": entry_counts["unsafe_entry_types"],
        },
        "findings": findings,
    }


def _optional_finding_array(
    result: dict[str, Any],
    field: str,
    component: str,
    result_index: int,
    findings: list[dict[str, str]],
    finding_total: list[int],
) -> list[Any]:
    """Read an optional Trivy finding array and fail closed on invalid types."""
    value = result.get(field, [])
    if not isinstance(value, list):
        _append_finding(
            findings,
            _finding(
                "malformed_report",
                f"{component} result {result_index} has malformed {field}",
            ),
            finding_total,
        )
        return []
    return value


def _trivy_package_is_valid(package: object) -> bool:
    """Return whether a Trivy package has bounded identity fields."""
    if not isinstance(package, dict):
        return False
    for field in ("Name", "Version"):
        value = package.get(field)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > MAX_TEXT_LENGTH
        ):
            return False
    return True


def inspect_trivy_report(
    component: str,
    report_path: Path,
    *,
    expected_artifact: str,
    expected_image_id: str,
) -> dict[str, object]:
    """Parse and summarize one Trivy JSON report without exposing secret contents."""
    findings: list[dict[str, str]] = []
    finding_total = [0]
    severity_counts: Counter[str] = Counter()
    scanner_classes: Counter[str] = Counter()
    scanner_types: Counter[str] = Counter()
    targets: set[str] = set()
    secret_count = 0
    os_package_count = 0
    python_package_count = 0

    try:
        raw_document = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _append_finding(
            findings,
            _finding("missing_report", f"{component} Trivy report is missing"),
            finding_total,
        )
        raw_document = None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _append_finding(
            findings,
            _finding(
                "malformed_report",
                f"{component} Trivy report is malformed: {type(error).__name__}",
            ),
            finding_total,
        )
        raw_document = None

    if raw_document is not None and not isinstance(raw_document, dict):
        _append_finding(
            findings,
            _finding("malformed_report", f"{component} Trivy report root is malformed"),
            finding_total,
        )
        raw_document = None

    if isinstance(raw_document, dict):
        artifact_name = raw_document.get("ArtifactName")
        if artifact_name != expected_artifact:
            _append_finding(
                findings,
                _finding(
                    "artifact_mismatch",
                    f"{component} report artifact name does not match expected image",
                ),
                finding_total,
            )
        metadata = raw_document.get("Metadata")
        image_id = metadata.get("ImageID") if isinstance(metadata, dict) else None
        if image_id != expected_image_id:
            _append_finding(
                findings,
                _finding(
                    "image_id_mismatch",
                    f"{component} report image ID does not match expected image",
                ),
                finding_total,
            )
        schema_version = raw_document.get("SchemaVersion")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != TRIVY_SCHEMA_VERSION
        ):
            _append_finding(
                findings,
                _finding(
                    "unsupported_report",
                    f"{component} Trivy report has unsupported schema version",
                ),
                finding_total,
            )
        results = raw_document.get("Results")
        if not isinstance(results, list):
            _append_finding(
                findings,
                _finding(
                    "malformed_report", f"{component} Trivy Results array is malformed"
                ),
                finding_total,
            )
            results = []
        elif not results:
            _append_finding(
                findings,
                _finding(
                    "empty_report",
                    f"{component} Trivy Results array is empty",
                ),
                finding_total,
            )

        for result_index, raw_result in enumerate(results):
            if not isinstance(raw_result, dict):
                _append_finding(
                    findings,
                    _finding(
                        "malformed_report",
                        f"{component} result {result_index} is malformed",
                    ),
                    finding_total,
                )
                continue
            result: dict[str, Any] = raw_result
            required_values: dict[str, str] = {}
            for field in ("Target", "Class", "Type"):
                value = result.get(field)
                if not isinstance(value, str) or not value.strip():
                    _append_finding(
                        findings,
                        _finding(
                            "malformed_report",
                            f"{component} result {result_index} has malformed {field}",
                        ),
                        finding_total,
                    )
                else:
                    required_values[field] = value.strip()
            if "Target" in required_values:
                targets.add(_bounded_text(required_values["Target"]))
            if "Class" in required_values:
                scanner_classes[required_values["Class"].casefold()] += 1
            if "Type" in required_values:
                scanner_types[required_values["Type"].casefold()] += 1

            result_class = required_values.get("Class", "").casefold()
            result_type = required_values.get("Type", "").casefold()
            if result_class in {"os-pkgs", "lang-pkgs"}:
                packages = result.get("Packages")
                if not isinstance(packages, list):
                    _append_finding(
                        findings,
                        _finding(
                            "malformed_report",
                            f"{component} result {result_index} has malformed Packages",
                        ),
                        finding_total,
                    )
                else:
                    package_count = 0
                    for package_index, package in enumerate(packages):
                        if not _trivy_package_is_valid(package):
                            _append_finding(
                                findings,
                                _finding(
                                    "malformed_report",
                                    f"{component} result {result_index} package "
                                    f"{package_index} is malformed",
                                ),
                                finding_total,
                            )
                            continue
                        package_count += 1
                    if result_class == "os-pkgs":
                        os_package_count += package_count
                    if result_class == "lang-pkgs" and result_type == "python-pkg":
                        python_package_count += package_count

            vulnerabilities = _optional_finding_array(
                result,
                "Vulnerabilities",
                component,
                result_index,
                findings,
                finding_total,
            )
            for vulnerability_index, vulnerability in enumerate(vulnerabilities):
                if not isinstance(vulnerability, dict):
                    _append_finding(
                        findings,
                        _finding(
                            "malformed_report",
                            f"{component} vulnerability "
                            f"{vulnerability_index} is malformed",
                        ),
                        finding_total,
                    )
                    continue
                severity = vulnerability.get("Severity")
                if not isinstance(severity, str) or not severity.strip():
                    _append_finding(
                        findings,
                        _finding(
                            "malformed_report",
                            f"{component} vulnerability "
                            f"{vulnerability_index} has malformed severity",
                        ),
                        finding_total,
                    )
                    continue
                severity_counts[severity.strip().upper()] += 1

            secrets = _optional_finding_array(
                result,
                "Secrets",
                component,
                result_index,
                findings,
                finding_total,
            )
            for secret_index, secret in enumerate(secrets):
                if not isinstance(secret, dict):
                    _append_finding(
                        findings,
                        _finding(
                            "malformed_report",
                            f"{component} secret finding {secret_index} is malformed",
                        ),
                        finding_total,
                    )
                    continue
                secret_count += 1

    if isinstance(raw_document, dict) and os_package_count == 0:
        _append_finding(
            findings,
            _finding(
                "missing_os_package_coverage",
                f"{component} report has no OS package scanner coverage",
            ),
            finding_total,
        )
    if isinstance(raw_document, dict) and python_package_count == 0:
        _append_finding(
            findings,
            _finding(
                "missing_python_package_coverage",
                f"{component} report has no Python package scanner coverage",
            ),
            finding_total,
        )

    if severity_counts["HIGH"]:
        _append_finding(
            findings,
            _finding(
                "high_vulnerabilities",
                f"{component} report contains HIGH vulnerabilities",
            ),
            finding_total,
        )
    if severity_counts["CRITICAL"]:
        _append_finding(
            findings,
            _finding(
                "critical_vulnerabilities",
                f"{component} report contains CRITICAL vulnerabilities",
            ),
            finding_total,
        )
    if secret_count:
        _append_finding(
            findings,
            _finding("secret_findings", f"{component} report contains secret findings"),
            finding_total,
        )

    findings.sort(key=lambda item: (item["code"], item["message"]))
    return {
        "component": component,
        "report": report_path.name,
        "status": "pass" if finding_total[0] == 0 else "fail",
        "target_count": len(targets),
        "targets": sorted(targets),
        "scanner_classes": dict(sorted(scanner_classes.items())),
        "scanner_types": dict(sorted(scanner_types.items())),
        "severity_counts": dict(sorted(severity_counts.items())),
        "secret_findings": secret_count,
        "os_package_count": os_package_count,
        "python_package_count": python_package_count,
        "finding_count": finding_total[0],
        "findings": findings,
    }


def _component_values(
    values: list[str],
    *,
    label: str,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Parse unique component=value CLI contracts."""
    parsed: dict[str, str] = {}
    findings: list[dict[str, str]] = []
    for index, value in enumerate(values):
        component, separator, contract_value = value.partition("=")
        component = component.strip()
        contract_value = contract_value.strip()
        if not separator or not component or not contract_value:
            findings.append(
                _finding(
                    f"malformed_{label}_argument",
                    f"{label} argument {index} is malformed",
                )
            )
        elif component in parsed:
            findings.append(
                _finding(
                    f"duplicate_{label}_component",
                    f"duplicate {component} {label} component",
                )
            )
        else:
            parsed[component] = contract_value
    return parsed, findings


def inspect_trivy_reports(
    report_values: list[str],
    expected_artifact_values: list[str],
    expected_image_id_values: list[str],
) -> dict[str, object]:
    """Evaluate every supplied Trivy report and return one aggregate summary."""
    reports: list[dict[str, object]] = []
    aggregate_severities: Counter[str] = Counter()
    aggregate_classes: Counter[str] = Counter()
    aggregate_types: Counter[str] = Counter()
    aggregate_secret_count = 0
    aggregate_target_count = 0
    components: set[str] = set()
    configuration_findings: list[dict[str, str]] = []
    expected_artifacts, artifact_findings = _component_values(
        expected_artifact_values,
        label="expected_artifact",
    )
    expected_image_ids, image_id_findings = _component_values(
        expected_image_id_values,
        label="expected_image_id",
    )
    configuration_findings.extend(artifact_findings)
    configuration_findings.extend(image_id_findings)

    if len(report_values) < 2:
        configuration_findings.append(
            _finding("insufficient_reports", "two or more Trivy reports are required")
        )

    for index, report_value in enumerate(report_values):
        component, separator, path_text = report_value.partition("=")
        component = component.strip()
        if not separator or not component or not path_text.strip():
            configuration_findings.append(
                _finding(
                    "malformed_report_argument", f"report argument {index} is malformed"
                )
            )
            continue
        if component in components:
            configuration_findings.append(
                _finding(
                    "duplicate_report_component",
                    f"duplicate {component} report component",
                )
            )
            continue
        components.add(component)
        expected_artifact = expected_artifacts.get(component)
        expected_image_id = expected_image_ids.get(component)
        if expected_artifact is None or expected_image_id is None:
            configuration_findings.append(
                _finding(
                    "missing_expected_image_contract",
                    f"missing expected image contract for {component}",
                )
            )
            continue
        report = inspect_trivy_report(
            component,
            Path(path_text),
            expected_artifact=expected_artifact,
            expected_image_id=expected_image_id,
        )
        reports.append(report)
        severity_counts = report["severity_counts"]
        scanner_classes = report["scanner_classes"]
        scanner_types = report["scanner_types"]
        secret_findings = report["secret_findings"]
        target_count = report["target_count"]
        if isinstance(severity_counts, dict):
            aggregate_severities.update(severity_counts)
        if isinstance(scanner_classes, dict):
            aggregate_classes.update(scanner_classes)
        if isinstance(scanner_types, dict):
            aggregate_types.update(scanner_types)
        if isinstance(secret_findings, int):
            aggregate_secret_count += secret_findings
        if isinstance(target_count, int):
            aggregate_target_count += target_count

    unexpected_contract_components = (
        set(expected_artifacts) | set(expected_image_ids)
    ) - components
    for component in sorted(unexpected_contract_components):
        configuration_findings.append(
            _finding(
                "orphan_expected_image_contract",
                f"expected image contract has no {component} report",
            )
        )

    reports.sort(key=lambda item: str(item["component"]))
    configuration_findings.sort(key=lambda item: (item["code"], item["message"]))
    failed = bool(configuration_findings) or any(
        report["status"] == "fail" for report in reports
    )
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "policy": "optima-trivy-final-images-v1",
        "status": "fail" if failed else "pass",
        "report_count": len(reports),
        "severity_counts": dict(sorted(aggregate_severities.items())),
        "secret_findings": aggregate_secret_count,
        "target_count": aggregate_target_count,
        "scanner_classes": dict(sorted(aggregate_classes.items())),
        "scanner_types": dict(sorted(aggregate_types.items())),
        "configuration_findings": configuration_findings,
        "reports": reports,
    }


def _write_evidence(output: Path, evidence: dict[str, object]) -> None:
    """Write deterministic UTF-8 JSON evidence."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _rootfs_console_summary(evidence: dict[str, object]) -> str:
    """Return a bounded rootfs policy summary for console logs."""
    component = evidence["component"]
    status = str(evidence["status"]).upper()
    findings = evidence["findings"]
    messages: list[str] = []
    if isinstance(findings, list):
        for finding in findings[:8]:
            if isinstance(finding, dict):
                message = finding.get("message", "policy finding")
                path = finding.get("path")
                messages.append(f"{message}{f' ({path})' if path else ''}")
    suffix = f": {'; '.join(messages)}" if messages else ""
    return _bounded_text(f"rootfs {component}: {status}{suffix}")


def _trivy_console_summary(evidence: dict[str, object]) -> str:
    """Return a bounded aggregate Trivy summary for console logs."""
    segments = [f"trivy aggregate: {str(evidence['status']).upper()}"]
    reports = evidence["reports"]
    if isinstance(reports, list):
        for report in reports:
            if not isinstance(report, dict):
                continue
            severity_counts = report.get("severity_counts", {})
            severity_text = "none"
            if isinstance(severity_counts, dict) and severity_counts:
                severity_text = ",".join(
                    f"{severity}={count}"
                    for severity, count in sorted(severity_counts.items())
                )
            segments.append(
                f"{report.get('component', 'unknown')} "
                f"vulnerabilities[{severity_text}] "
                f"secrets={report.get('secret_findings', 0)} "
                f"status={report.get('status', 'fail')}"
            )
            findings = report.get("findings", [])
            if isinstance(findings, list):
                segments.extend(
                    str(finding.get("message", "policy finding"))
                    for finding in findings[:4]
                    if isinstance(finding, dict)
                )
    configuration_findings = evidence["configuration_findings"]
    if isinstance(configuration_findings, list):
        segments.extend(
            str(finding.get("message", "configuration finding"))
            for finding in configuration_findings[:4]
            if isinstance(finding, dict)
        )
    return _bounded_text("; ".join(segments))


def create_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Verify exported final-image and Trivy security artifacts",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    rootfs_parser = subparsers.add_parser(
        "rootfs",
        help="inspect one exported final-image rootfs tar",
    )
    rootfs_parser.add_argument("--component", choices=("api", "ui"), required=True)
    rootfs_parser.add_argument("--archive", type=Path, required=True)
    rootfs_parser.add_argument("--output", type=Path, required=True)

    trivy_parser = subparsers.add_parser(
        "trivy",
        help="evaluate two or more Trivy JSON reports together",
    )
    trivy_parser.add_argument("--report", action="append", required=True)
    trivy_parser.add_argument("--expected-artifact", action="append", required=True)
    trivy_parser.add_argument("--expected-image-id", action="append", required=True)
    trivy_parser.add_argument("--output", type=Path, required=True)
    return parser


def run(arguments: argparse.Namespace) -> int:
    """Run the selected verifier and return its policy exit code."""
    if arguments.command == "rootfs":
        component: Component = arguments.component
        evidence = inspect_rootfs(component, arguments.archive)
        _write_evidence(arguments.output, evidence)
        print(_rootfs_console_summary(evidence))
        return EXIT_SUCCESS if evidence["status"] == "pass" else EXIT_FAILURE

    report_values: list[str] = arguments.report
    expected_artifact_values: list[str] = arguments.expected_artifact
    expected_image_id_values: list[str] = arguments.expected_image_id
    evidence = inspect_trivy_reports(
        report_values,
        expected_artifact_values,
        expected_image_id_values,
    )
    _write_evidence(arguments.output, evidence)
    print(_trivy_console_summary(evidence))
    return EXIT_SUCCESS if evidence["status"] == "pass" else EXIT_FAILURE


def main() -> int:
    """Parse arguments, verify artifacts, and return an explicit exit code."""
    try:
        return run(create_parser().parse_args())
    except KeyboardInterrupt:
        print("Interrupted by user", file=sys.stderr)
        return 130
    except (OSError, ValueError) as error:
        print(f"Error: {_bounded_text(type(error).__name__)}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
