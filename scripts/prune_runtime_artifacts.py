"""Prune wheel-owned test and reviewed build artifacts from a virtual environment."""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import io
import json
import re
import stat
import sys
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
MAX_METADATA_BYTES = 1024 * 1024
MAX_WHEEL_BYTES = 128 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
EXPECTED_CACHE_TAG = "cpython-312"
REVIEWED_BUILD_MANIFESTS = frozenset({("pandas", "pandas/pyproject.toml")})
REVIEWED_CONFTESTS = frozenset(
    {
        ("numpy", "numpy/conftest.py"),
        ("pandas", "pandas/conftest.py"),
        ("pyarrow", "pyarrow/conftest.py"),
    }
)
REVIEWED_TEST_ROOTS: dict[str, dict[str, frozenset[str]]] = {
    "certifi": {"certifi/tests": frozenset({".py"})},
    "gitdb": {"gitdb/test": frozenset({".py"})},
    "jsonschema": {"jsonschema/tests": frozenset({".py"})},
    "jsonschema-specifications": {
        "jsonschema_specifications/tests": frozenset({".py"})
    },
    "numpy": {
        "numpy/_core/tests": frozenset(
            {
                ".build",
                ".c",
                ".cpp",
                ".csv",
                ".fits",
                ".pkl",
                ".py",
                ".pyx",
                ".txt",
            }
        ),
        "numpy/_pyinstaller/tests": frozenset({".py"}),
        "numpy/f2py/tests": frozenset(
            {
                ".c",
                ".f",
                ".f2py_f2cmap",
                ".f90",
                ".f95",
                ".inc",
                ".py",
                ".pyf",
            }
        ),
        "numpy/fft/tests": frozenset({".py"}),
        "numpy/lib/tests": frozenset({".npy", ".npz", ".py"}),
        "numpy/linalg/tests": frozenset({".py"}),
        "numpy/ma/tests": frozenset({".py"}),
        "numpy/matrixlib/tests": frozenset({".py"}),
        "numpy/polynomial/tests": frozenset({".py"}),
        "numpy/random/tests": frozenset({".csv", ".gz", ".py"}),
        "numpy/testing/tests": frozenset({".py"}),
        "numpy/tests": frozenset({".py"}),
        "numpy/typing/tests": frozenset({".ini", ".py", ".pyi"}),
    },
    "pandas": {"pandas/tests": frozenset({".py"})},
    "pyarrow": {
        "pyarrow/tests": frozenset(
            {".feather", ".gz", ".md", ".orc", ".parquet", ".py", ".pyx"}
        )
    },
    "referencing": {"referencing/tests": frozenset({".py"})},
    "smmap": {"smmap/test": frozenset({".py"})},
    "tornado": {
        "tornado/test": frozenset(
            {
                ".bz2",
                ".cfg",
                ".crt",
                ".csv",
                ".gz",
                ".html",
                ".key",
                ".mo",
                ".po",
                ".py",
                ".txt",
                ".xml",
            }
        )
    },
}
BUILD_MANIFEST_NAMES = frozenset({"pyproject.toml", "uv.lock"})
PROTECTED_TEST_DIRECTORIES = frozenset({".libs", "headers", "include", "licenses"})
PROTECTED_TEST_SUFFIXES = frozenset(
    {".a", ".dll", ".dylib", ".h", ".hh", ".hpp", ".hxx", ".lib", ".so"}
)
PROTECTED_TEST_FILENAMES = frozenset(
    {"direct_url.json", "entry_points.txt", "metadata", "top_level.txt", "wheel"}
)


class PruneError(Exception):
    """Raised when an environment cannot be pruned without weakening provenance."""


@dataclass(frozen=True)
class RecordEntry:
    """One validated wheel RECORD row."""

    raw_path: str
    hash_field: str
    size_field: str
    path: Path


@dataclass(frozen=True)
class Distribution:
    """One validated installed wheel distribution."""

    canonical_name: str
    version: str
    site_packages: Path
    info_directory: Path
    record_path: Path
    entries: tuple[RecordEntry, ...]


@dataclass(frozen=True)
class PrunePlan:
    """Fully validated mutations for one environment."""

    distributions: tuple[Distribution, ...]
    deleted_paths: frozenset[Path]
    empty_directory_targets: frozenset[Path]
    rewritten_records: dict[Path, str]


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PruneError(f"{label} is missing or is not a regular file: {path}")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise PruneError(f"{label} exceeds its {limit}-byte safety limit: {path}")
    return content


