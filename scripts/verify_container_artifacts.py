"""Verify final-image root filesystems and aggregate Trivy reports."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import tarfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal
from urllib.parse import unquote

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
MAX_DISTRIBUTION_METADATA_BYTES = 1024 * 1024
MAX_DISTRIBUTION_RECORD_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_SBOM_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 250_000
MAX_ATTESTATION_JSON_BYTES = 1024 * 1024
MAX_IMAGE_SAVE_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
MAX_ROOTFS_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
MAX_SAVED_LAYER_BYTES = 4 * 1024 * 1024 * 1024
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")

Component = Literal["api", "ui"]

PINNED_BASE_IDENTITIES = {
    (
        "sha256:d921452dba64944bf959f22450bb3740f5b2fff4a59faa64bd6b8eaf4c57b5b8",
        "linux",
        "amd64",
    ): {
        "config_digest": (
            "sha256:18da20740c8286c11f78700fe506957a8009f2cc3291c8cc8288454b2cae7511"
        ),
        "manifest_digest": (
            "sha256:62e947ec7edfe308b97cebfab4e89e413c66a63ffcb3c021cb25ff3b70332639"
        ),
    }
}

REVIEWED_BASE_PATHS = frozenset(
    {
        "usr/include/e_scossl.h",
        "usr/include/symcrypt.h",
        "usr/include/symcrypt_internal.h",
        "usr/include/symcrypt_low_level.h",
        "usr/include/symcrypt_no_sal.h",
        "var/cache",
        "var/cache/ldconfig",
        "var/cache/ldconfig/aux-cache",
    }
)
EXACT_BASE_SUBTREES = {
    "var/cache": frozenset(
        {
            "var/cache",
            "var/cache/ldconfig",
            "var/cache/ldconfig/aux-cache",
        }
    )
}

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


class AttestationError(ValueError):
    """Represent one fail-closed pinned-base attestation finding."""

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.finding = _finding(code, message, path=path)


def _sha256_stream(stream: IO[bytes]) -> str:
    """Return a streaming SHA-256 digest with the standard prefix."""
    digest = hashlib.sha256()
    while chunk := stream.read(PRIVATE_KEY_SCAN_CHUNK_BYTES):
        digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _sha256_path(path: Path) -> str:
    """Return a streaming SHA-256 digest for one file."""
    with path.open("rb") as stream:
        return _sha256_stream(stream)


def _require_bounded_archive(path: Path, *, label: str, maximum_bytes: int) -> None:
    """Require one nonempty regular archive within its byte-size safety limit."""
    if path.is_symlink() or not path.is_file():
        raise AttestationError(
            "unsafe_archive_file",
            f"{label} must be a regular non-symlink file",
            path=path.name,
        )
    size = path.stat().st_size
    if size <= 0 or size > maximum_bytes:
        raise AttestationError(
            "archive_size_limit",
            f"{label} is empty or exceeds the byte-size safety limit",
            path=path.name,
        )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting ambiguous duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    """Reject non-standard JSON constants such as NaN and Infinity."""
    raise ValueError(f"non-standard JSON constant: {value}")


def _strict_json_loads(content: bytes) -> object:
    """Parse strict JSON bytes without duplicate keys or non-standard constants."""
    return json.loads(
        content,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_json_constant,
    )


def _bounded_json_document(path: Path) -> dict[str, Any]:
    """Read one bounded JSON object or raise an attestation error."""
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_ATTESTATION_JSON_BYTES + 1)
    except FileNotFoundError as error:
        raise AttestationError(
            "missing_trusted_manifest",
            "trusted manifest is missing",
            path=path.name,
        ) from error
    if len(content) > MAX_ATTESTATION_JSON_BYTES:
        raise AttestationError(
            "malformed_trusted_manifest",
            "trusted manifest exceeds the size limit",
            path=path.name,
        )
    try:
        document = _strict_json_loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AttestationError(
            "malformed_trusted_manifest",
            "trusted manifest is not valid JSON",
            path=path.name,
        ) from error
    if not isinstance(document, dict):
        raise AttestationError(
            "malformed_trusted_manifest",
            "trusted manifest root must be an object",
            path=path.name,
        )
    return document


def _require_object(value: object, label: str) -> dict[str, Any]:
    """Return a JSON object or reject the malformed trusted field."""
    if not isinstance(value, dict):
        raise AttestationError(
            "malformed_trusted_manifest",
            f"trusted manifest {label} must be an object",
        )
    return value


def _require_string(value: object, label: str) -> str:
    """Return a nonempty bounded JSON string or reject it."""
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT_LENGTH:
        raise AttestationError(
            "malformed_trusted_manifest",
            f"trusted manifest {label} must be a bounded string",
        )
    return value


def _require_digest(value: object, label: str) -> str:
    """Return a canonical SHA-256 digest or reject it."""
    digest = _require_string(value, label)
    if SHA256_PATTERN.fullmatch(digest) is None:
        raise AttestationError(
            "malformed_trusted_manifest",
            f"trusted manifest {label} is not a canonical SHA-256 digest",
        )
    return digest


def _trusted_attestation_contract(
    component: Component,
    manifest_path: Path,
) -> dict[str, Any]:
    """Validate and return the narrowly bounded trusted-base contract."""
    document = _bounded_json_document(manifest_path)
    if document.get("schema_version") != 1:
        raise AttestationError(
            "unsupported_trusted_manifest",
            "trusted manifest schema version is unsupported",
        )
    if document.get("policy") != "optima-pinned-runtime-base-v1":
        raise AttestationError(
            "unsupported_trusted_manifest",
            "trusted manifest policy is unsupported",
        )

    components = _require_object(document.get("components"), "components")
    component_contract = _require_object(components.get(component), component)
    expected_dockerfile = _require_string(
        component_contract.get("dockerfile"),
        f"{component} dockerfile",
    )
    expected_tag = _require_string(
        component_contract.get("image_tag"),
        f"{component} image tag",
    )
    if expected_tag != f"optima-{component}:pr":
        raise AttestationError(
            "component_mismatch",
            f"trusted manifest {component} image tag is invalid",
        )

    image = _require_object(document.get("image"), "image")
    index_digest = _require_digest(image.get("index_digest"), "index digest")
    manifest_digest = _require_digest(
        image.get("manifest_digest"),
        "child manifest digest",
    )
    config_digest = _require_digest(image.get("config_digest"), "base config digest")
    reference = _require_string(image.get("reference"), "image reference")
    if reference.rpartition("@")[2] != index_digest:
        raise AttestationError(
            "index_digest_mismatch",
            "trusted image reference does not match the index digest",
        )
    platform = _require_object(image.get("platform"), "image platform")
    expected_os = _require_string(platform.get("os"), "platform OS")
    expected_architecture = _require_string(
        platform.get("architecture"),
        "platform architecture",
    )
    if expected_os != "linux" or expected_architecture != "amd64":
        raise AttestationError(
            "platform_mismatch",
            "trusted platform must be linux/amd64",
        )
    compiled_identity = PINNED_BASE_IDENTITIES.get(
        (index_digest, expected_os, expected_architecture)
    )
    if compiled_identity is None:
        raise AttestationError(
            "unapproved_base_identity",
            "trusted index and platform are not an approved compiled base identity",
        )
    if manifest_digest != compiled_identity["manifest_digest"]:
        raise AttestationError(
            "manifest_digest_mismatch",
            "reviewed child manifest digest does not match the compiled base identity",
        )
    if config_digest != compiled_identity["config_digest"]:
        raise AttestationError(
            "base_config_digest_mismatch",
            "reviewed base config digest does not match the compiled base identity",
        )
    raw_prefix = image.get("rootfs_diff_ids")
    if not isinstance(raw_prefix, list) or len(raw_prefix) != 2:
        raise AttestationError(
            "malformed_trusted_manifest",
            "trusted rootfs DiffID prefix must contain exactly two entries",
        )
    diff_id_prefix = [
        _require_digest(value, f"rootfs DiffID {index}")
        for index, value in enumerate(raw_prefix)
    ]
    if len(set(diff_id_prefix)) != len(diff_id_prefix):
        raise AttestationError(
            "malformed_trusted_manifest",
            "trusted rootfs DiffID prefix contains duplicates",
        )

    raw_entries = document.get("reviewed_entries")
    if not isinstance(raw_entries, list):
        raise AttestationError(
            "malformed_trusted_manifest",
            "trusted reviewed entries must be an array",
        )
    reviewed_entries: dict[str, dict[str, Any]] = {}
    for index, raw_entry in enumerate(raw_entries):
        entry = _require_object(raw_entry, f"reviewed entry {index}")
        path = _require_string(entry.get("path"), f"reviewed entry {index} path")
        normalized, path_error = _normalize_archive_path(path)
        if (
            path_error is not None
            or normalized != path
            or path not in REVIEWED_BASE_PATHS
        ):
            raise AttestationError(
                "unapproved_reviewed_path",
                "trusted manifest contains an unapproved reviewed path",
                path=path,
            )
        if path in reviewed_entries:
            raise AttestationError(
                "duplicate_reviewed_path",
                "trusted manifest contains a duplicate reviewed path",
                path=path,
            )
        entry_type = _require_string(entry.get("type"), f"{path} type")
        expected_type = (
            "directory"
            if path
            in EXACT_BASE_SUBTREES["var/cache"] - {"var/cache/ldconfig/aux-cache"}
            else "file"
        )
        if entry_type != expected_type:
            raise AttestationError(
                "reviewed_type_mismatch",
                "trusted reviewed path has the wrong type",
                path=path,
            )
        for identity_field in ("uid", "gid"):
            identity = entry.get(identity_field)
            if type(identity) is not int or identity < 0:
                raise AttestationError(
                    "malformed_trusted_manifest",
                    f"trusted {path} {identity_field} is invalid",
                    path=path,
                )
        mode = _require_string(entry.get("mode"), f"{path} mode")
        if re.fullmatch(r"0[0-7]{3}", mode) is None:
            raise AttestationError(
                "malformed_trusted_manifest",
                "trusted reviewed mode is invalid",
                path=path,
            )
        if entry_type == "file":
            size = entry.get("size")
            if type(size) is not int or size < 0:
                raise AttestationError(
                    "malformed_trusted_manifest",
                    "trusted reviewed file size is invalid",
                    path=path,
                )
            digest = entry.get("sha256")
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise AttestationError(
                    "malformed_trusted_manifest",
                    "trusted reviewed file hash is invalid",
                    path=path,
                )
        reviewed_entries[path] = entry
    if reviewed_entries.keys() != REVIEWED_BASE_PATHS:
        raise AttestationError(
            "reviewed_inventory_mismatch",
            "trusted manifest must contain exactly the eight reviewed paths",
        )

    subtrees = document.get("exact_subtrees")
    if subtrees != [
        {"entries": sorted(EXACT_BASE_SUBTREES["var/cache"]), "path": "var/cache"}
    ]:
        raise AttestationError(
            "exact_subtree_mismatch",
            "trusted manifest var/cache subtree contract is invalid",
        )
    packages = document.get("packages")
    if not isinstance(packages, list) or not packages:
        raise AttestationError(
            "malformed_trusted_manifest",
            "trusted manifest package provenance is missing",
        )
    for index, raw_package in enumerate(packages):
        package = _require_object(raw_package, f"package {index}")
        _require_string(package.get("name"), f"package {index} name")
        _require_string(package.get("version"), f"package {index} version")
        purl = _require_string(package.get("purl"), f"package {index} purl")
        if not purl.startswith("pkg:rpm/"):
            raise AttestationError(
                "malformed_trusted_manifest",
                "trusted package purl is not an RPM purl",
            )
    return {
        "base_config_digest": config_digest,
        "component_dockerfile": expected_dockerfile,
        "diff_id_prefix": diff_id_prefix,
        "expected_architecture": expected_architecture,
        "expected_os": expected_os,
        "expected_tag": expected_tag,
        "index_digest": index_digest,
        "manifest_digest": manifest_digest,
        "reference": reference,
        "reviewed_entries": reviewed_entries,
    }


def _archive_members(
    archive: tarfile.TarFile, *, label: str
) -> dict[str, tarfile.TarInfo]:
    """Index one tar archive while rejecting unsafe and duplicate entries."""
    members: dict[str, tarfile.TarInfo] = {}
    for index, member in enumerate(archive):
        if index >= MAX_ARCHIVE_ENTRIES:
            raise AttestationError(
                "archive_entry_limit",
                f"{label} exceeds the entry safety limit",
            )
        normalized, path_error = _normalize_archive_path(member.name)
        if path_error is not None or normalized is None:
            raise AttestationError(
                "unsafe_archive_path",
                f"{label} contains {path_error or 'an unsafe archive path'}",
            )
        if normalized in members:
            raise AttestationError(
                "duplicate_archive_entry",
                f"{label} contains a duplicate archive path",
                path=normalized,
            )
        members[normalized] = member
    return members


def _parse_image_config_path(path: str) -> str:
    """Return the digest from one canonical Docker-save config path."""
    legacy_match = re.fullmatch(r"([0-9a-f]{64})\.json", path)
    oci_match = re.fullmatch(r"blobs/sha256/([0-9a-f]{64})", path)
    match = legacy_match or oci_match
    if match is None:
        raise AttestationError(
            "malformed_image_config_path",
            "image-save config path is not a canonical SHA-256 config path",
            path=path,
        )
    return f"sha256:{match.group(1)}"


def _bounded_archive_member_bytes(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    *,
    label: str,
) -> bytes:
    """Read one bounded regular archive member."""
    if not member.isreg() or member.size > MAX_ATTESTATION_JSON_BYTES:
        raise AttestationError(
            "malformed_image_archive",
            f"{label} is not a bounded regular file",
            path=member.name,
        )
    extracted = archive.extractfile(member)
    if extracted is None:
        raise AttestationError(
            "malformed_image_archive",
            f"{label} cannot be read",
            path=member.name,
        )
    return extracted.read()


def _json_archive_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    *,
    label: str,
) -> object:
    """Read one bounded regular JSON member from an archive."""
    content = _bounded_archive_member_bytes(archive, member, label=label)
    try:
        return _strict_json_loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AttestationError(
            "malformed_image_archive",
            f"{label} is not valid JSON",
            path=member.name,
        ) from error


def _protected_ancestors(paths: set[str]) -> set[str]:
    """Return every strict ancestor of the protected reviewed paths."""
    ancestors = {"."}
    for path in paths:
        parent = PurePosixPath(path).parent
        while str(parent) != ".":
            ancestors.add(str(parent))
            parent = parent.parent
    return ancestors


def _whiteout_target(path: str) -> tuple[str | None, bool]:
    """Return a Docker whiteout target and whether it is opaque."""
    pure_path = PurePosixPath(path)
    if pure_path.name == ".wh..wh..opq":
        return str(pure_path.parent), True
    if pure_path.name.startswith(".wh."):
        target_name = pure_path.name.removeprefix(".wh.")
        if target_name:
            return str(pure_path.parent / target_name), False
    return None, False


def _inspect_later_layer(
    stream: IO[bytes],
    *,
    label: str,
    reviewed_paths: set[str],
) -> None:
    """Reject later-layer operations that can modify reviewed base paths."""
    ancestors = _protected_ancestors(reviewed_paths)
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=stream, mode="r|") as layer:
            for index, member in enumerate(layer):
                if index >= MAX_ARCHIVE_ENTRIES:
                    raise AttestationError(
                        "archive_entry_limit",
                        f"{label} exceeds the entry safety limit",
                    )
                normalized, path_error = _normalize_archive_path(member.name)
                if path_error is not None or normalized is None:
                    raise AttestationError(
                        "unsafe_archive_path",
                        f"{label} contains {path_error or 'an unsafe path'}",
                    )
                if normalized in seen:
                    raise AttestationError(
                        "duplicate_archive_entry",
                        f"{label} contains a duplicate path",
                        path=normalized,
                    )
                seen.add(normalized)
                whiteout_target, opaque = _whiteout_target(normalized)
                if whiteout_target is not None and (
                    whiteout_target == "."
                    or any(
                        _starts_with(path, whiteout_target) for path in reviewed_paths
                    )
                ):
                    code = "opaque_reviewed_ancestor" if opaque else "reviewed_whiteout"
                    raise AttestationError(
                        code,
                        "later layer masks a reviewed path or ancestor",
                        path=normalized,
                    )
                if normalized in reviewed_paths:
                    raise AttestationError(
                        "later_layer_reviewed_override",
                        "later layer adds or replaces a reviewed path",
                        path=normalized,
                    )
                if any(
                    _starts_with(normalized, subtree) for subtree in EXACT_BASE_SUBTREES
                ):
                    raise AttestationError(
                        "later_layer_reviewed_subtree_change",
                        "later layer changes an exact reviewed subtree",
                        path=normalized,
                    )
                if normalized in ancestors:
                    raise AttestationError(
                        "later_layer_ancestor_replacement",
                        "later layer restates an ancestor of a reviewed path",
                        path=normalized,
                    )
    except tarfile.TarError as error:
        raise AttestationError(
            "malformed_layer",
            f"{label} is malformed",
        ) from error


def _inspect_saved_image(
    image_archive: Path,
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Verify Docker-save structure, layer bytes, platform, and base ancestry."""
    try:
        _require_bounded_archive(
            image_archive,
            label="image-save archive",
            maximum_bytes=MAX_IMAGE_SAVE_ARCHIVE_BYTES,
        )
        with tarfile.open(image_archive, mode="r:") as archive:
            members = _archive_members(archive, label="image-save archive")
            manifest_member = members.get("manifest.json")
            if manifest_member is None:
                raise AttestationError(
                    "missing_image_manifest",
                    "image-save archive has no manifest.json",
                )
            raw_manifest = _json_archive_member(
                archive,
                manifest_member,
                label="image-save manifest",
            )
            if not isinstance(raw_manifest, list) or len(raw_manifest) != 1:
                raise AttestationError(
                    "image_count_mismatch",
                    "image-save archive must contain exactly one image",
                )
            image_record = _require_object(raw_manifest[0], "image-save record")
            repo_tags = image_record.get("RepoTags")
            if repo_tags != [contract["expected_tag"]]:
                raise AttestationError(
                    "image_tag_mismatch",
                    "image-save archive does not contain the expected image tag",
                )
            config_name = _require_string(
                image_record.get("Config"),
                "image-save config path",
            )
            normalized_config, config_error = _normalize_archive_path(config_name)
            if config_error is not None or normalized_config != config_name:
                raise AttestationError(
                    "unsafe_archive_path",
                    "image-save config path is unsafe",
                )
            expected_config_digest = _parse_image_config_path(config_name)
            config_member = members.get(config_name)
            if config_member is None:
                raise AttestationError(
                    "missing_image_config",
                    "image-save config file is missing",
                    path=config_name,
                )
            config_content = _bounded_archive_member_bytes(
                archive,
                config_member,
                label="image config",
            )
            final_config_digest = f"sha256:{hashlib.sha256(config_content).hexdigest()}"
            if final_config_digest != expected_config_digest:
                raise AttestationError(
                    "config_digest_mismatch",
                    "image config path digest does not match its content digest",
                    path=config_name,
                )
            try:
                raw_config = _strict_json_loads(config_content)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise AttestationError(
                    "malformed_image_archive",
                    "image config is not valid JSON",
                    path=config_name,
                ) from error
            if not isinstance(raw_config, dict):
                raise AttestationError(
                    "malformed_image_config",
                    "image config root must be an object",
                )
            if raw_config.get("os") != contract["expected_os"]:
                raise AttestationError(
                    "os_mismatch",
                    "image config OS does not match trusted platform",
                )
            if raw_config.get("architecture") != contract["expected_architecture"]:
                raise AttestationError(
                    "architecture_mismatch",
                    "image config architecture does not match trusted platform",
                )
            rootfs = _require_object(raw_config.get("rootfs"), "image rootfs")
            if rootfs.get("type") != "layers":
                raise AttestationError(
                    "malformed_image_config",
                    "image rootfs type must be layers",
                )
            raw_diff_ids = rootfs.get("diff_ids")
            raw_layers = image_record.get("Layers")
            if not isinstance(raw_diff_ids, list) or not isinstance(raw_layers, list):
                raise AttestationError(
                    "malformed_image_config",
                    "image layers and DiffIDs must be arrays",
                )
            if len(raw_diff_ids) != len(raw_layers) or not raw_layers:
                raise AttestationError(
                    "layer_count_mismatch",
                    "image layer and DiffID counts do not match",
                )
            diff_ids = [
                _require_digest(value, f"image DiffID {index}")
                for index, value in enumerate(raw_diff_ids)
            ]
            layer_names: list[str] = []
            layer_digests: list[str] = []
            for index, value in enumerate(raw_layers):
                layer_name = _require_string(value, f"image layer {index}")
                normalized_layer, layer_error = _normalize_archive_path(layer_name)
                if layer_error is not None or normalized_layer != layer_name:
                    raise AttestationError(
                        "unsafe_archive_path",
                        "image-save layer path is unsafe",
                    )
                if layer_name in layer_names:
                    raise AttestationError(
                        "duplicate_image_layer",
                        "image-save record contains a duplicate layer",
                        path=layer_name,
                    )
                layer_member = members.get(layer_name)
                if layer_member is None or not layer_member.isreg():
                    raise AttestationError(
                        "missing_image_layer",
                        "image-save layer is missing or not regular",
                        path=layer_name,
                    )
                if layer_member.size <= 0 or layer_member.size > MAX_SAVED_LAYER_BYTES:
                    raise AttestationError(
                        "archive_size_limit",
                        "saved image layer exceeds the byte-size safety limit",
                        path=layer_name,
                    )
                layer_stream = archive.extractfile(layer_member)
                if layer_stream is None:
                    raise AttestationError(
                        "malformed_image_layer",
                        "image-save layer cannot be read",
                        path=layer_name,
                    )
                layer_names.append(layer_name)
                layer_digests.append(_sha256_stream(layer_stream))
            if layer_digests != diff_ids:
                raise AttestationError(
                    "layer_diff_id_mismatch",
                    "saved layer content does not match ordered image DiffIDs",
                )
            prefix = contract["diff_id_prefix"]
            if diff_ids[: len(prefix)] != prefix:
                raise AttestationError(
                    "base_diff_id_mismatch",
                    "ordered image RootFS DiffIDs do not begin with the trusted "
                    "base prefix",
                )
            reviewed_paths = set(contract["reviewed_entries"])
            for layer_index, layer_name in enumerate(
                layer_names[len(prefix) :], len(prefix)
            ):
                layer_member = members[layer_name]
                layer_stream = archive.extractfile(layer_member)
                if layer_stream is None:
                    raise AttestationError(
                        "malformed_image_layer",
                        "later image layer cannot be read",
                        path=layer_name,
                    )
                _inspect_later_layer(
                    layer_stream,
                    label=f"image layer {layer_index}",
                    reviewed_paths=reviewed_paths,
                )
            return {
                "final_config_digest": final_config_digest,
                "final_image_platform": {
                    "architecture": raw_config["architecture"],
                    "os": raw_config["os"],
                },
                "later_layer_protected_paths_absent": True,
                "layer_count": len(layer_names),
                "rootfs_diff_ids": diff_ids,
                "verified_base_rootfs_diff_id_prefix": diff_ids[: len(prefix)],
            }
    except FileNotFoundError as error:
        raise AttestationError(
            "missing_image_archive",
            "image-save archive is missing",
            path=image_archive.name,
        ) from error
    except (OSError, tarfile.TarError) as error:
        raise AttestationError(
            "malformed_image_archive",
            f"image-save archive is malformed: {type(error).__name__}",
        ) from error


