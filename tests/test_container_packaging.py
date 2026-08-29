"""Static contracts for the API and UI production container definitions."""

import copy
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from scripts import verify_container_artifacts as artifact_verifier

ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = ROOT / "scripts" / "verify_container_artifacts.py"
TRUSTED_BASE_MANIFEST = ROOT / "security" / "config" / "pinned-runtime-base.json"
RUNTIME_BASE_INDEX = (
    "sha256:d921452dba64944bf959f22450bb3740f5b2fff4a59faa64bd6b8eaf4c57b5b8"
)
RUNTIME_BASE_MANIFEST = (
    "sha256:62e947ec7edfe308b97cebfab4e89e413c66a63ffcb3c021cb25ff3b70332639"
)
RUNTIME_BASE_CONFIG = (
    "sha256:18da20740c8286c11f78700fe506957a8009f2cc3291c8cc8288454b2cae7511"
)
RUNTIME_BASE_DIFF_IDS = [
    "sha256:e7cf7da29bd85aa6f70e9b80640e82141e6e20112096c8adbb3ebd8174aa3965",
    "sha256:81d1727fb374caf1bbd1183d3fd22d4bc8d87a48a25f51a4b07bac7ebb200b79",
]


def _rootfs_files(component: str) -> dict[str, bytes]:
    sources = (
        ("app/src/optima/api/app.py", "app/src/optima/api/production.py")
        if component == "api"
        else ("app/src/ui/app.py", "app/src/ui/api_client.py")
    )
    return {
        **{source: b"# application source\n" for source in sources},
        "app/.venv/bin/python": b"synthetic python runtime\n",
        "app/.venv/lib/python3.12/site-packages/optima.pth": b"/app/src\n",
        f"app/sbom/{component}.cdx.json": (
            b'{"bomFormat":"CycloneDX","components":[]}\n'
        ),
    }


def _private_key_pem(key_type: str = "PRIVATE KEY") -> bytes:
    encoded_body = b"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
    return (
        f"-----BEGIN {key_type}-----\n".encode()
        + encoded_body
        + b"\n"
        + f"-----END {key_type}-----\n".encode()
    )


def _distribution_files(
    distribution: str,
    *owned_paths: str,
    metadata_suffix: bytes = b"",
) -> dict[str, bytes]:
    site_packages = "app/.venv/lib/python3.12/site-packages"
    info_directory = f"{distribution.replace('-', '_')}-1.0.0.dist-info"
    metadata_paths = (
        f"{info_directory}/METADATA",
        f"{info_directory}/WHEEL",
        f"{info_directory}/RECORD",
    )
    record = "".join(f"{path},,\n" for path in (*owned_paths, *metadata_paths))
    return {
        f"{site_packages}/{info_directory}/METADATA": (
            f"Metadata-Version: 2.4\nName: {distribution}\nVersion: 1.0.0\n"
        ).encode()
        + metadata_suffix,
        f"{site_packages}/{info_directory}/WHEEL": b"Wheel-Version: 1.0\n",
        f"{site_packages}/{info_directory}/RECORD": record.encode(),
        **{
            f"{site_packages}/{owned_path}": b"synthetic dependency content\n"
            for owned_path in owned_paths
        },
    }


def _runtime_sbom(*distributions: tuple[str, str]) -> bytes:
    components = [
        {
            "type": "library",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{name}@{version}",
        }
        for name, version in distributions
    ]
    return json.dumps({"bomFormat": "CycloneDX", "components": components}).encode()


def _write_rootfs(
    path: Path,
    component: str,
    *,
    extra_files: dict[str, bytes] | None = None,
    extra_directories: set[str] | None = None,
    omitted_files: set[str] | None = None,
) -> None:
    files = _rootfs_files(component)
    files.update(extra_files or {})
    for omitted in omitted_files or set():
        files.pop(omitted)
    with tarfile.open(path, "w") as archive:
        for name in sorted(extra_directories or set()):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o755
            archive.addfile(member)
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o755 if name.endswith("/python") else 0o644
            archive.addfile(member, BytesIO(data))


def _tar_bytes(
    entries: list[tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]],
    *,
    archive_format: int | None = None,
) -> bytes:
    stream = BytesIO()
    with tarfile.open(
        fileobj=stream,
        mode="w",
        format=archive_format or tarfile.DEFAULT_FORMAT,
    ) as archive:
        for entry in entries:
            member, data = entry if isinstance(entry, tuple) else (entry, None)
            archive.addfile(member, BytesIO(data) if data is not None else None)
    return stream.getvalue()


def _regular_member(
    name: str,
    data: bytes,
    *,
    mode: int = 0o644,
    uid: int = 0,
    gid: int = 0,
) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = mode
    member.uid = uid
    member.gid = gid
    return member, data


def _directory_member(
    name: str,
    *,
    mode: int = 0o755,
    uid: int = 0,
    gid: int = 0,
) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = mode
    member.uid = uid
    member.gid = gid
    return member


def _synthetic_attestation_inputs(
    tmp_path: Path,
    component: str = "api",
    *,
    compress_later_layer: bool = False,
    image_architecture: str = "amd64",
    later_layer_entries: list[tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]]
    | None = None,
    later_layer_format: int | None = None,
) -> tuple[Path, Path, Path, Path]:
    reviewed_files = {
        "usr/include/e_scossl.h": b"synthetic openssl header\n",
        "usr/include/symcrypt.h": b"synthetic symcrypt header\n",
        "usr/include/symcrypt_internal.h": b"synthetic internal header\n",
        "usr/include/symcrypt_low_level.h": b"synthetic low-level header\n",
        "usr/include/symcrypt_no_sal.h": b"synthetic no-sal header\n",
        "var/cache/ldconfig/aux-cache": b"synthetic linker cache\n",
    }
    reviewed_entries = [
        {
            "gid": 0,
            "mode": "0755",
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "type": "file",
            "uid": 0,
        }
        for path, data in reviewed_files.items()
        if path.startswith("usr/include/")
    ]
    reviewed_entries.extend(
        [
            {
                "gid": 0,
                "mode": "0755",
                "path": "var/cache",
                "type": "directory",
                "uid": 0,
            },
            {
                "gid": 0,
                "mode": "0700",
                "path": "var/cache/ldconfig",
                "type": "directory",
                "uid": 0,
            },
            {
                "gid": 0,
                "mode": "0600",
                "path": "var/cache/ldconfig/aux-cache",
                "sha256": hashlib.sha256(
                    reviewed_files["var/cache/ldconfig/aux-cache"]
                ).hexdigest(),
                "size": len(reviewed_files["var/cache/ldconfig/aux-cache"]),
                "type": "file",
                "uid": 0,
            },
        ]
    )
    base_layers = [
        _tar_bytes([_regular_member("base/one", b"base layer one\n")]),
        _tar_bytes([_regular_member("base/two", b"base layer two\n")]),
    ]
    application_layer = _tar_bytes(
        later_layer_entries
        if later_layer_entries is not None
        else [_regular_member("app/src/application.py", b"application\n")],
        archive_format=later_layer_format,
    )
    if compress_later_layer:
        application_layer = gzip.compress(application_layer, mtime=0)
    layers = [*base_layers, application_layer]
    diff_ids = [f"sha256:{hashlib.sha256(layer).hexdigest()}" for layer in layers]
    index_digest = RUNTIME_BASE_INDEX
    child_digest = RUNTIME_BASE_MANIFEST
    base_config_digest = RUNTIME_BASE_CONFIG
    reference = f"example.invalid/runtime@{index_digest}"
    trusted = {
        "components": {
            component: {
                "dockerfile": f"Dockerfile.{component}",
                "image_tag": f"optima-{component}:pr",
            }
        },
        "exact_subtrees": [
            {
                "entries": [
                    "var/cache",
                    "var/cache/ldconfig",
                    "var/cache/ldconfig/aux-cache",
                ],
                "path": "var/cache",
            }
        ],
        "image": {
            "config_digest": base_config_digest,
            "index_digest": index_digest,
            "manifest_digest": child_digest,
            "platform": {"architecture": "amd64", "os": "linux"},
            "reference": reference,
            "rootfs_diff_ids": diff_ids[:2],
        },
        "packages": [
            {
                "name": "Synthetic",
                "purl": "pkg:rpm/example/Synthetic@1.0?arch=x86_64",
                "version": "1.0.x86_64",
            }
        ],
        "policy": "optima-pinned-runtime-base-v1",
        "reviewed_entries": reviewed_entries,
        "schema_version": 1,
    }
    trusted_path = tmp_path / "trusted.json"
    trusted_path.write_text(json.dumps(trusted), encoding="utf-8")
    dockerfile_path = tmp_path / f"Dockerfile.{component}"
    dockerfile_path.write_text(f"FROM {reference} AS runtime\n", encoding="utf-8")

    config = {
        "architecture": image_architecture,
        "os": "linux",
        "rootfs": {"diff_ids": diff_ids, "type": "layers"},
    }
    config_bytes = json.dumps(config, separators=(",", ":")).encode()
    config_name = f"{hashlib.sha256(config_bytes).hexdigest()}.json"
    layer_names = [f"layer-{index}/layer.tar" for index in range(len(layers))]
    save_manifest = [
        {
            "Config": config_name,
            "Layers": layer_names,
            "RepoTags": [f"optima-{component}:pr"],
        }
    ]
    image_archive = tmp_path / f"{component}-image.tar"
    with tarfile.open(image_archive, "w") as archive:
        manifest_bytes = json.dumps(save_manifest).encode()
        archive.addfile(
            _regular_member("manifest.json", manifest_bytes)[0],
            BytesIO(manifest_bytes),
        )
        archive.addfile(
            _regular_member(config_name, config_bytes)[0],
            BytesIO(config_bytes),
        )
        for name, layer in zip(layer_names, layers, strict=True):
            archive.addfile(_regular_member(name, layer)[0], BytesIO(layer))

    rootfs_path = tmp_path / f"{component}-rootfs.tar"
    _write_rootfs(rootfs_path, component)
    with tarfile.open(rootfs_path, "a") as archive:
        archive.addfile(_directory_member("var/cache", mode=0o755))
        archive.addfile(_directory_member("var/cache/ldconfig", mode=0o700))
        for path, data in reviewed_files.items():
            mode = 0o600 if path.endswith("aux-cache") else 0o755
            archive.addfile(_regular_member(path, data, mode=mode)[0], BytesIO(data))
    return image_archive, rootfs_path, trusted_path, dockerfile_path