def _single_header(content: bytes, field: str, label: str) -> str:
    try:
        message = BytesParser(policy=default).parsebytes(content, headersonly=True)
    except (TypeError, ValueError) as error:
        raise PruneError(f"malformed {label}") from error
    values = message.get_all(field, [])
    if len(values) != 1 or not str(values[0]).strip():
        raise PruneError(f"{label} must contain exactly one {field} field")
    return str(values[0]).strip()


def _validate_metadata(info_directory: Path) -> tuple[str, str]:
    content = _read_bounded(
        info_directory / "METADATA", MAX_METADATA_BYTES, "distribution METADATA"
    )
    return (
        _single_header(content, "Name", "distribution METADATA"),
        _single_header(content, "Version", "distribution METADATA"),
    )


def _validate_wheel(info_directory: Path) -> None:
    content = _read_bounded(
        info_directory / "WHEEL", MAX_WHEEL_BYTES, "distribution WHEEL"
    )
    if _single_header(content, "Wheel-Version", "distribution WHEEL") != "1.0":
        raise PruneError(f"unsupported Wheel-Version in {info_directory / 'WHEEL'}")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _record_target(environment_root: Path, site_packages: Path, value: str) -> Path:
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or "//" in value
        or value.endswith("/")
    ):
        raise PruneError(f"malformed RECORD path: {value!r}")
    relative_site_packages = site_packages.relative_to(environment_root)
    parts = list(relative_site_packages.parts)
    for part in value.split("/"):
        if part in {"", "."}:
            raise PruneError(f"malformed RECORD path: {value!r}")
        if part == "..":
            if not parts:
                raise PruneError(f"RECORD path escapes environment root: {value!r}")
            parts.pop()
        else:
            parts.append(part)
    target = environment_root.joinpath(*parts)
    if not _is_relative_to(target, environment_root):
        raise PruneError(f"RECORD path escapes environment root: {value!r}")
    return target


def _assert_no_symlink(path: Path, environment_root: Path) -> None:
    relative = path.relative_to(environment_root)
    current = environment_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise PruneError(f"refusing to follow symlink: {current}")


def _decode_sha256(value: str, record_path: Path) -> bytes:
    algorithm, separator, encoded = value.partition("=")
    if separator != "=" or algorithm != "sha256" or not encoded:
        raise PruneError(f"unsupported RECORD hash in {record_path}")
    if len(encoded) != 43 or re.fullmatch(r"[A-Za-z0-9_-]{43}", encoded) is None:
        raise PruneError(f"malformed sha256 RECORD hash in {record_path}")
    try:
        digest = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, binascii.Error) as error:
        raise PruneError(f"malformed sha256 RECORD hash in {record_path}") from error
    if len(digest) != hashlib.sha256().digest_size:
        raise PruneError(f"malformed sha256 RECORD hash in {record_path}")
    canonical = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    if encoded != canonical:
        raise PruneError(f"non-canonical sha256 RECORD hash in {record_path}")
    return digest


def _parse_record(
    environment_root: Path,
    site_packages: Path,
    record_path: Path,
) -> tuple[RecordEntry, ...]:
    content = _read_bounded(record_path, MAX_RECORD_BYTES, "distribution RECORD")
    try:
        rows = csv.reader(io.StringIO(content.decode("utf-8"), newline=""), strict=True)
        entries: list[RecordEntry] = []
        seen: set[Path] = set()
        for row in rows:
            if len(row) != 3:
                raise PruneError(f"malformed CSV row in {record_path}")
            target = _record_target(environment_root, site_packages, row[0])
            if target in seen:
                raise PruneError(f"duplicate RECORD path in {record_path}: {row[0]}")
            seen.add(target)
            if target == record_path:
                if row[1] or row[2]:
                    raise PruneError(
                        f"RECORD self row must have blank hash and size: {record_path}"
                    )
            elif not row[1] or not row[2]:
                raise PruneError(
                    f"RECORD path requires sha256 hash and exact size: {row[0]}"
                )
            if row[2] and re.fullmatch(r"0|[1-9][0-9]*", row[2]) is None:
                raise PruneError(f"malformed RECORD size in {record_path}: {row[0]}")
            if row[1]:
                _decode_sha256(row[1], record_path)
            entries.append(RecordEntry(row[0], row[1], row[2], target))
    except (csv.Error, UnicodeDecodeError) as error:
        raise PruneError(f"malformed CSV in {record_path}") from error
    self_rows = [entry for entry in entries if entry.path == record_path]
    if len(self_rows) != 1 or self_rows[0].hash_field or self_rows[0].size_field:
        raise PruneError(f"RECORD must contain one unhashed self row: {record_path}")
    return tuple(entries)