def _verify_reviewed_rootfs(
    rootfs_archive: Path,
    reviewed_entries: dict[str, dict[str, Any]],
) -> list[dict[str, object]]:
    """Verify and return exact reviewed rootfs metadata, sizes, and hashes."""
    try:
        _require_bounded_archive(
            rootfs_archive,
            label="exported rootfs",
            maximum_bytes=MAX_ROOTFS_ARCHIVE_BYTES,
        )
        with tarfile.open(rootfs_archive, mode="r:") as archive:
            members = _archive_members(archive, label="exported rootfs")
            verified_entries: list[dict[str, object]] = []
            for path, expected in reviewed_entries.items():
                member = members.get(path)
                if member is None:
                    raise AttestationError(
                        "missing_reviewed_path",
                        "exported rootfs is missing a reviewed path",
                        path=path,
                    )
                expected_type = expected["type"]
                if (expected_type == "file" and not member.isreg()) or (
                    expected_type == "directory" and not member.isdir()
                ):
                    raise AttestationError(
                        "reviewed_type_mismatch",
                        "exported rootfs reviewed path has the wrong type",
                        path=path,
                    )
                if member.uid != expected["uid"]:
                    raise AttestationError(
                        "reviewed_uid_mismatch",
                        "exported rootfs reviewed path has the wrong uid",
                        path=path,
                    )
                if member.gid != expected["gid"]:
                    raise AttestationError(
                        "reviewed_gid_mismatch",
                        "exported rootfs reviewed path has the wrong gid",
                        path=path,
                    )
                if member.mode & 0o7777 != int(expected["mode"], 8):
                    raise AttestationError(
                        "reviewed_mode_mismatch",
                        "exported rootfs reviewed path has the wrong mode",
                        path=path,
                    )
                verified_entry: dict[str, object] = {
                    "gid": member.gid,
                    "mode": f"0{member.mode & 0o7777:03o}",
                    "path": path,
                    "type": expected_type,
                    "uid": member.uid,
                }
                if expected_type == "file":
                    if member.size != expected["size"]:
                        raise AttestationError(
                            "reviewed_size_mismatch",
                            "exported rootfs reviewed file has the wrong size",
                            path=path,
                        )
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise AttestationError(
                            "reviewed_read_error",
                            "exported rootfs reviewed file cannot be read",
                            path=path,
                        )
                    actual_hash = _sha256_stream(extracted).removeprefix("sha256:")
                    if actual_hash != expected["sha256"]:
                        raise AttestationError(
                            "reviewed_hash_mismatch",
                            "exported rootfs reviewed file has the wrong content hash",
                            path=path,
                        )
                    verified_entry.update({"sha256": actual_hash, "size": member.size})
                verified_entries.append(verified_entry)
            for subtree, expected_paths in EXACT_BASE_SUBTREES.items():
                actual_paths = {path for path in members if _starts_with(path, subtree)}
                if actual_paths != expected_paths:
                    raise AttestationError(
                        "reviewed_subtree_mismatch",
                        "exported rootfs reviewed subtree contains unexpected entries",
                        path=subtree,
                    )
            return verified_entries
    except FileNotFoundError as error:
        raise AttestationError(
            "missing_rootfs_archive",
            "exported rootfs archive is missing",
            path=rootfs_archive.name,
        ) from error
    except (OSError, tarfile.TarError) as error:
        raise AttestationError(
            "malformed_rootfs_archive",
            f"exported rootfs archive is malformed: {type(error).__name__}",
        ) from error