def _run_base_attestation(
    tmp_path: Path,
    image: Path,
    rootfs: Path,
    trusted: Path,
    dockerfile_path: Path,
    *,
    component: str = "api",
) -> subprocess.CompletedProcess[str]:
    return _run_verifier(
        "attest-base",
        "--component",
        component,
        "--image-archive",
        str(image),
        "--rootfs-archive",
        str(rootfs),
        "--trusted-manifest",
        str(trusted),
        "--dockerfile",
        str(dockerfile_path),
        "--output",
        str(tmp_path / "attestation.json"),
    )


def _rewrite_rootfs_entry(
    archive_path: Path,
    target: str,
    mutation: str,
) -> None:
    entries: list[tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]] = []
    with tarfile.open(archive_path, "r") as archive:
        for member in archive:
            rewritten = copy.copy(member)
            extracted = archive.extractfile(member) if member.isreg() else None
            data = extracted.read() if extracted is not None else None
            if member.name == target:
                if mutation == "bytes":
                    assert data is not None
                    data = b"X" + data[1:]
                elif mutation == "size":
                    assert data is not None
                    data += b"larger"
                    rewritten.size = len(data)
                elif mutation == "mode":
                    rewritten.mode ^= 0o100
                elif mutation == "uid":
                    rewritten.uid += 1
                elif mutation == "gid":
                    rewritten.gid += 1
                elif mutation == "type":
                    rewritten.type = tarfile.SYMTYPE
                    rewritten.linkname = "elsewhere"
                    rewritten.size = 0
                    data = None
                else:
                    raise AssertionError(f"unsupported mutation {mutation}")
            entries.append((rewritten, data) if data is not None else rewritten)
    archive_path.write_bytes(_tar_bytes(entries))


def _append_tar_entries(
    archive_path: Path,
    entries: list[tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]],
) -> None:
    with tarfile.open(archive_path, "a") as archive:
        for entry in entries:
            member, data = entry if isinstance(entry, tuple) else (entry, None)
            archive.addfile(member, BytesIO(data) if data is not None else None)


def _corrupt_saved_config_filename(image_archive: Path) -> None:
    entries: list[tuple[str, bytes]] = []
    with tarfile.open(image_archive, "r") as archive:
        for member in archive:
            if not member.isreg():
                continue
            extracted = archive.extractfile(member)
            assert extracted is not None
            entries.append((member.name, extracted.read()))
    manifest = json.loads(
        next(data for name, data in entries if name == "manifest.json")
    )
    old_config = manifest[0]["Config"]
    new_config = f"{'f' * 64}.json"
    manifest[0]["Config"] = new_config
    rewritten: list[tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]] = []
    for name, data in entries:
        if name == "manifest.json":
            data = json.dumps(manifest).encode()
        elif name == old_config:
            name = new_config
        rewritten.append(_regular_member(name, data))
    image_archive.write_bytes(_tar_bytes(rewritten))


def _run_verifier(
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_environment = os.environ.copy()
    process_environment.update(environment or {})
    return subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=process_environment,
    )


def _write_trivy_report(
    path: Path,
    *,
    vulnerabilities: list[dict[str, str]] | None = None,
    secrets: list[dict[str, str]] | None = None,
    schema_version: int = 2,
    artifact_name: str = "optima-api:pr",
    image_id: str = "sha256:synthetic-api",
    results: list[dict[str, Any]] | None = None,
) -> None:
    package_results: list[dict[str, Any]] = [
        {
            "Target": "synthetic-rootfs (azure 3.0)",
            "Class": "os-pkgs",
            "Type": "azure",
            "Packages": [{"Name": "synthetic-os-package", "Version": "1.0"}],
            "Vulnerabilities": [],
        },
        {
            "Target": "Python",
            "Class": "lang-pkgs",
            "Type": "python-pkg",
            "Packages": [{"Name": "synthetic-python-package", "Version": "1.0"}],
            "Vulnerabilities": vulnerabilities or [],
        },
    ]
    if secrets:
        package_results.append(
            {
                "Target": "synthetic-rootfs",
                "Class": "secret",
                "Type": "secret",
                "Secrets": secrets,
            }
        )
    report: dict[str, Any] = {
        "SchemaVersion": schema_version,
        "ArtifactName": artifact_name,
        "Metadata": {"ImageID": image_id},
        "Results": package_results if results is None else results,
    }
    path.write_text(json.dumps(report), encoding="utf-8")


def _trivy_vulnerability(severity: str) -> dict[str, str]:
    return {
        "VulnerabilityID": f"CVE-SYNTHETIC-{severity}",
        "PkgName": "synthetic-package",
        "InstalledVersion": "1.0",
        "Severity": severity,
    }


def _run_trivy_verifier(
    tmp_path: Path,
    api_report: Path,
    ui_report: Path,
) -> subprocess.CompletedProcess[str]:
    return _run_verifier(
        "trivy",
        "--report",
        f"api={api_report}",
        "--report",
        f"ui={ui_report}",
        "--expected-artifact",
        "api=optima-api:pr",
        "--expected-artifact",
        "ui=optima-ui:pr",
        "--expected-image-id",
        "api=sha256:synthetic-api",
        "--expected-image-id",
        "ui=sha256:synthetic-ui",
        "--output",
        str(tmp_path / "trivy-evidence.json"),
    )


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
    assert "scripts/prune_runtime_artifacts.py --environment-root /app/.venv" in content
    assert "--environment-root /app/.venv --verify-only" in content
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
    assert "scripts/prune_runtime_artifacts.py --environment-root /app/.venv" in content
    assert "--environment-root /app/.venv --verify-only" in content
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
        "!scripts/prune_runtime_artifacts.py",
        "**/__pycache__/",
        "**/*.py[cod]",
        "**/.env",
        "**/.env.*",
        "**/.git",
        "**/.git/**",
    ]


def test_container_build_prunes_and_verifies_before_generating_sbom() -> None:
    """Run builder-only pruning after sync and before runtime inventory generation."""
    for name in ("Dockerfile.api", "Dockerfile.ui"):
        content = dockerfile(name)
        runtime = content.split(" AS runtime", maxsplit=1)[1]

        assert content.count("COPY scripts/prune_runtime_artifacts.py") == 1
        assert content.count("--disposable-build-environment") == 1
        assert content.index("uv sync --frozen") < content.index(
            "scripts/prune_runtime_artifacts.py --environment-root /app/.venv"
        )
        assert content.index(
            "--environment-root /app/.venv --verify-only"
        ) < content.index("scripts/generate_sbom.py --component")
        assert "prune_runtime_artifacts.py" not in runtime
        assert runtime.count("COPY --from=builder") == 2
        assert "COPY src ./src" in runtime


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


