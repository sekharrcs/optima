"""Static contracts for the API and UI production container definitions."""

import json
import os
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = ROOT / "scripts" / "verify_container_artifacts.py"


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
        ).encode(),
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
    omitted_files: set[str] | None = None,
) -> None:
    files = _rootfs_files(component)
    files.update(extra_files or {})
    for omitted in omitted_files or set():
        files.pop(omitted)
    with tarfile.open(path, "w") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o755 if name.endswith("/python") else 0o644
            archive.addfile(member, BytesIO(data))


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
        ("cryptography", "cryptography/tests/test_fernet.py"),
        ("msal", "msal/test_authority.py"),
        ("package", "package/fixtures/certificate.pem"),
        ("package", "package/pyproject.toml"),
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