def inspect_base_attestation(
    component: Component,
    image_archive: Path,
    rootfs_archive: Path,
    trusted_manifest: Path,
    dockerfile: Path,
) -> dict[str, object]:
    """Attest one final image to the exact trusted base and reviewed rootfs paths."""
    findings: list[dict[str, str]] = []
    image_evidence: dict[str, Any] = {}
    contract: dict[str, Any] = {}
    try:
        contract = _trusted_attestation_contract(component, trusted_manifest)
        if dockerfile.name != contract["component_dockerfile"]:
            raise AttestationError(
                "dockerfile_component_mismatch",
                "Dockerfile path does not match the trusted component",
                path=dockerfile.name,
            )
        try:
            dockerfile_text = dockerfile.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise AttestationError(
                "dockerfile_read_error",
                "Dockerfile cannot be read as UTF-8",
                path=dockerfile.name,
            ) from error
        runtime_from = f"FROM {contract['reference']} AS runtime"
        if dockerfile_text.splitlines().count(runtime_from) != 1:
            raise AttestationError(
                "dockerfile_digest_mismatch",
                "Dockerfile does not contain exactly one trusted runtime FROM",
                path=dockerfile.name,
            )
        image_evidence = _inspect_saved_image(image_archive, contract)
        image_evidence["verified_reviewed_entries"] = _verify_reviewed_rootfs(
            rootfs_archive,
            contract["reviewed_entries"],
        )
    except AttestationError as error:
        findings.append(error.finding)

    manifest_digest = None
    rootfs_digest = None
    try:
        manifest_digest = _sha256_path(trusted_manifest)
    except OSError:
        pass
    try:
        _require_bounded_archive(
            rootfs_archive,
            label="exported rootfs",
            maximum_bytes=MAX_ROOTFS_ARCHIVE_BYTES,
        )
        rootfs_digest = _sha256_path(rootfs_archive)
    except (AttestationError, OSError):
        pass
    evidence: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "policy": "optima-pinned-runtime-base-attestation-v1",
        "component": component,
        "status": "fail" if findings else "pass",
        "trusted_manifest": trusted_manifest.name,
        "trusted_manifest_sha256": manifest_digest,
        "rootfs_archive": rootfs_archive.name,
        "rootfs_archive_sha256": rootfs_digest,
        "image_archive": image_archive.name,
        "reviewed_path_count": len(REVIEWED_BASE_PATHS),
        "attested_paths": sorted(REVIEWED_BASE_PATHS),
        "findings": findings,
    }
    if contract:
        evidence.update(
            {
                "base_index_digest": contract["index_digest"],
                "base_reference": contract["reference"],
                "reviewed_base_config_digest": contract["base_config_digest"],
                "reviewed_base_manifest_digest": contract["manifest_digest"],
            }
        )
    evidence.update(image_evidence)
    return evidence


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
        purl_name = ""
        purl_version = ""
        if isinstance(purl, str) and purl.startswith("pkg:pypi/"):
            purl_identity = purl.removeprefix("pkg:pypi/").split("?", maxsplit=1)[0]
            purl_identity = purl_identity.split("#", maxsplit=1)[0]
            encoded_name, separator, encoded_version = purl_identity.rpartition("@")
            if separator:
                purl_name = unquote(encoded_name)
                purl_version = unquote(encoded_version)
        if (
            isinstance(name, str)
            and isinstance(version, str)
            and isinstance(purl, str)
            and _canonical_distribution_name(purl_name)
            == _canonical_distribution_name(name)
            and purl_version == version
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


def _pep3147_bytecode_path(source_path: str, site_packages_root: str) -> str | None:
    """Derive the active CPython cache path for one wheel-owned Python source."""
    python_directory = PurePosixPath(site_packages_root).parts[3]
    version_match = re.fullmatch(r"python(\d+)\.(\d+)", python_directory)
    source = PurePosixPath(source_path)
    if version_match is None or source.suffix != ".py":
        return None
    cache_tag = f"cpython-{version_match.group(1)}{version_match.group(2)}"
    return str(source.parent / "__pycache__" / f"{source.stem}.{cache_tag}.pyc")


def _validated_third_party_distribution_paths(
    archive: tarfile.TarFile,
    component: Component,
) -> set[str]:
    """Return files and directories proven to belong to non-OPTIMA wheels."""
    regular_members: dict[str, tarfile.TarInfo] = {}
    directory_paths: set[str] = set()
    record_candidates: list[tuple[str, str, tarfile.TarInfo]] = []
    for index, member in enumerate(archive):
        if index >= MAX_ARCHIVE_ENTRIES:
            return set()
        normalized, error = _normalize_archive_path(member.name)
        if error is not None or normalized is None:
            continue
        if member.isdir():
            directory_paths.add(normalized)
            continue
        if not member.isreg():
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
            bytecode_path = _pep3147_bytecode_path(distribution_file, root)
            if bytecode_path is None or bytecode_path not in regular_members:
                continue
            owned_paths.add(bytecode_path)
            cache_directory = str(PurePosixPath(bytecode_path).parent)
            if cache_directory in directory_paths:
                owned_paths.add(cache_directory)
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
    if (
        _starts_with(path.casefold(), "app")
        and (
            any(part in {"test", "tests"} for part in parts)
            or filename == "conftest.py"
        )
    ) or (
        not third_party_site_package
        and (
            _starts_with(path.casefold(), "app/tests")
            or (_starts_with(path.casefold(), "app") and "tests" in parts)
            or (
                _starts_with(path.casefold(), "app")
                and filename.startswith("test_")
                and filename.endswith(".py")
            )
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
    if (
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


def _attested_rootfs_paths(
    component: Component,
    archive_path: Path,
    attestation_path: Path,
) -> tuple[set[str], dict[str, object]]:
    """Validate an attestation summary against this exact rootfs archive."""
    try:
        with attestation_path.open("rb") as stream:
            content = stream.read(MAX_ATTESTATION_JSON_BYTES + 1)
    except FileNotFoundError as error:
        raise AttestationError(
            "missing_base_attestation",
            "base attestation summary is missing",
            path=attestation_path.name,
        ) from error
    if len(content) > MAX_ATTESTATION_JSON_BYTES:
        raise AttestationError(
            "malformed_base_attestation",
            "base attestation summary exceeds the size limit",
        )
    try:
        evidence = _strict_json_loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AttestationError(
            "malformed_base_attestation",
            "base attestation summary is not valid JSON",
        ) from error
    if not isinstance(evidence, dict):
        raise AttestationError(
            "malformed_base_attestation",
            "base attestation summary root must be an object",
        )
    if (
        evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or evidence.get("policy") != "optima-pinned-runtime-base-attestation-v1"
        or evidence.get("status") != "pass"
        or evidence.get("component") != component
    ):
        raise AttestationError(
            "base_attestation_mismatch",
            "base attestation summary is not a passing component match",
        )
    if evidence.get("rootfs_archive_sha256") != _sha256_path(archive_path):
        raise AttestationError(
            "base_attestation_rootfs_mismatch",
            "base attestation does not bind this exported rootfs",
        )
    expected_paths = sorted(REVIEWED_BASE_PATHS)
    verified_entries = evidence.get("verified_reviewed_entries")
    verified_paths = (
        [entry.get("path") for entry in verified_entries]
        if isinstance(verified_entries, list)
        and all(isinstance(entry, dict) for entry in verified_entries)
        else None
    )
    if (
        evidence.get("attested_paths") != expected_paths
        or evidence.get("reviewed_path_count") != len(expected_paths)
        or evidence.get("findings") != []
        or verified_paths != expected_paths
    ):
        raise AttestationError(
            "base_attestation_inventory_mismatch",
            "base attestation does not contain exactly the reviewed path inventory",
        )
    final_config_digest = evidence.get("final_config_digest")
    final_platform = evidence.get("final_image_platform")
    rootfs_diff_ids = evidence.get("rootfs_diff_ids")
    verified_prefix = evidence.get("verified_base_rootfs_diff_id_prefix")
    complete_derived_facts = (
        isinstance(final_config_digest, str)
        and SHA256_PATTERN.fullmatch(final_config_digest) is not None
        and final_platform == {"architecture": "amd64", "os": "linux"}
        and isinstance(rootfs_diff_ids, list)
        and isinstance(verified_prefix, list)
        and len(verified_prefix) == 2
        and rootfs_diff_ids[:2] == verified_prefix
        and all(
            isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None
            for value in rootfs_diff_ids
        )
        and evidence.get("later_layer_protected_paths_absent") is True
    )
    if not complete_derived_facts:
        raise AttestationError(
            "base_attestation_derived_facts_mismatch",
            "base attestation is missing complete archive-derived facts",
        )
    assert isinstance(verified_entries, list)
    for entry in verified_entries:
        assert isinstance(entry, dict)
        required = {"gid", "mode", "path", "type", "uid"}
        if (
            not required <= entry.keys()
            or type(entry["uid"]) is not int
            or entry["uid"] < 0
            or type(entry["gid"]) is not int
            or entry["gid"] < 0
            or not isinstance(entry["mode"], str)
            or re.fullmatch(r"0[0-7]{3}", entry["mode"]) is None
            or entry["type"] not in {"directory", "file"}
        ):
            raise AttestationError(
                "base_attestation_derived_facts_mismatch",
                "base attestation reviewed metadata is incomplete",
            )
        if entry["type"] == "file" and (
            type(entry.get("size")) is not int
            or entry["size"] < 0
            or not isinstance(entry.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
        ):
            raise AttestationError(
                "base_attestation_derived_facts_mismatch",
                "base attestation reviewed file facts are incomplete",
            )
    identity = {
        key: evidence.get(key)
        for key in (
            "base_index_digest",
            "reviewed_base_config_digest",
            "reviewed_base_manifest_digest",
            "trusted_manifest_sha256",
        )
    }
    if any(
        not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None
        for value in identity.values()
    ):
        raise AttestationError(
            "base_attestation_identity_mismatch",
            "base attestation is missing a canonical trusted identity",
        )
    return set(expected_paths), identity


def inspect_rootfs(
    component: Component,
    archive_path: Path,
    *,
    attestation_path: Path | None = None,
) -> dict[str, object]:
    """Inspect one exported rootfs tar without extracting archive members."""
    findings: list[dict[str, str]] = []
    finding_total = [0]
    attested_paths: set[str] = set()
    attestation_identity: dict[str, object] | None = None
    if attestation_path is not None:
        try:
            attested_paths, attestation_identity = _attested_rootfs_paths(
                component,
                archive_path,
                attestation_path,
            )
        except (AttestationError, OSError) as error:
            finding = (
                error.finding
                if isinstance(error, AttestationError)
                else _finding(
                    "base_attestation_read_error",
                    f"base attestation binding failed: {type(error).__name__}",
                )
            )
            _append_finding(findings, finding, finding_total)
    paths: set[str] = set()
    file_entries: set[str] = set()
    regular_files: set[str] = set()
    entry_counts: Counter[str] = Counter()
    seen_paths: dict[str, tuple[bytes, int, str]] = {}
    test_roots: set[str] = set()
    conftest_files: set[str] = set()
    build_manifests: set[str] = set()

    try:
        _require_bounded_archive(
            archive_path,
            label="exported rootfs",
            maximum_bytes=MAX_ROOTFS_ARCHIVE_BYTES,
        )
        with tarfile.open(archive_path, mode="r:") as archive:
            third_party_distribution_paths = _validated_third_party_distribution_paths(
                archive,
                component,
            )
        with tarfile.open(archive_path, mode="r:") as archive:
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
                normalized_path = PurePosixPath(normalized)
                normalized_parts = tuple(
                    part.casefold() for part in normalized_path.parts
                )
                for part_index, part in enumerate(normalized_parts):
                    if part in {"test", "tests"} and normalized_parts[0] == "app":
                        test_roots.add(
                            "/".join(normalized_path.parts[: part_index + 1])
                        )
                if normalized_parts and normalized_parts[0] == "app":
                    filename = normalized_path.name.casefold()
                    if filename == "conftest.py":
                        conftest_files.add(normalized)
                    if filename in {
                        "pyproject.toml",
                        "uv.lock",
                        "dockerfile",
                    } or filename.startswith("dockerfile."):
                        build_manifests.add(normalized)

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
                attested_base_finding = (
                    normalized in attested_paths
                    and path_finding is not None
                    and path_finding["code"]
                    in {"build_header", "package_manager_cache"}
                )
                if path_finding is not None and not attested_base_finding:
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
    except AttestationError as error:
        _append_finding(findings, error.finding, finding_total)
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
    evidence: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "policy": "optima-final-rootfs-v1",
        "component": component,
        "archive": archive_path.name,
        "status": "pass" if finding_total[0] == 0 else "fail",
        "counts": {
            "directories": entry_counts["directories"],
            "entries": entry_counts["entries"],
            "findings": finding_total[0],
            "build_manifests": len(build_manifests),
            "conftest_files": len(conftest_files),
            "links": entry_counts["links"],
            "regular_files": entry_counts["regular_files"],
            "retained_findings": len(findings),
            "test_roots": len(test_roots),
            "unsafe_entry_types": entry_counts["unsafe_entry_types"],
        },
        "findings": findings,
    }
    if attestation_identity is not None:
        evidence["base_attestation"] = attestation_identity
    return evidence


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


def _attestation_console_summary(evidence: dict[str, object]) -> str:
    """Return a bounded pinned-base attestation summary for console logs."""
    component = evidence["component"]
    status = str(evidence["status"]).upper()
    findings = evidence["findings"]
    messages: list[str] = []
    if isinstance(findings, list):
        for finding in findings[:8]:
            if isinstance(finding, dict):
                message = finding.get("message", "attestation finding")
                path = finding.get("path")
                messages.append(f"{message}{f' ({path})' if path else ''}")
    suffix = f": {'; '.join(messages)}" if messages else ""
    return _bounded_text(f"base attestation {component}: {status}{suffix}")


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
    rootfs_parser.add_argument("--attestation", type=Path)
    rootfs_parser.add_argument("--output", type=Path, required=True)

    attestation_parser = subparsers.add_parser(
        "attest-base",
        help="attest one saved image and exported rootfs to the pinned runtime base",
    )
    attestation_parser.add_argument(
        "--component",
        choices=("api", "ui"),
        required=True,
    )
    attestation_parser.add_argument("--image-archive", type=Path, required=True)
    attestation_parser.add_argument("--rootfs-archive", type=Path, required=True)
    attestation_parser.add_argument("--trusted-manifest", type=Path, required=True)
    attestation_parser.add_argument("--dockerfile", type=Path, required=True)
    attestation_parser.add_argument("--output", type=Path, required=True)

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
        evidence = inspect_rootfs(
            component,
            arguments.archive,
            attestation_path=arguments.attestation,
        )
        _write_evidence(arguments.output, evidence)
        print(_rootfs_console_summary(evidence))
        return EXIT_SUCCESS if evidence["status"] == "pass" else EXIT_FAILURE

    if arguments.command == "attest-base":
        attestation_component: Component = arguments.component
        attestation = inspect_base_attestation(
            attestation_component,
            arguments.image_archive,
            arguments.rootfs_archive,
            arguments.trusted_manifest,
            arguments.dockerfile,
        )
        _write_evidence(arguments.output, attestation)
        print(_attestation_console_summary(attestation))
        return EXIT_SUCCESS if attestation["status"] == "pass" else EXIT_FAILURE

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