def test_trusted_runtime_base_manifest_pins_reviewed_provenance() -> None:
    """Pin reviewed OCI and RPM provenance independently from the trust manifest."""
    manifest = json.loads(TRUSTED_BASE_MANIFEST.read_text(encoding="utf-8"))

    assert set(manifest) == {
        "components",
        "exact_subtrees",
        "image",
        "packages",
        "policy",
        "reviewed_entries",
        "schema_version",
    }
    assert manifest["schema_version"] == 1
    assert manifest["policy"] == "optima-pinned-runtime-base-v1"
    assert manifest["components"] == {
        "api": {
            "dockerfile": "Dockerfile.api",
            "image_tag": "optima-api:pr",
        },
        "ui": {
            "dockerfile": "Dockerfile.ui",
            "image_tag": "optima-ui:pr",
        },
    }
    assert manifest["exact_subtrees"] == [
        {
            "entries": [
                "var/cache",
                "var/cache/ldconfig",
                "var/cache/ldconfig/aux-cache",
            ],
            "path": "var/cache",
        }
    ]
    assert manifest["image"] == {
        "config_digest": RUNTIME_BASE_CONFIG,
        "index_digest": RUNTIME_BASE_INDEX,
        "manifest_digest": RUNTIME_BASE_MANIFEST,
        "platform": {"architecture": "amd64", "os": "linux"},
        "reference": (
            "mcr.microsoft.com/azurelinux/distroless/python:3.12-nonroot@"
            f"{RUNTIME_BASE_INDEX}"
        ),
        "rootfs_diff_ids": RUNTIME_BASE_DIFF_IDS,
    }
    assert manifest["packages"] == [
        {
            "name": "SymCrypt",
            "purl": (
                "pkg:rpm/azurelinux/SymCrypt@103.8.0-1.azl3?"
                "arch=x86_64&distro=azurelinux-3.0"
            ),
            "version": "103.8.0-1.azl3.x86_64",
        },
        {
            "name": "SymCrypt-OpenSSL",
            "purl": (
                "pkg:rpm/azurelinux/SymCrypt-OpenSSL@1.10.0-1.azl3?"
                "arch=x86_64&distro=azurelinux-3.0"
            ),
            "version": "1.10.0-1.azl3.x86_64",
        },
    ]
    assert {entry["path"]: entry for entry in manifest["reviewed_entries"]} == {
        "usr/include/e_scossl.h": {
            "gid": 0,
            "mode": "0755",
            "path": "usr/include/e_scossl.h",
            "sha256": (
                "d9bcb4c543480bcc624c7258e07c53fd8be004b4a05b5e85709f7e7d3820c9ad"
            ),
            "size": 339,
            "type": "file",
            "uid": 0,
        },
        "usr/include/symcrypt.h": {
            "gid": 0,
            "mode": "0755",
            "path": "usr/include/symcrypt.h",
            "sha256": (
                "54fdf60238a02328cb7457420c8bdcb6478a3b54152135ecc5221084e80ce33b"
            ),
            "size": 453529,
            "type": "file",
            "uid": 0,
        },
        "usr/include/symcrypt_internal.h": {
            "gid": 0,
            "mode": "0755",
            "path": "usr/include/symcrypt_internal.h",
            "sha256": (
                "1aff0521fddc3bb40ae9ba7ef1a88285f85d21c24a4876ca7961899493233b44"
            ),
            "size": 174122,
            "type": "file",
            "uid": 0,
        },
        "usr/include/symcrypt_low_level.h": {
            "gid": 0,
            "mode": "0755",
            "path": "usr/include/symcrypt_low_level.h",
            "sha256": (
                "1b7099c7815a111d8f9b396e938e168a7aaa8b17153c43e98d9d115170696371"
            ),
            "size": 135062,
            "type": "file",
            "uid": 0,
        },
        "usr/include/symcrypt_no_sal.h": {
            "gid": 0,
            "mode": "0755",
            "path": "usr/include/symcrypt_no_sal.h",
            "sha256": (
                "12588ccebc480440555242b10f6078692a9d38d4435a865de243d48e185f4aca"
            ),
            "size": 1268,
            "type": "file",
            "uid": 0,
        },
        "var/cache": {
            "gid": 0,
            "mode": "0755",
            "path": "var/cache",
            "type": "directory",
            "uid": 0,
        },
        "var/cache/ldconfig": {
            "gid": 0,
            "mode": "0700",
            "path": "var/cache/ldconfig",
            "type": "directory",
            "uid": 0,
        },
        "var/cache/ldconfig/aux-cache": {
            "gid": 0,
            "mode": "0600",
            "path": "var/cache/ldconfig/aux-cache",
            "sha256": (
                "4730ffbcc3c3ab820172649e4422c98dbcbf4f1fa5692913341537e3be894584"
            ),
            "size": 3567,
            "type": "file",
            "uid": 0,
        },
    }
    runtime_from = (
        "FROM mcr.microsoft.com/azurelinux/distroless/python:3.12-nonroot@"
        f"{RUNTIME_BASE_INDEX} AS runtime"
    )
    assert all(
        runtime_from in dockerfile(name) for name in ("Dockerfile.api", "Dockerfile.ui")
    )


def test_verifier_compiles_exact_production_base_identity() -> None:
    """Keep reviewed remote declarations independent from editable evidence JSON."""
    assert artifact_verifier.PINNED_BASE_IDENTITIES == {
        (RUNTIME_BASE_INDEX, "linux", "amd64"): {
            "config_digest": RUNTIME_BASE_CONFIG,
            "manifest_digest": RUNTIME_BASE_MANIFEST,
        }
    }