def _distribution_directory_identity(name: str) -> tuple[str, str]:
    stem = name.removesuffix(".dist-info")
    distribution, separator, version = stem.rpartition("-")
    if not separator or not distribution or not version:
        raise PruneError(f"malformed dist-info directory name: {name}")
    return _canonical_distribution_name(distribution), version


def _site_packages_directories(environment_root: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for library_name in ("lib", "lib64"):
        library = environment_root / library_name
        if not library.exists() or library.is_symlink() or not library.is_dir():
            continue
        for python_directory in sorted(library.iterdir(), key=lambda path: path.name):
            site_packages = python_directory / "site-packages"
            if (
                python_directory.name.startswith("python")
                and not python_directory.is_symlink()
                and site_packages.is_dir()
                and not site_packages.is_symlink()
            ):
                candidates.append(site_packages)
    windows_site_packages = environment_root / "Lib" / "site-packages"
    if windows_site_packages.is_dir() and not windows_site_packages.is_symlink():
        candidates.append(windows_site_packages)
    unique = tuple(dict.fromkeys(candidates))
    if not unique:
        raise PruneError("environment contains no supported site-packages directory")
    return unique


def _load_distributions(environment_root: Path) -> tuple[Distribution, ...]:
    distributions: list[Distribution] = []
    identities: set[tuple[str, str]] = set()
    for site_packages in _site_packages_directories(environment_root):
        for info_directory in sorted(site_packages.glob("*.dist-info")):
            _assert_no_symlink(info_directory, environment_root)
            if not info_directory.is_dir():
                raise PruneError(f"dist-info path is not a directory: {info_directory}")
            for signature_name in ("RECORD.jws", "RECORD.p7s"):
                if (info_directory / signature_name).exists():
                    raise PruneError(
                        f"signed RECORD cannot be rewritten: {info_directory}"
                    )
            directory_name, directory_version = _distribution_directory_identity(
                info_directory.name
            )
            metadata_name, metadata_version = _validate_metadata(info_directory)
            canonical_name = _canonical_distribution_name(metadata_name)
            if (
                canonical_name != directory_name
                or metadata_version != directory_version
            ):
                raise PruneError(f"dist-info identity mismatch: {info_directory}")
            identity = (canonical_name, metadata_version)
            if identity in identities:
                raise PruneError(
                    "duplicate distribution identity across site-packages roots: "
                    f"{canonical_name}=={metadata_version}"
                )
            identities.add(identity)
            _validate_wheel(info_directory)
            record_path = info_directory / "RECORD"
            entries = _parse_record(environment_root, site_packages, record_path)
            required = {
                info_directory / "METADATA",
                info_directory / "WHEEL",
                record_path,
            }
            if not required <= {entry.path for entry in entries}:
                raise PruneError(f"RECORD omits required wheel metadata: {record_path}")
            distributions.append(
                Distribution(
                    canonical_name,
                    metadata_version,
                    site_packages,
                    info_directory,
                    record_path,
                    entries,
                )
            )
    return tuple(distributions)


def _verify_entry(
    entry: RecordEntry,
    distribution: Distribution,
    environment_root: Path,
) -> None:
    if entry.path == distribution.record_path:
        return
    _assert_no_symlink(entry.path, environment_root)
    try:
        mode = entry.path.stat().st_mode
    except FileNotFoundError as error:
        raise PruneError(
            f"retained RECORD path is missing: {entry.raw_path}"
        ) from error
    if not stat.S_ISREG(mode):
        raise PruneError(f"RECORD path is not a regular file: {entry.raw_path}")
    if entry.path.stat().st_size != int(entry.size_field):
        raise PruneError(f"RECORD size mismatch: {entry.raw_path}")
    expected = _decode_sha256(entry.hash_field, distribution.record_path)
    digest = hashlib.sha256()
    with entry.path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.digest() != expected:
        raise PruneError(f"RECORD sha256 mismatch: {entry.raw_path}")


def _relative_wheel_path(distribution: Distribution, path: Path) -> str | None:
    try:
        return path.relative_to(distribution.site_packages).as_posix()
    except ValueError:
        return None


def _test_root(path: Path, site_packages: Path) -> str | None:
    try:
        relative = path.relative_to(site_packages)
    except ValueError:
        return None
    for index, part in enumerate(relative.parts):
        if part.casefold() in {"test", "tests"}:
            return Path(*relative.parts[: index + 1]).as_posix()
    return None


def _artifact_kind(path: Path, site_packages: Path) -> str | None:
    try:
        path.relative_to(site_packages)
    except ValueError:
        return None
    if _test_root(path, site_packages) is not None:
        return "test tree"
    filename = path.name.casefold()
    if filename == "conftest.py":
        return "conftest.py"
    if (
        filename in BUILD_MANIFEST_NAMES
        or filename.startswith("dockerfile.")
        or filename == "dockerfile"
    ):
        return "build manifest"
    return None


def _reviewed_test_extensions(
    distribution: Distribution,
    path: Path,
) -> frozenset[str] | None:
    relative = _relative_wheel_path(distribution, path)
    if relative is None:
        return None
    root = _test_root(path, distribution.site_packages)
    if root is None:
        return None
    return REVIEWED_TEST_ROOTS.get(distribution.canonical_name, {}).get(root)


def _is_protected_test_path(
    path: Path,
    site_packages: Path,
    test_root: str,
) -> bool:
    relative = path.relative_to(site_packages / Path(test_root))
    parts = tuple(part.casefold() for part in relative.parts)
    filename = path.name.casefold()
    return (
        any(part in PROTECTED_TEST_DIRECTORIES for part in parts[:-1])
        or path.suffix.casefold() in PROTECTED_TEST_SUFFIXES
        or filename in PROTECTED_TEST_FILENAMES
        or filename.startswith(("license", "copying", "notice"))
    )


def _test_file_class(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix:
        return suffix
    filename = path.name.casefold()
    if filename in {".build", ".f2py_f2cmap"}:
        return filename
    return ""


def _validate_reviewed_target(
    distribution: Distribution,
    entry: RecordEntry,
    kind: str,
) -> bool:
    relative = _relative_wheel_path(distribution, entry.path)
    if relative is None:
        raise PruneError(f"artifact is outside site-packages: {entry.raw_path}")
    if distribution.canonical_name == "optima":
        raise PruneError(
            f"OPTIMA distribution artifact is never pruned: {entry.raw_path}"
        )
    if kind == "test tree":
        if entry.path.suffix.casefold() == ".pyc":
            return False
        allowed_extensions = _reviewed_test_extensions(distribution, entry.path)
        if allowed_extensions is None:
            raise PruneError(f"unreviewed distribution or test root: {entry.raw_path}")
        test_root = _test_root(entry.path, distribution.site_packages)
        if (
            test_root is None
            or _is_protected_test_path(
                entry.path, distribution.site_packages, test_root
            )
            or _test_file_class(entry.path) not in allowed_extensions
            or entry.path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        ):
            raise PruneError(
                f"unsafe file class in reviewed test root: {entry.raw_path}"
            )
        return True
    if kind == "conftest.py":
        if (distribution.canonical_name, relative) not in REVIEWED_CONFTESTS:
            raise PruneError(f"unreviewed wheel conftest.py: {entry.raw_path}")
        return True
    if (distribution.canonical_name, relative) not in REVIEWED_BUILD_MANIFESTS:
        raise PruneError(f"unreviewed wheel build manifest: {entry.raw_path}")
    return True


def _iter_tree(root: Path, environment_root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(
            directory.iterdir(), key=lambda item: item.name, reverse=True
        ):
            if path.is_symlink():
                if _artifact_kind(path, root) is not None:
                    raise PruneError(f"prunable path is a symlink: {path}")
                continue
            _assert_no_symlink(path, environment_root)
            paths.append(path)
            if path.is_dir():
                pending.append(path)
    return tuple(paths)


def _derived_bytecode_path(source: Path) -> Path:
    return source.parent / "__pycache__" / f"{source.stem}.{EXPECTED_CACHE_TAG}.pyc"


def _record_text(entries: tuple[RecordEntry, ...]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for entry in sorted(entries, key=lambda item: item.raw_path):
        writer.writerow((entry.raw_path, entry.hash_field, entry.size_field))
    return output.getvalue()


def _build_plan(environment_root: Path) -> PrunePlan:
    if sys.implementation.cache_tag != EXPECTED_CACHE_TAG:
        raise PruneError(
            "runtime artifact pruning requires active cache tag "
            f"{EXPECTED_CACHE_TAG}, got {sys.implementation.cache_tag!r}"
        )
    distributions = _load_distributions(environment_root)
    owners: dict[Path, Distribution] = {}
    for distribution in distributions:
        for entry in distribution.entries:
            prior_owner = owners.get(entry.path)
            if prior_owner is not None and prior_owner != distribution:
                raise PruneError(f"conflicting RECORD ownership: {entry.raw_path}")
            owners[entry.path] = distribution
            _verify_entry(entry, distribution, environment_root)

    initial_targets: set[Path] = set()
    deferred_record_bytecode: set[Path] = set()
    for distribution in distributions:
        for entry in distribution.entries:
            kind = _artifact_kind(entry.path, distribution.site_packages)
            if kind is None:
                continue
            if _validate_reviewed_target(distribution, entry, kind):
                initial_targets.add(entry.path)
            else:
                deferred_record_bytecode.add(entry.path)

    derived_targets: set[Path] = set()
    derived_owners: dict[Path, Distribution] = {}
    cache_sources: dict[Path, set[Distribution]] = {}
    for source in initial_targets:
        if source.suffix != ".py":
            continue
        distribution = owners[source]
        cache_directory = source.parent / "__pycache__"
        cache_sources.setdefault(cache_directory, set()).add(distribution)
        bytecode = _derived_bytecode_path(source)
        if not bytecode.exists() and not bytecode.is_symlink():
            continue
        _assert_no_symlink(bytecode, environment_root)
        if not bytecode.is_file():
            raise PruneError(f"derived bytecode is not a regular file: {bytecode}")
        bytecode_owner = owners.get(bytecode)
        if bytecode_owner is not None and bytecode_owner != distribution:
            raise PruneError(f"derived bytecode has conflicting owner: {bytecode}")
        derived_targets.add(bytecode)
        derived_owners[bytecode] = distribution

    unresolved_bytecode = deferred_record_bytecode - derived_targets
    if unresolved_bytecode:
        path = min(unresolved_bytecode, key=str)
        raise PruneError(
            f"RECORD-owned bytecode lacks a validated removable source: {path}"
        )

    empty_directory_targets: set[Path] = set()
    validated_cache_directories: set[Path] = set()
    for cache_directory, source_owners in cache_sources.items():
        if not cache_directory.exists() and not cache_directory.is_symlink():
            continue
        _assert_no_symlink(cache_directory, environment_root)
        if not cache_directory.is_dir():
            raise PruneError(
                f"bytecode cache path is not a directory: {cache_directory}"
            )
        children = tuple(cache_directory.iterdir())
        if len(source_owners) == 1 and all(
            child in derived_targets
            and derived_owners.get(child) in source_owners
            and not child.is_symlink()
            for child in children
        ):
            validated_cache_directories.add(cache_directory)
            empty_directory_targets.add(cache_directory)
    deleted_paths = initial_targets | derived_targets

    for site_packages in _site_packages_directories(environment_root):
        reviewed_roots = {
            site_packages / Path(root)
            for distribution in distributions
            if distribution.site_packages == site_packages
            for root in REVIEWED_TEST_ROOTS.get(distribution.canonical_name, {})
        }
        for path in _iter_tree(site_packages, environment_root):
            kind = _artifact_kind(path, site_packages)
            if kind is None:
                continue
            matching_root = next(
                (root for root in reviewed_roots if _is_relative_to(path, root)),
                None,
            )
            if kind == "test tree" and matching_root is None:
                raise PruneError(f"unreviewed distribution or test root: {path}")
            if path.is_dir():
                if matching_root is not None and any(
                    part.casefold() in PROTECTED_TEST_DIRECTORIES
                    for part in path.relative_to(matching_root).parts
                ):
                    raise PruneError(f"unsafe directory in reviewed test root: {path}")
                if (
                    path.name == "__pycache__"
                    and path not in validated_cache_directories
                ):
                    raise PruneError(f"unvalidated bytecode cache directory: {path}")
                empty_directory_targets.add(path)
                continue
            owner = owners.get(path)
            if path in derived_targets:
                continue
            if owner is None:
                raise PruneError(f"unowned {kind} content cannot be pruned: {path}")
            if path not in initial_targets:
                raise PruneError(
                    f"target content lacks validated RECORD ownership: {path}"
                )

    rewritten_records: dict[Path, str] = {}
    for distribution in distributions:
        retained = tuple(
            entry for entry in distribution.entries if entry.path not in deleted_paths
        )
        if len(retained) != len(distribution.entries):
            rewritten_records[distribution.record_path] = _record_text(retained)
    return PrunePlan(
        distributions,
        frozenset(deleted_paths),
        frozenset(
            path
            for path in empty_directory_targets
            if path.is_dir() and not path.is_symlink()
        ),
        rewritten_records,
    )


def _remove_empty_directories(
    paths: frozenset[Path],
    targets: frozenset[Path],
    roots: set[Path],
) -> int:
    candidates = set(targets)
    for path in paths:
        parent = path.parent
        while any(_is_relative_to(parent, root) and parent != root for root in roots):
            if parent.name != "__pycache__" or parent in targets:
                candidates.add(parent)
            parent = parent.parent
    removed = 0
    for directory in sorted(candidates, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            continue
        removed += 1
    return removed


def _validated_environment_root(environment_root: Path) -> Path:
    candidate = environment_root.absolute()
    if candidate.is_symlink():
        raise PruneError("environment root must not be a symlink")
    root = candidate.resolve(strict=True)
    if not root.is_dir():
        raise PruneError("environment root must be a real directory")
    return root


def prune_environment(environment_root: Path) -> dict[str, int]:
    """Prune one explicit virtual environment and verify the resulting RECORDs."""
    root = _validated_environment_root(environment_root)
    plan = _build_plan(root)
    staged_records: dict[Path, Path] = {}
    try:
        for record_path, content in plan.rewritten_records.items():
            staged = record_path.with_name(f".{record_path.name}.prune.tmp")
            if staged.exists() or staged.is_symlink():
                raise PruneError(f"stale staged RECORD exists: {staged}")
            with staged.open("x", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
            staged_records[record_path] = staged
        for path in sorted(
            plan.deleted_paths, key=lambda item: len(item.parts), reverse=True
        ):
            _assert_no_symlink(path, root)
            if not path.is_file():
                raise PruneError(f"prune target changed before deletion: {path}")
            path.unlink()
        for record_path, staged in staged_records.items():
            staged.replace(record_path)
    finally:
        for staged in staged_records.values():
            staged.unlink(missing_ok=True)

    roots = {distribution.site_packages for distribution in plan.distributions}
    directories_removed = _remove_empty_directories(
        plan.deleted_paths,
        plan.empty_directory_targets,
        roots,
    )
    verification = _build_plan(root)
    if (
        verification.deleted_paths
        or verification.empty_directory_targets
        or verification.rewritten_records
    ):
        raise PruneError("runtime artifact verification found remaining prune targets")
    return {
        "directories_removed": directories_removed,
        "distributions_verified": len(verification.distributions),
        "files_removed": len(plan.deleted_paths),
        "records_rewritten": len(plan.rewritten_records),
    }


def verify_environment(environment_root: Path) -> dict[str, int]:
    """Verify that an environment is consistent and has no prunable artifacts."""
    root = _validated_environment_root(environment_root)
    plan = _build_plan(root)
    if plan.deleted_paths or plan.empty_directory_targets or plan.rewritten_records:
        raise PruneError("runtime environment still contains prunable artifacts")
    return {
        "directories_removed": 0,
        "distributions_verified": len(plan.distributions),
        "files_removed": 0,
        "records_rewritten": 0,
    }


def create_parser() -> argparse.ArgumentParser:
    """Create the runtime artifact pruning command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-root", required=True, type=Path)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Validate RECORD consistency and require no remaining prune targets.",
    )
    parser.add_argument(
        "--disposable-build-environment",
        action="store_true",
        help=(
            "Acknowledge that pruning mutates in place and is supported only in a "
            "disposable image-builder environment; it is not transactional."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the pruning or verification command."""
    arguments = create_parser().parse_args(argv)
    try:
        if not arguments.verify_only and not arguments.disposable_build_environment:
            raise PruneError(
                "mutation requires --disposable-build-environment because pruning "
                "is in-place and non-transactional"
            )
        summary = (
            verify_environment(arguments.environment_root)
            if arguments.verify_only
            else prune_environment(arguments.environment_root)
        )
    except (OSError, PruneError) as error:
        print(f"runtime artifact pruning failed: {error}", file=sys.stderr)
        return EXIT_FAILURE
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