@pytest.mark.parametrize("component", ["api", "ui"])
def test_pinned_base_attestation_accepts_valid_synthetic_archives(
    tmp_path: Path,
    component: str,
) -> None:
    """Accept exact base ancestry, untouched reviewed paths, and matching rootfs."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(
        tmp_path,
        component,
    )
    output = tmp_path / "attestation.json"

    result = _run_base_attestation(
        tmp_path,
        image,
        rootfs,
        trusted,
        dockerfile_path,
        component=component,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["status"] == "pass"
    assert evidence["component"] == component
    assert evidence["reviewed_path_count"] == 8
    assert evidence["reviewed_base_config_digest"] == RUNTIME_BASE_CONFIG
    assert evidence["reviewed_base_manifest_digest"] == RUNTIME_BASE_MANIFEST
    assert "base_config_digest" not in evidence
    assert "base_manifest_digest" not in evidence
    assert evidence["final_image_platform"] == {
        "architecture": "amd64",
        "os": "linux",
    }
    trusted_document = json.loads(trusted.read_text(encoding="utf-8"))
    assert (
        evidence["verified_base_rootfs_diff_id_prefix"]
        == trusted_document["image"]["rootfs_diff_ids"]
    )
    assert evidence["later_layer_protected_paths_absent"] is True
    verified_entries = evidence["verified_reviewed_entries"]
    assert {
        entry["path"] for entry in verified_entries
    } == artifact_verifier.REVIEWED_BASE_PATHS
    assert all(
        {"gid", "mode", "path", "type", "uid"} <= entry.keys()
        for entry in verified_entries
    )
    assert all(
        {"sha256", "size"} <= entry.keys()
        for entry in verified_entries
        if entry["type"] == "file"
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("index_digest", "index"),
        ("manifest_digest", "manifest"),
        ("config_digest", "config"),
        ("architecture", "platform"),
        ("diff_id", "diff"),
        ("diff_order", "diff"),
        ("dockerfile", "dockerfile"),
    ],
)
def test_pinned_base_attestation_rejects_identity_mismatch(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    """Reject every OCI, platform, and Dockerfile identity mismatch."""
    image, rootfs, trusted_path, dockerfile_path = _synthetic_attestation_inputs(
        tmp_path
    )
    trusted = json.loads(trusted_path.read_text(encoding="utf-8"))
    if mutation == "architecture":
        trusted["image"]["platform"]["architecture"] = "arm64"
    elif mutation == "diff_id":
        trusted["image"]["rootfs_diff_ids"][0] = f"sha256:{'f' * 64}"
    elif mutation == "diff_order":
        trusted["image"]["rootfs_diff_ids"].reverse()
    elif mutation == "dockerfile":
        dockerfile_path.write_text("FROM scratch AS runtime\n", encoding="utf-8")
    else:
        trusted["image"][mutation] = f"sha256:{'f' * 64}"
    trusted_path.write_text(json.dumps(trusted), encoding="utf-8")

    result = _run_base_attestation(
        tmp_path,
        image,
        rootfs,
        trusted_path,
        dockerfile_path,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert reason in (result.stdout + result.stderr).casefold()


def test_attested_rootfs_suppresses_only_exact_reviewed_base_findings(
    tmp_path: Path,
) -> None:
    """Allow only the eight attested base paths while keeping global checks active."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(tmp_path)
    attestation = _run_base_attestation(
        tmp_path, image, rootfs, trusted, dockerfile_path
    )
    assert attestation.returncode == 0, attestation.stdout + attestation.stderr

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(rootfs),
        "--attestation",
        str(tmp_path / "attestation.json"),
        "--output",
        str(tmp_path / "rootfs-summary.json"),
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("target", "mutation", "reason"),
    [
        ("usr/include/e_scossl.h", "bytes", "hash"),
        ("usr/include/e_scossl.h", "size", "size"),
        ("usr/include/e_scossl.h", "mode", "mode"),
        ("usr/include/e_scossl.h", "uid", "uid"),
        ("usr/include/e_scossl.h", "gid", "gid"),
        ("usr/include/e_scossl.h", "type", "type"),
        ("var/cache", "mode", "mode"),
        ("var/cache/ldconfig", "type", "type"),
    ],
)
def test_pinned_base_attestation_rejects_changed_rootfs_metadata_or_bytes(
    tmp_path: Path,
    target: str,
    mutation: str,
    reason: str,
) -> None:
    """Reject changed bytes, size, mode, ownership, or type for reviewed entries."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(tmp_path)
    _rewrite_rootfs_entry(rootfs, target, mutation)

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert reason in (result.stdout + result.stderr).casefold()


def test_pinned_base_attestation_requires_exact_cache_subtree(tmp_path: Path) -> None:
    """Reject every additional descendant under the reviewed var/cache subtree."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(tmp_path)
    _append_tar_entries(rootfs, [_regular_member("var/cache/unlisted", b"cache\n")])

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "subtree" in (result.stdout + result.stderr).casefold()


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (_regular_member("usr/include/e_scossl.h", b"override\n"), "replaces"),
        (_regular_member("var/cache/unlisted", b"addition\n"), "reviewed"),
        (_regular_member("usr/include/.wh.e_scossl.h", b""), "masks"),
        (_regular_member("usr/.wh.include", b""), "masks"),
        (_regular_member("usr/include/.wh..wh..opq", b""), "masks"),
        (_regular_member(".wh..wh..opq", b""), "masks"),
    ],
)
def test_pinned_base_attestation_rejects_later_layer_changes_and_whiteouts(
    tmp_path: Path,
    entry: tarfile.TarInfo | tuple[tarfile.TarInfo, bytes],
    reason: str,
) -> None:
    """Reject direct, ancestor, and opaque later-layer changes to reviewed paths."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(
        tmp_path,
        later_layer_entries=[entry],
    )

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert reason in (result.stdout + result.stderr).casefold()


@pytest.mark.parametrize(
    "entry_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE]
)
def test_pinned_base_attestation_rejects_later_layer_links_and_devices(
    tmp_path: Path,
    entry_type: bytes,
) -> None:
    """Reject later-layer symlinks, hardlinks, and devices at protected ancestors."""
    member = tarfile.TarInfo("usr/include")
    member.type = entry_type
    member.linkname = "elsewhere"
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(
        tmp_path,
        later_layer_entries=[member],
    )

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "ancestor" in (result.stdout + result.stderr).casefold()


@pytest.mark.parametrize(
    "entry",
    [
        _directory_member(".", mode=0o700),
        _directory_member("usr", uid=1),
        _directory_member("usr/include", gid=1),
        _directory_member("var", mode=0o700),
        _directory_member("./usr/include", mode=0o700),
        _directory_member("usr//include", mode=0o700),
    ],
)
def test_pinned_base_attestation_rejects_later_layer_ancestor_directories(
    tmp_path: Path,
    entry: tarfile.TarInfo,
) -> None:
    """Reject every root or strict-ancestor restatement regardless of metadata."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(
        tmp_path,
        later_layer_entries=[entry],
    )

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "ancestor" in (result.stdout + result.stderr).casefold()


@pytest.mark.parametrize("archive_format", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT])
def test_pinned_base_attestation_rejects_tar_path_overrides(
    tmp_path: Path,
    archive_format: int,
) -> None:
    """Apply PAX and GNU path overrides before protected-path decisions."""
    if archive_format == tarfile.PAX_FORMAT:
        member = _directory_member("ignored")
        member.pax_headers = {"path": "./usr/include"}
    else:
        member = _directory_member("./" * 60 + "usr/include")
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(
        tmp_path,
        later_layer_entries=[member],
        later_layer_format=archive_format,
    )

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "ancestor" in (result.stdout + result.stderr).casefold()


@pytest.mark.parametrize(
    "entries",
    [
        [_regular_member("../usr/include/e_scossl.h", b"escape\n")],
        [
            _regular_member("app/duplicate", b"one\n"),
            _regular_member("app/duplicate", b"two\n"),
        ],
        [
            _regular_member("app/normalized", b"one\n"),
            _regular_member("./app//normalized", b"two\n"),
        ],
    ],
)
def test_pinned_base_attestation_rejects_unsafe_or_duplicate_layer_paths(
    tmp_path: Path,
    entries: list[tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]],
) -> None:
    """Reject traversal and duplicate paths even outside the reviewed inventory."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(
        tmp_path,
        later_layer_entries=entries,
    )

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr


def test_pinned_base_attestation_rejects_wrong_image_architecture(
    tmp_path: Path,
) -> None:
    """Bind the saved final image config to linux/amd64."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(
        tmp_path,
        image_architecture="arm64",
    )

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "architecture" in (result.stdout + result.stderr).casefold()


def test_pinned_base_attestation_rejects_config_filename_digest_mismatch(
    tmp_path: Path,
) -> None:
    """Require Docker-save config filenames to equal their content digest."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(tmp_path)
    _corrupt_saved_config_filename(image)

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "config filename" in (result.stdout + result.stderr).casefold()


def test_pinned_base_attestation_rejects_compressed_saved_layer(tmp_path: Path) -> None:
    """Do not compare a compressed blob digest with an uncompressed RootFS DiffID."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(
        tmp_path,
        compress_later_layer=True,
    )

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "layer" in (result.stdout + result.stderr).casefold()


def test_pinned_base_attestation_rejects_duplicate_trusted_json_keys(
    tmp_path: Path,
) -> None:
    """Reject ambiguous trusted JSON instead of silently accepting the last key."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(tmp_path)
    content = trusted.read_text(encoding="utf-8")
    trusted.write_text(
        content.replace(
            '"schema_version": 1',
            '"schema_version": 1, "schema_version": 1',
        ),
        encoding="utf-8",
    )

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "json" in (result.stdout + result.stderr).casefold()


@pytest.mark.parametrize(
    ("limit_name", "reason"),
    [
        ("MAX_IMAGE_SAVE_ARCHIVE_BYTES", "image-save archive"),
        ("MAX_ROOTFS_ARCHIVE_BYTES", "exported rootfs"),
    ],
)
def test_pinned_base_attestation_enforces_archive_byte_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    reason: str,
) -> None:
    """Bound archive bytes before parsing attacker-controlled tar structures."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(tmp_path)
    monkeypatch.setattr(artifact_verifier, limit_name, 1)

    evidence = artifact_verifier.inspect_base_attestation(
        "api",
        image,
        rootfs,
        trusted,
        dockerfile_path,
    )

    assert evidence["status"] == "fail"
    findings = evidence["findings"]
    assert isinstance(findings, list)
    finding = findings[0]
    assert isinstance(finding, dict)
    assert finding["code"] == "archive_size_limit"
    assert reason in finding["message"]


@pytest.mark.parametrize("archive_kind", ["image", "rootfs"])
def test_pinned_base_attestation_rejects_compressed_outer_archives(
    tmp_path: Path,
    archive_kind: str,
) -> None:
    """Require the uncompressed tar formats emitted by Docker save and export."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(tmp_path)
    archive = image if archive_kind == "image" else rootfs
    archive.write_bytes(gzip.compress(archive.read_bytes(), mtime=0))

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "archive" in (result.stdout + result.stderr).casefold()


def test_attested_rootfs_rejects_unlisted_header(tmp_path: Path) -> None:
    """Keep the global build-header policy active outside the eight exact paths."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(tmp_path)
    _append_tar_entries(
        rootfs, [_regular_member("usr/include/unlisted.h", b"header\n")]
    )
    attestation = _run_base_attestation(
        tmp_path, image, rootfs, trusted, dockerfile_path
    )
    assert attestation.returncode == 0, attestation.stdout + attestation.stderr

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(rootfs),
        "--attestation",
        str(tmp_path / "attestation.json"),
        "--output",
        str(tmp_path / "rootfs-summary.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "build header" in (result.stdout + result.stderr).casefold()


def test_rootfs_rejects_tampered_attestation_summary(tmp_path: Path) -> None:
    """Reject a passing summary whose exact attested inventory was broadened."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(tmp_path)
    attestation = _run_base_attestation(
        tmp_path, image, rootfs, trusted, dockerfile_path
    )
    assert attestation.returncode == 0, attestation.stdout + attestation.stderr
    summary_path = tmp_path / "attestation.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["attested_paths"].append("usr/include/unlisted.h")
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(rootfs),
        "--attestation",
        str(summary_path),
        "--output",
        str(tmp_path / "rootfs-summary.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "inventory" in (result.stdout + result.stderr).casefold()


@pytest.mark.parametrize(
    "field",
    [
        "later_layer_protected_paths_absent",
        "verified_base_rootfs_diff_id_prefix",
        "verified_reviewed_entries",
    ],
)
def test_rootfs_requires_complete_artifact_derived_attestation(
    tmp_path: Path,
    field: str,
) -> None:
    """Do not activate exact-path suppression from an incomplete attestation."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(tmp_path)
    attestation = _run_base_attestation(
        tmp_path, image, rootfs, trusted, dockerfile_path
    )
    assert attestation.returncode == 0, attestation.stdout + attestation.stderr
    summary_path = tmp_path / "attestation.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.pop(field)
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(rootfs),
        "--attestation",
        str(summary_path),
        "--output",
        str(tmp_path / "rootfs-summary.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    evidence = json.loads(
        (tmp_path / "rootfs-summary.json").read_text(encoding="utf-8")
    )
    assert "base_attestation" not in evidence


@pytest.mark.parametrize("malformation", ["duplicate", "nonfinite"])
def test_rootfs_rejects_ambiguous_attestation_summary_json(
    tmp_path: Path,
    malformation: str,
) -> None:
    """Reject duplicate keys and non-standard numeric constants in summaries."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(tmp_path)
    attestation = _run_base_attestation(
        tmp_path, image, rootfs, trusted, dockerfile_path
    )
    assert attestation.returncode == 0, attestation.stdout + attestation.stderr
    summary_path = tmp_path / "attestation.json"
    content = summary_path.read_text(encoding="utf-8")
    if malformation == "duplicate":
        content = content.replace(
            '"status": "pass"',
            '"status": "pass", "status": "pass"',
        )
    else:
        content = content.replace("{", '{"nonfinite": NaN,', 1)
    summary_path.write_text(content, encoding="utf-8")

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(rootfs),
        "--attestation",
        str(summary_path),
        "--output",
        str(tmp_path / "rootfs-summary.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    evidence = json.loads(
        (tmp_path / "rootfs-summary.json").read_text(encoding="utf-8")
    )
    assert any(
        finding["code"] == "malformed_base_attestation"
        for finding in evidence["findings"]
    )


@pytest.mark.parametrize("unsafe_kind", ["duplicate", "traversal", "malformed"])
def test_pinned_base_attestation_rejects_unsafe_rootfs_archives(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    """Reject duplicate, parent-traversal, and malformed exported rootfs archives."""
    image, rootfs, trusted, dockerfile_path = _synthetic_attestation_inputs(tmp_path)
    if unsafe_kind == "duplicate":
        _append_tar_entries(
            rootfs, [_regular_member("app/src/optima/api/app.py", b"x")]
        )
    elif unsafe_kind == "traversal":
        _append_tar_entries(rootfs, [_regular_member("../escape", b"x")])
    else:
        rootfs.write_bytes(b"not a tar archive")

    result = _run_base_attestation(tmp_path, image, rootfs, trusted, dockerfile_path)

    assert result.returncode == 1, result.stdout + result.stderr


def test_pinned_base_attestation_rejects_trusted_manifest_path_broadening(
    tmp_path: Path,
) -> None:
    """Prevent manifest edits from broadening the fixed reviewed path inventory."""
    image, rootfs, trusted_path, dockerfile_path = _synthetic_attestation_inputs(
        tmp_path
    )
    trusted = json.loads(trusted_path.read_text(encoding="utf-8"))
    trusted["reviewed_entries"][0]["path"] = "usr/include/unlisted.h"
    trusted_path.write_text(json.dumps(trusted), encoding="utf-8")

    result = _run_base_attestation(
        tmp_path, image, rootfs, trusted_path, dockerfile_path
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "unapproved" in (result.stdout + result.stderr).casefold()


@pytest.mark.parametrize("component", ["api", "ui"])
def test_final_image_rootfs_accepts_minimal_runtime_inventory(
    tmp_path: Path,
    component: str,
) -> None:
    """Accept expected source, virtual environment, and dependency SBOM only."""
    archive = tmp_path / f"{component}-rootfs.tar"
    _write_rootfs(archive, component)

    result = _run_verifier(
        "rootfs",
        "--component",
        component,
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(
        (tmp_path / "rootfs-evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["counts"]["test_roots"] == 0
    assert evidence["counts"]["conftest_files"] == 0
    assert evidence["counts"]["build_manifests"] == 0


@pytest.mark.parametrize("component", ["api", "ui"])
def test_final_image_rootfs_accepts_safe_venv_python_symlink(
    tmp_path: Path,
    component: str,
) -> None:
    """Accept uv's container-root virtual-environment Python symlink."""
    archive = tmp_path / f"{component}-rootfs.tar"
    _write_rootfs(
        archive,
        component,
        omitted_files={"app/.venv/bin/python"},
        extra_files={"usr/local/bin/python3.12": b"runtime python\n"},
    )
    with tarfile.open(archive, "a") as rootfs:
        python_link = tarfile.TarInfo("app/.venv/bin/python")
        python_link.type = tarfile.SYMTYPE
        python_link.linkname = "/usr/local/bin/python3.12"
        rootfs.addfile(python_link)

    result = _run_verifier(
        "rootfs",
        "--component",
        component,
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("component", "relative_path", "reason"),
    [
        ("api", "app/src/optima/api/production.py", "source"),
        ("ui", "app/src/ui/app.py", "source"),
        ("api", "app/.venv/bin/python", "virtual environment"),
        ("ui", "app/sbom/ui.cdx.json", "sbom"),
    ],
)
def test_final_image_rootfs_requires_runtime_inventory(
    tmp_path: Path,
    component: str,
    relative_path: str,
    reason: str,
) -> None:
    """Reject final images missing required component runtime inventory."""
    archive = tmp_path / f"{component}-rootfs.tar"
    _write_rootfs(
        archive,
        component,
        omitted_files={relative_path},
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        component,
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "missing" in output.casefold()
    assert reason in output.casefold()


@pytest.mark.parametrize(
    ("relative_path", "content", "reason"),
    [
        (".git/config", b"synthetic git metadata", ".git"),
        ("app/.env", b"SETTING=synthetic", ".env"),
        ("app/.env.production", b"SETTING=synthetic", ".env.production"),
        (
            "app/src/optima/private.pem",
            _private_key_pem(),
            "private key",
        ),
        ("app/tests/test_example.py", b"assert True", "tests"),
        ("app/src/test_example.py", b"assert True", "tests"),
        ("app/src/optima/tests/helper.py", b"helper = True", "tests"),
        ("app/src/optima/fixtures/example.json", b"{}", "fixtures"),
        ("app/scripts/build.py", b"print('build')", "scripts"),
        ("app/pyproject.toml", b"[build-system]", "build-only source"),
        ("app/uv.lock", b"version = 1", "build-only source"),
        ("app/Dockerfile", b"FROM scratch", "build-only source"),
        ("app/docs/design.md", b"design", "repository source"),
        ("app/infra/main.bicep", b"targetScope = 'subscription'", "repository source"),
        ("app/.copilot-tracking/plan.md", b"plan", "repository source"),
        ("app/src/optima/include/private.h", b"build header", "build header"),
        ("app/src/optima/native.hpp", b"build header", "build header"),
        ("root/.cache/uv/archive-v0/item", b"cache", "uv cache"),
        ("root/.cache/pip/http-v2/item", b"cache", "pip cache"),
        ("var/cache/dnf/item", b"cache", "package-manager cache"),
        ("usr/bin/apt-get", b"package manager", "package manager"),
        ("usr/bin/dnf", b"package manager", "package manager"),
        ("usr/bin/gcc", b"compiler", "compiler"),
        ("usr/bin/g++", b"compiler", "compiler"),
        ("usr/include/Python.h", b"build header", "build header"),
        ("bin/sh", b"shell", "shell"),
    ],
)
def test_final_image_rootfs_rejects_build_and_sensitive_artifacts(
    tmp_path: Path,
    relative_path: str,
    content: bytes,
    reason: str,
) -> None:
    """Reject one unsafe final-image artifact with a bounded safe reason."""
    archive = tmp_path / "api-rootfs.tar"
    _write_rootfs(archive, "api", extra_files={relative_path: content})

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert reason in output.casefold()
    assert len(output) <= 4096
    assert "TOP-SECRET-MATERIAL" not in output


@pytest.mark.parametrize(
    ("distribution", "owned_path"),
    [
        ("numpy", "numpy/_core/include/numpy/arrayobject.h"),
        ("msal", "msal/test_authority.py"),
        ("package", "package/fixtures/certificate.pem"),
        ("package", "package/data/schema.json"),
        ("package", "package/LICENSE"),
    ],
)
def test_final_image_rootfs_allows_dependency_owned_package_contents(
    tmp_path: Path,
    distribution: str,
    owned_path: str,
) -> None:
    """Do not classify installed wheel contents as copied repository material."""
    archive = tmp_path / "api-rootfs.tar"
    _write_rootfs(
        archive,
        "api",
        extra_files={
            **_distribution_files(distribution, owned_path),
            "app/sbom/api.cdx.json": _runtime_sbom((distribution, "1.0.0")),
        },
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("distribution", "source_path"),
    [
        ("certifi", "certifi/core.py"),
        ("numpy", "numpy/_core/numeric.py"),
    ],
)
def test_final_image_rootfs_allows_record_derived_pep3147_bytecode(
    tmp_path: Path,
    distribution: str,
    source_path: str,
) -> None:
    """Accept UV bytecode derived from a validated wheel-owned Python source."""
    archive = tmp_path / "api-rootfs.tar"
    source = PurePosixPath(source_path)
    cache_directory = source.parent / "__pycache__"
    bytecode_path = cache_directory / f"{source.stem}.cpython-312.pyc"
    _write_rootfs(
        archive,
        "api",
        extra_directories={f"app/.venv/lib/python3.12/site-packages/{cache_directory}"},
        extra_files={
            **_distribution_files(distribution, source_path),
            (
                f"app/.venv/lib/python3.12/site-packages/{bytecode_path}"
            ): b"synthetic UV bytecode\n",
            "app/sbom/api.cdx.json": _runtime_sbom((distribution, "1.0.0")),
        },
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_final_image_rootfs_accepts_pandas_sized_metadata(tmp_path: Path) -> None:
    """Recognize valid wheel identity when METADATA is larger than 64 KiB."""
    archive = tmp_path / "api-rootfs.tar"
    owned_path = "pandas/include/public.h"
    _write_rootfs(
        archive,
        "api",
        extra_files={
            **_distribution_files(
                "pandas",
                owned_path,
                metadata_suffix=b"Description: " + (b"x" * 90_000) + b"\n",
            ),
            "app/sbom/api.cdx.json": _runtime_sbom(("pandas", "1.0.0")),
        },
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("distribution", "owned_path"),
    [
        ("cryptography", "cryptography/tests/test_fernet.py"),
        ("package", "package/test/test_policy.py"),
        ("package", "package/conftest.py"),
        ("pandas", "pandas/pyproject.toml"),
    ],
)
def test_final_image_rootfs_rejects_wheel_owned_prunable_artifacts(
    tmp_path: Path,
    distribution: str,
    owned_path: str,
) -> None:
    """Never use wheel ownership to exempt tests, conftest, or build manifests."""
    archive = tmp_path / "api-rootfs.tar"
    _write_rootfs(
        archive,
        "api",
        extra_files={
            **_distribution_files(distribution, owned_path),
            "app/sbom/api.cdx.json": _runtime_sbom((distribution, "1.0.0")),
        },
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    evidence = json.loads(
        (tmp_path / "rootfs-evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["counts"]["findings"] >= 1


@pytest.mark.parametrize(
    ("distribution", "source_path", "bytecode_path"),
    [
        (
            "package",
            "package/runtime.py",
            "package/tests/__pycache__/test_orphan.cpython-312.pyc",
        ),
        (
            "package",
            "package/tests/test_policy.py",
            "package/tests/__pycache__/test_policy.cpython-311.pyc",
        ),
        (
            "package",
            "package/tests/test_policy.py",
            "package/tests/__pycache__/forged.cpython-312.pyc",
        ),
        (
            "optima",
            "optima/tests/test_policy.py",
            "optima/tests/__pycache__/test_policy.cpython-312.pyc",
        ),
    ],
)
def test_final_image_rootfs_rejects_unowned_or_forged_bytecode(
    tmp_path: Path,
    distribution: str,
    source_path: str,
    bytecode_path: str,
) -> None:
    """Reject orphan, wrong-tag, forged-name, and OPTIMA-owned bytecode."""
    archive = tmp_path / "api-rootfs.tar"
    site_packages = "app/.venv/lib/python3.12/site-packages"
    _write_rootfs(
        archive,
        "api",
        extra_directories={f"{site_packages}/{PurePosixPath(bytecode_path).parent}"},
        extra_files={
            **_distribution_files(distribution, source_path),
            f"{site_packages}/{bytecode_path}": b"synthetic untrusted bytecode\n",
            "app/sbom/api.cdx.json": _runtime_sbom((distribution, "1.0.0")),
        },
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "tests" in (result.stdout + result.stderr).casefold()


def test_final_image_rootfs_rejects_app_owned_pep3147_bytecode(
    tmp_path: Path,
) -> None:
    """Keep application source bytecode outside third-party wheel ownership."""
    archive = tmp_path / "api-rootfs.tar"
    _write_rootfs(
        archive,
        "api",
        extra_directories={"app/src/optima/tests/__pycache__"},
        extra_files={
            "app/src/optima/tests/test_policy.py": b"assert True\n",
            "app/src/optima/tests/__pycache__/test_policy.cpython-312.pyc": (
                b"synthetic application bytecode\n"
            ),
        },
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "tests" in (result.stdout + result.stderr).casefold()


@pytest.mark.parametrize(
    ("owned_path", "content", "reason"),
    [
        (
            "package/.git/config",
            b"synthetic git metadata",
            ".git",
        ),
        (
            "package/.env",
            b"SETTING=synthetic",
            ".env",
        ),
        (
            "package/private.pem",
            _private_key_pem(),
            "private key",
        ),
    ],
)
def test_dependency_path_exemption_does_not_bypass_global_artifact_rules(
    tmp_path: Path,
    owned_path: str,
    content: bytes,
    reason: str,
) -> None:
    """Keep global metadata, credential, and key rules active in dependencies."""
    archive = tmp_path / "api-rootfs.tar"
    files = _distribution_files("package", owned_path)
    files["app/sbom/api.cdx.json"] = _runtime_sbom(("package", "1.0.0"))
    files[f"app/.venv/lib/python3.12/site-packages/{owned_path}"] = content
    _write_rootfs(archive, "api", extra_files=files)

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert reason in (result.stdout + result.stderr).casefold()


@pytest.mark.parametrize(
    "owned_path",
    [
        "optima/tests/test_policy.py",
        "optima/fixtures/policy.json",
        "optima/include/policy.h",
        "optima/pyproject.toml",
    ],
)
def test_optima_distribution_cannot_inherit_third_party_exemption(
    tmp_path: Path,
    owned_path: str,
) -> None:
    """Keep app-owned material forbidden even with internally consistent metadata."""
    archive = tmp_path / "api-rootfs.tar"
    _write_rootfs(
        archive,
        "api",
        extra_files=_distribution_files("optima", owned_path),
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr


def test_unowned_site_package_path_cannot_inherit_third_party_exemption(
    tmp_path: Path,
) -> None:
    """Require wheel metadata to prove ownership of dependency test material."""
    archive = tmp_path / "api-rootfs.tar"
    _write_rootfs(
        archive,
        "api",
        extra_files={
            "app/.venv/lib/python3.12/site-packages/package/tests/test_policy.py": (
                b"assert True\n"
            )
        },
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr


def test_distribution_missing_from_runtime_sbom_cannot_inherit_exemption(
    tmp_path: Path,
) -> None:
    """Require the generated runtime inventory to corroborate wheel ownership."""
    archive = tmp_path / "api-rootfs.tar"
    owned_path = "package/tests/test_policy.py"
    _write_rootfs(
        archive,
        "api",
        extra_files=_distribution_files("package", owned_path),
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr


def test_forged_runtime_sbom_purl_identity_cannot_inherit_exemption(
    tmp_path: Path,
) -> None:
    """Require SBOM name/version fields to agree with the package purl identity."""
    archive = tmp_path / "api-rootfs.tar"
    owned_path = "package/include/public.h"
    forged_sbom = json.dumps(
        {
            "bomFormat": "CycloneDX",
            "components": [
                {
                    "type": "library",
                    "name": "package",
                    "version": "1.0.0",
                    "purl": "pkg:pypi/different-package@1.0.0",
                }
            ],
        }
    ).encode()
    _write_rootfs(
        archive,
        "api",
        extra_files={
            **_distribution_files("package", owned_path),
            "app/sbom/api.cdx.json": forged_sbom,
        },
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "build header" in (result.stdout + result.stderr).casefold()


def test_distribution_record_cannot_exempt_a_symlink(tmp_path: Path) -> None:
    """Do not allow a dependency-owned link to evade path classification."""
    archive = tmp_path / "api-rootfs.tar"
    owned_path = "package/tests/test_policy.py"
    files = _distribution_files("package", owned_path)
    files.pop(f"app/.venv/lib/python3.12/site-packages/{owned_path}")
    _write_rootfs(archive, "api", extra_files=files)
    with tarfile.open(archive, "a") as rootfs:
        test_link = tarfile.TarInfo(
            f"app/.venv/lib/python3.12/site-packages/{owned_path}"
        )
        test_link.type = tarfile.SYMTYPE
        test_link.linkname = "/app/src/optima/api/app.py"
        rootfs.addfile(test_link)

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "tests" in (result.stdout + result.stderr).casefold()


def test_distribution_record_does_not_exempt_parent_traversal(tmp_path: Path) -> None:
    """Ignore installed console-script paths without exempting their targets."""
    archive = tmp_path / "api-rootfs.tar"
    owned_path = "package/tests/test_policy.py"
    files = _distribution_files("package", owned_path)
    record_path = (
        "app/.venv/lib/python3.12/site-packages/package-1.0.0.dist-info/RECORD"
    )
    files[record_path] += b"../../../bin/package-cli,,\n"
    files["app/src/test_escape.py"] = b"assert True\n"
    files["app/sbom/api.cdx.json"] = _runtime_sbom(("package", "1.0.0"))
    _write_rootfs(archive, "api", extra_files=files)

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "app/src/test_escape.py" in (result.stdout + result.stderr)


@pytest.mark.parametrize(
    "content",
    [
        b'PRIVATE_KEY_TYPES = (b"-----BEGIN PRIVATE KEY-----", '
        b'b"-----END PRIVATE KEY-----")\n',
        b'PEM_BEGIN = "-----BEGIN RSA PRIVATE KEY-----"\n'
        b'PEM_END = "-----END RSA PRIVATE KEY-----"\n',
        b"-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----\n",
        b"-----BEGIN PRIVATE KEY-----\nnot-base64!\n-----END PRIVATE KEY-----\n",
        b"-----BEGIN PRIVATE KEY-----\nQUJDREVGR0hJSktM\n"
        b"-----END RSA PRIVATE KEY-----\n",
    ],
)
def test_final_image_rootfs_allows_private_key_marker_literals(
    tmp_path: Path,
    content: bytes,
) -> None:
    """Allow marker names and malformed fragments that are not structured keys."""
    archive = tmp_path / "api-rootfs.tar"
    _write_rootfs(
        archive,
        "api",
        extra_files={"app/src/optima/marker_literal.py": content},
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "key_type",
    [
        "PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "RSA PRIVATE KEY",
        "DSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
    ],
)
def test_final_image_rootfs_rejects_every_structured_private_key_type(
    tmp_path: Path,
    key_type: str,
) -> None:
    """Reject each supported complete private-key PEM boundary pair."""
    archive = tmp_path / "api-rootfs.tar"
    _write_rootfs(
        archive,
        "api",
        extra_files={"app/src/optima/private.pem": _private_key_pem(key_type)},
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "private key" in (result.stdout + result.stderr).casefold()


def test_final_image_rootfs_rejects_legacy_encrypted_private_key(
    tmp_path: Path,
) -> None:
    """Recognize OpenSSL PEM encryption headers before encoded key data."""
    archive = tmp_path / "api-rootfs.tar"
    encrypted_key = (
        b"-----BEGIN RSA PRIVATE KEY-----\r\n"
        b"Proc-Type: 4,ENCRYPTED\r\n"
        b"DEK-Info: AES-256-CBC,0123456789ABCDEF\r\n\r\n"
        b"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\r\n"
        b"-----END RSA PRIVATE KEY-----"
    )
    _write_rootfs(
        archive,
        "api",
        extra_files={"app/src/optima/private.pem": encrypted_key},
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "private key" in (result.stdout + result.stderr).casefold()


def test_final_image_rootfs_rejects_forbidden_executable_in_nonstandard_path(
    tmp_path: Path,
) -> None:
    """Reject forbidden executable basenames outside conventional binary folders."""
    archive = tmp_path / "api-rootfs.tar"
    _write_rootfs(archive, "api")
    with tarfile.open(archive, "a") as rootfs:
        executable = tarfile.TarInfo("opt/runtime/tools/sh")
        executable.size = len(b"synthetic executable\n")
        executable.mode = 0o755
        rootfs.addfile(executable, BytesIO(b"synthetic executable\n"))

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "shell" in (result.stdout + result.stderr).casefold()


def test_final_image_rootfs_rejects_forbidden_executable_symlink_in_nonstandard_path(
    tmp_path: Path,
) -> None:
    """Reject forbidden executable symlink basenames in every image directory."""
    archive = tmp_path / "api-rootfs.tar"
    _write_rootfs(archive, "api")
    with tarfile.open(archive, "a") as rootfs:
        shell_link = tarfile.TarInfo("opt/runtime/tools/sh")
        shell_link.type = tarfile.SYMTYPE
        shell_link.mode = 0o777
        shell_link.linkname = "/app/.venv/bin/python"
        rootfs.addfile(shell_link)

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "shell" in (result.stdout + result.stderr).casefold()


def test_final_image_rootfs_scans_private_key_material_past_first_chunk(
    tmp_path: Path,
) -> None:
    """Scan an entire regular file for private-key headers with bounded memory."""
    archive = tmp_path / "api-rootfs.tar"
    padding = b"x" * ((64 * 1024) - 12) + b"\n"
    padded_key = padding + _private_key_pem()
    _write_rootfs(
        archive,
        "api",
        extra_files={"app/src/optima/padded.bin": padded_key},
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "private key" in output.casefold()
    assert "TOP-SECRET-MATERIAL" not in output


def test_final_image_rootfs_policy_cannot_be_bypassed_with_pythonoptimize(
    tmp_path: Path,
) -> None:
    """Keep verifier policy active under an optimized child interpreter."""
    archive = tmp_path / "api-rootfs.tar"
    _write_rootfs(
        archive,
        "api",
        extra_files={"app/src/optima/private.pem": _private_key_pem("RSA PRIVATE KEY")},
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        "api",
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
        environment={"PYTHONOPTIMIZE": "2"},
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "private key" in (result.stdout + result.stderr).casefold()


@pytest.mark.parametrize("component", ["api", "ui"])
def test_final_image_rootfs_allows_runtime_libraries_and_bytecode(
    tmp_path: Path,
    component: str,
) -> None:
    """Do not mistake runtime native libraries or Python bytecode for build tools."""
    archive = tmp_path / f"{component}-rootfs.tar"
    _write_rootfs(
        archive,
        component,
        extra_files={
            "app/.venv/lib/python3.12/site-packages/native_module.so": b"runtime so",
            "app/src/runtime/__pycache__/module.cpython-312.pyc": b"runtime bytecode",
            "usr/lib/libssl.so.3": b"runtime shared library",
        },
    )

    result = _run_verifier(
        "rootfs",
        "--component",
        component,
        "--archive",
        str(archive),
        "--output",
        str(tmp_path / "rootfs-evidence.json"),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_trivy_low_and_informational_findings_are_visible_but_allowed(
    tmp_path: Path,
) -> None:
    """Summarize non-blocking findings without hiding their severity."""
    api_report = tmp_path / "api.json"
    ui_report = tmp_path / "ui.json"
    _write_trivy_report(
        api_report,
        vulnerabilities=[
            _trivy_vulnerability("LOW"),
            _trivy_vulnerability("UNKNOWN"),
        ],
    )
    _write_trivy_report(
        ui_report,
        artifact_name="optima-ui:pr",
        image_id="sha256:synthetic-ui",
    )

    result = _run_trivy_verifier(tmp_path, api_report, ui_report)
    output = (result.stdout + result.stderr).casefold()

    assert result.returncode == 0, output
    assert "low" in output
    assert "unknown" in output


@pytest.mark.parametrize("severity", ["HIGH", "CRITICAL"])
def test_trivy_high_or_critical_vulnerability_fails_policy(
    tmp_path: Path,
    severity: str,
) -> None:
    """Reject every blocking vulnerability severity."""
    api_report = tmp_path / "api.json"
    ui_report = tmp_path / "ui.json"
    _write_trivy_report(
        api_report,
        vulnerabilities=[_trivy_vulnerability(severity)],
    )
    _write_trivy_report(
        ui_report,
        artifact_name="optima-ui:pr",
        image_id="sha256:synthetic-ui",
    )

    result = _run_trivy_verifier(tmp_path, api_report, ui_report)
    output = (result.stdout + result.stderr).casefold()

    assert result.returncode == 1, output
    assert severity.casefold() in output


def test_trivy_secret_fails_policy_regardless_of_vulnerability_severity(
    tmp_path: Path,
) -> None:
    """Reject secret findings even when vulnerabilities are non-blocking."""
    api_report = tmp_path / "api.json"
    ui_report = tmp_path / "ui.json"
    _write_trivy_report(
        api_report,
        vulnerabilities=[_trivy_vulnerability("LOW")],
        secrets=[
            {
                "RuleID": "synthetic-secret",
                "Category": "synthetic",
                "Severity": "LOW",
                "Title": "Synthetic secret",
            }
        ],
    )
    _write_trivy_report(
        ui_report,
        artifact_name="optima-ui:pr",
        image_id="sha256:synthetic-ui",
    )

    result = _run_trivy_verifier(tmp_path, api_report, ui_report)
    output = (result.stdout + result.stderr).casefold()

    assert result.returncode == 1, output
    assert "secret" in output


@pytest.mark.parametrize("invalid_kind", ["malformed", "missing", "unsupported"])
def test_trivy_invalid_json_fails_closed(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    """Reject unreadable and unsupported scanner evidence."""
    api_report = tmp_path / "api.json"
    ui_report = tmp_path / "ui.json"
    _write_trivy_report(api_report)
    if invalid_kind == "malformed":
        ui_report.write_text("{not-json", encoding="utf-8")
    elif invalid_kind == "unsupported":
        _write_trivy_report(
            ui_report,
            schema_version=999,
            artifact_name="optima-ui:pr",
            image_id="sha256:synthetic-ui",
        )

    result = _run_trivy_verifier(tmp_path, api_report, ui_report)
    output = (result.stdout + result.stderr).casefold()

    assert result.returncode != 0
    assert "ui" in output
    assert invalid_kind in output


def test_trivy_evaluates_api_and_ui_before_aggregate_failure(tmp_path: Path) -> None:
    """Report findings from both images before returning aggregate failure."""
    api_report = tmp_path / "api.json"
    ui_report = tmp_path / "ui.json"
    _write_trivy_report(
        api_report,
        vulnerabilities=[_trivy_vulnerability("HIGH")],
    )
    _write_trivy_report(
        ui_report,
        secrets=[
            {
                "RuleID": "synthetic-secret",
                "Category": "synthetic",
                "Severity": "HIGH",
                "Title": "Synthetic secret",
            }
        ],
        artifact_name="optima-ui:pr",
        image_id="sha256:synthetic-ui",
    )

    result = _run_trivy_verifier(tmp_path, api_report, ui_report)
    output = (result.stdout + result.stderr).casefold()

    assert result.returncode == 1, output
    assert "api" in output and "high" in output
    assert "ui" in output and "secret" in output


@pytest.mark.parametrize(
    ("results", "reason"),
    [
        ([], "empty"),
        (
            [
                {
                    "Target": "Python",
                    "Class": "lang-pkgs",
                    "Type": "python-pkg",
                    "Packages": [{"Name": "synthetic", "Version": "1.0"}],
                }
            ],
            "os package",
        ),
        (
            [
                {
                    "Target": "synthetic-rootfs (azure 3.0)",
                    "Class": "os-pkgs",
                    "Type": "azure",
                    "Packages": [{"Name": "synthetic", "Version": "1.0"}],
                }
            ],
            "python package",
        ),
    ],
)
def test_trivy_requires_nonempty_os_and_python_package_coverage(
    tmp_path: Path,
    results: list[dict[str, Any]],
    reason: str,
) -> None:
    """Reject reports without package coverage appropriate to both final images."""
    api_report = tmp_path / "api.json"
    ui_report = tmp_path / "ui.json"
    _write_trivy_report(api_report, results=results)
    _write_trivy_report(
        ui_report,
        artifact_name="optima-ui:pr",
        image_id="sha256:synthetic-ui",
    )

    result = _run_trivy_verifier(tmp_path, api_report, ui_report)
    output = (result.stdout + result.stderr).casefold()

    assert result.returncode == 1, output
    assert reason in output


@pytest.mark.parametrize(
    ("result_class", "result_type", "coverage_name"),
    [
        ("os-pkgs", "azure", "os"),
        ("lang-pkgs", "python-pkg", "python"),
    ],
)
@pytest.mark.parametrize(
    "malformed_package",
    [
        pytest.param({}, id="empty-object"),
        pytest.param({"Name": "synthetic"}, id="missing-version"),
        pytest.param({"Version": "1.0"}, id="missing-name"),
        pytest.param({"Name": " ", "Version": "1.0"}, id="empty-name"),
        pytest.param({"Name": "synthetic", "Version": " "}, id="empty-version"),
        pytest.param({"Name": 1, "Version": "1.0"}, id="non-string-name"),
        pytest.param({"Name": "synthetic", "Version": None}, id="non-string-version"),
        pytest.param(
            {"Name": "n" * 241, "Version": "1.0"},
            id="oversized-name",
        ),
        pytest.param(
            {"Name": "synthetic", "Version": "1" * 241},
            id="oversized-version",
        ),
    ],
)
def test_trivy_rejects_malformed_package_objects_for_coverage(
    tmp_path: Path,
    result_class: str,
    result_type: str,
    coverage_name: str,
    malformed_package: dict[str, object],
) -> None:
    """Count only bounded, identified Trivy packages as scanner coverage."""
    results: list[dict[str, Any]] = [
        {
            "Target": "synthetic-rootfs (azure 3.0)",
            "Class": "os-pkgs",
            "Type": "azure",
            "Packages": [{"Name": "synthetic-os", "Version": "1.0"}],
        },
        {
            "Target": "Python",
            "Class": "lang-pkgs",
            "Type": "python-pkg",
            "Packages": [{"Name": "synthetic-python", "Version": "1.0"}],
        },
    ]
    target = next(
        result
        for result in results
        if result["Class"] == result_class and result["Type"] == result_type
    )
    target["Packages"] = [malformed_package]
    api_report = tmp_path / "api.json"
    ui_report = tmp_path / "ui.json"
    _write_trivy_report(api_report, results=results)
    _write_trivy_report(
        ui_report,
        artifact_name="optima-ui:pr",
        image_id="sha256:synthetic-ui",
    )

    result = _run_trivy_verifier(tmp_path, api_report, ui_report)
    evidence = json.loads(
        (tmp_path / "trivy-evidence.json").read_text(encoding="utf-8")
    )
    api_evidence = next(
        report for report in evidence["reports"] if report["component"] == "api"
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert api_evidence[f"{coverage_name}_package_count"] == 0
    assert any(
        finding["code"] == "malformed_report"
        and "package 0" in finding["message"].casefold()
        for finding in api_evidence["findings"]
    )


@pytest.mark.parametrize("wrong_field", ["artifact", "image_id"])
def test_trivy_binds_report_to_expected_image_identity(
    tmp_path: Path,
    wrong_field: str,
) -> None:
    """Reject scanner evidence produced for a different image reference or ID."""
    api_report = tmp_path / "api.json"
    ui_report = tmp_path / "ui.json"
    _write_trivy_report(
        api_report,
        artifact_name=(
            "attacker/image:latest" if wrong_field == "artifact" else "optima-api:pr"
        ),
        image_id=(
            "sha256:wrong" if wrong_field == "image_id" else "sha256:synthetic-api"
        ),
    )
    _write_trivy_report(
        ui_report,
        artifact_name="optima-ui:pr",
        image_id="sha256:synthetic-ui",
    )

    result = _run_trivy_verifier(tmp_path, api_report, ui_report)
    output = (result.stdout + result.stderr).casefold()

    assert result.returncode == 1, output
    assert wrong_field.replace("_", " ") in output
