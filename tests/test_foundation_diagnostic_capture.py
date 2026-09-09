"""Private capture tests using only disposable synthetic keys and diagnostics."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

from scripts import foundation_diagnostic_capture as capture

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts/foundation_diagnostic_capture.py"
)


@pytest.fixture(scope="module")
def synthetic_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


def _certificate(
    key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    policy: str = "valid",
    signer: rsa.RSAPrivateKey | None = None,
) -> bytes:
    now = datetime.now(UTC)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-capture-test")]
    )
    issuer = subject
    if policy == "other-issuer":
        issuer = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-issuer")]
        )
    start = now - timedelta(minutes=1)
    end = now + timedelta(days=3)
    if policy == "expired":
        start, end = now - timedelta(days=3), now - timedelta(days=1)
    elif policy == "future":
        start = now + timedelta(days=1)
    elif policy == "under-24-hours":
        end = now + timedelta(hours=23)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(start)
        .not_valid_after(end)
    )
    if policy != "missing-basic-constraints":
        builder = builder.add_extension(
            x509.BasicConstraints(ca=policy == "ca", path_length=None),
            critical=policy != "noncritical-basic-constraints",
        )
    if policy != "missing-key-usage":
        builder = builder.add_extension(
            x509.KeyUsage(
                policy == "extra-key-usage",
                False,
                policy != "no-encipherment",
                False,
                False,
                False,
                False,
                False,
                False,
            ),
            critical=policy != "noncritical-key-usage",
        )
    if policy != "missing-ski":
        builder = builder.add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False
        )
    certificate = builder.sign(signer or key, hashes.SHA256())
    return certificate.public_bytes(serialization.Encoding.DER)


@pytest.fixture
def recipient_env(synthetic_key: rsa.RSAPrivateKey) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if name in os.environ
    }
    executable = shutil.which("openssl")
    if os.name == "nt":
        executable = "C:/Program Files/Git/usr/bin/openssl.exe"
    assert executable is not None and Path(executable).is_file()
    environment["PATH"] = (
        str(Path(executable).parent) + os.pathsep + environment["PATH"]
    )
    certificate = _certificate(synthetic_key)
    environment[capture.CERTIFICATE_VARIABLE] = base64.b64encode(certificate).decode()
    environment[capture.PIN_VARIABLE] = hashlib.sha256(certificate).hexdigest()
    environment.update(
        GITHUB_REPOSITORY="synthetic-owner/optima",
        GITHUB_SHA="a" * 40,
        GITHUB_RUN_ID="123456",
        GITHUB_RUN_ATTEMPT="2",
    )
    return environment


def _cli(
    environment: dict[str, str], command: str, directory: Path, *extra: str
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), command, "--directory", str(directory), *extra],
        env=environment,
        capture_output=True,
        check=False,
        timeout=120,
    )


def test_prepare_proves_crypto_without_persistent_output(
    tmp_path: Path, recipient_env: dict[str, str]
) -> None:
    result = _cli(recipient_env, "prepare", tmp_path)
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    assert list(tmp_path.iterdir()) == []


def _raw(directory: Path) -> dict[str, bytes]:
    streams = {
        "whatif.stdout": (
            b'{"unknownMessage":{"nested":{"password":"SYNTHETIC_SECRET"}}}'
            b"\x00\xff\xfe\r\n\n"
        ),
        "whatif.stderr": b"unknown synthetic diagnostic\x80\x00\r\nSYNTHETIC_SECRET\n",
    }
    (directory / "foundation-whatif.json").write_bytes(streams["whatif.stdout"])
    (directory / "foundation-whatif.stderr").write_bytes(streams["whatif.stderr"])
    return streams


def _decrypt(
    environment: dict[str, str],
    ciphertext: Path,
    key: rsa.RSAPrivateKey,
    directory: Path,
) -> tuple[subprocess.CompletedProcess[bytes], bytes | None]:
    """Quarantine plaintext until CMS authenticates; discard it on any failure."""
    private_key = directory / "synthetic.key.pem"
    certificate = directory / "synthetic.cert.pem"
    quarantine = directory / "quarantine.tar"
    private_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    certificate.write_bytes(
        x509.load_der_x509_certificate(
            base64.b64decode(environment[capture.CERTIFICATE_VARIABLE])
        ).public_bytes(serialization.Encoding.PEM)
    )
    try:
        executable = shutil.which("openssl", path=environment["PATH"])
        assert executable is not None
        result = subprocess.run(
            [
                executable,
                "cms",
                "-decrypt",
                "-binary",
                "-inform",
                "DER",
                "-in",
                str(ciphertext),
                "-recip",
                str(certificate),
                "-inkey",
                str(private_key),
                "-out",
                str(quarantine),
            ],
            env=environment,
            capture_output=True,
            check=False,
            timeout=120,
        )
        return result, quarantine.read_bytes() if result.returncode == 0 else None
    finally:
        quarantine.unlink(missing_ok=True)
        private_key.unlink(missing_ok=True)
        certificate.unlink(missing_ok=True)


@pytest.mark.parametrize("exit_code", [0, 1, 42, 130, 255])
def test_binary_round_trip_and_exact_private_metadata(
    tmp_path: Path,
    recipient_env: dict[str, str],
    synthetic_key: rsa.RSAPrivateKey,
    exit_code: int,
) -> None:
    streams = _raw(tmp_path)
    (tmp_path / "must-not-be-archived.txt").write_bytes(b"unrelated synthetic file")
    result = _cli(recipient_env, "seal", tmp_path, "--whatif-exit-code", str(exit_code))
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    ciphertext = tmp_path / capture.CAPTURE_NAME
    assert b"SYNTHETIC_SECRET" not in ciphertext.read_bytes()
    assert b"synthetic-owner" not in ciphertext.read_bytes()
    result, plaintext = _decrypt(recipient_env, ciphertext, synthetic_key, tmp_path)
    assert result.returncode == 0
    assert result.stdout == b""
    assert plaintext is not None
    if exit_code == 1:
        executable = shutil.which("openssl", path=recipient_env["PATH"])
        assert executable is not None
        structure = subprocess.run(
            [
                executable,
                "cms",
                "-cmsout",
                "-print",
                "-inform",
                "DER",
                "-in",
                str(ciphertext),
            ],
            env=recipient_env,
            capture_output=True,
            check=False,
            timeout=120,
        )
        assert structure.returncode == 0
        assert b"id-smime-ct-authEnvelopedData" in structure.stdout
        assert b"aes-256-gcm" in structure.stdout
        assert b"rsaesOaep" in structure.stdout
        assert b"mgf1" in structure.stdout
        assert structure.stdout.count(b"sha256") == 2
        assert b"subjectKeyIdentifier" in structure.stdout
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:") as archive:
        assert archive.getnames() == ["manifest.json", "whatif.stdout", "whatif.stderr"]
        contents = {}
        for member in archive.getmembers():
            assert member.isfile() and member.mode == 0o600
            assert member.uid == member.gid == member.mtime == 0
            assert member.uname == member.gname == ""
            assert not member.pax_headers
            content = archive.extractfile(member)
            assert content is not None
            contents[member.name] = content.read()
    assert {name: contents[name] for name in streams} == streams
    assert json.loads(contents["manifest.json"]) == {
        "schema": "optima-foundation-private-capture-v1",
        "promotable": False,
        "repository": recipient_env["GITHUB_REPOSITORY"],
        "commit_sha": recipient_env["GITHUB_SHA"],
        "run_id": recipient_env["GITHUB_RUN_ID"],
        "run_attempt": recipient_env["GITHUB_RUN_ATTEMPT"],
        "whatif_exit_code": exit_code,
        "recipient_sha256": recipient_env[capture.PIN_VARIABLE],
        "streams": {
            name: {"length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in streams.items()
        },
    }
    assert (tmp_path / "foundation-whatif.json").read_bytes() == streams[
        "whatif.stdout"
    ]
    assert (tmp_path / "foundation-whatif.stderr").read_bytes() == streams[
        "whatif.stderr"
    ]
    assert {path.name for path in tmp_path.iterdir()} == {
        "foundation-whatif.json",
        "foundation-whatif.stderr",
        "must-not-be-archived.txt",
        capture.CAPTURE_NAME,
    }


@pytest.mark.parametrize("whatif_exit", [0, 23])
def test_workflow_capture_round_trip_and_cleanup_with_real_helper(
    tmp_path: Path,
    recipient_env: dict[str, str],
    synthetic_key: rsa.RSAPrivateKey,
    whatif_exit: int,
) -> None:
    from test_foundation_workflow import ROOT, _capture_step

    bash = (
        "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
    )
    assert bash is not None and Path(bash).is_file()
    source = tmp_path / "synthetic-source"
    source.mkdir()
    streams = _raw(source)
    streams["whatif.stdout"] = (
        json.dumps(
            {
                "diagnostics": [
                    {
                        "code": "UnknownSyntheticDiagnostic",
                        "message": "SYNTHETIC_PRIVATE_DIAGNOSTIC " * 200,
                        "nested": {
                            "resourceId": (
                                "/subscriptions/00000000-0000-0000-0000-000000000000"
                                "/resourceGroups/rg-synthetic-private"
                            ),
                            "url": "https://synthetic.invalid/?sig=SYNTHETIC_SECRET",
                            "password": "SYNTHETIC_NESTED_SECRET",
                        },
                    }
                ]
            }
        ).encode("utf-8")
        + b"\n"
        + streams["whatif.stdout"]
    )
    assert len(streams["whatif.stdout"]) > 4096
    (source / "foundation-whatif.json").write_bytes(streams["whatif.stdout"])
    runner_temp = tmp_path / "runner temp"
    runner_temp.mkdir()
    scratch = runner_temp / "foundation-private.synthetic"
    scratch.mkdir(mode=0o700)
    decrypt_directory = tmp_path / "decryption"
    decrypt_directory.mkdir()
    output = tmp_path / "github-output"
    calls = tmp_path / "az-calls"
    assert not output.exists()
    recipient_env.update(
        PATH=str(Path(sys.executable).parent) + os.pathsep + recipient_env["PATH"],
        ROOT=ROOT.as_posix(),
        RUNNER_TEMP=runner_temp.as_posix(),
        GITHUB_OUTPUT=output.as_posix(),
        AZURE_RESOURCE_GROUP="rg-synthetic-private",
        WHATIF_EXIT=str(whatif_exit),
        CAPTURE_AZ_CALLS=calls.as_posix(),
        SYNTHETIC_STDOUT=(source / "foundation-whatif.json").as_posix(),
        SYNTHETIC_STDERR=(source / "foundation-whatif.stderr").as_posix(),
        OPTIMA_DIAGNOSTIC_SCRATCH_DIRECTORY=scratch.as_posix(),
    )
    fake_az = textwrap.dedent(
        r"""\
        az() {
          case "$*" in
            'group show '*) return 0 ;;
            'deployment group list '*) return 0 ;;
            'deployment group what-if '*)
              printf 'what-if\n' >> "$CAPTURE_AZ_CALLS"
              cat -- "$SYNTHETIC_STDOUT"
              cat -- "$SYNTHETIC_STDERR" >&2
              return "$WHATIF_EXIT"
              ;;
            *) return 99 ;;
          esac
        }
        """
    )
    block = _capture_step("Run exactly one authoritative foundation what-if")["run"]
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-s"],
        input=(fake_az + block).encode("utf-8"),
        cwd=ROOT,
        env=recipient_env,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == whatif_exit
    assert result.stdout == result.stderr == b""
    assert calls.read_bytes() == b"what-if\n"
    assert output.read_bytes() == b"sealed=true\n"
    raw_stdout = runner_temp / "foundation-whatif.json"
    assert raw_stdout.exists() == (whatif_exit == 0)
    if whatif_exit == 0:
        assert raw_stdout.read_bytes() == streams["whatif.stdout"]
    assert not (runner_temp / "foundation-whatif.stderr").exists()
    assert list(scratch.iterdir()) == []
    ciphertext = runner_temp / capture.CAPTURE_NAME
    result, plaintext = _decrypt(
        recipient_env, ciphertext, synthetic_key, decrypt_directory
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    assert plaintext is not None
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:") as archive:
        assert archive.getnames() == ["manifest.json", *streams]
        contents = {}
        for member in archive.getmembers():
            content = archive.extractfile(member)
            assert content is not None
            contents[member.name] = content.read()
    assert {name: contents[name] for name in streams} == streams
    manifest = json.loads(contents["manifest.json"])
    assert manifest["whatif_exit_code"] == whatif_exit
    assert manifest["streams"] == {
        name: {"length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in streams.items()
    }
    assert list(decrypt_directory.iterdir()) == []
    cleanup = _capture_step("Clean up private foundation what-if capture")["run"]
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-s"],
        input=cleanup.encode("utf-8"),
        cwd=ROOT,
        env={**recipient_env, "CAPTURE_SCRATCH": scratch.as_posix()},
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    assert not scratch.exists()
    assert list(runner_temp.iterdir()) == [ciphertext]
    assert {path.name for path in tmp_path.iterdir()} == {
        "synthetic-source",
        "runner temp",
        "decryption",
        "github-output",
        "az-calls",
    }
    for raw_name in ("foundation-whatif.json", "foundation-whatif.stderr"):
        assert not (ROOT / raw_name).exists()
        assert (source / raw_name).is_file()


def _assert_safe_failure(result: subprocess.CompletedProcess[bytes]) -> None:
    assert result.returncode != 0
    assert result.stdout == b""
    assert result.stderr.replace(b"\r\n", b"\n") == (capture.SAFE_ERROR + "\n").encode()


def _set_certificate(environment: dict[str, str], certificate: bytes) -> None:
    environment[capture.CERTIFICATE_VARIABLE] = base64.b64encode(certificate).decode()
    environment[capture.PIN_VARIABLE] = hashlib.sha256(certificate).hexdigest()


def _install_environment(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]
) -> None:
    monkeypatch.delenv(capture.SCRATCH_VARIABLE, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("command", ["prepare", "seal"])
def test_alternate_scratch_parent_retains_only_requested_output(
    tmp_path: Path,
    recipient_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    directory = tmp_path / "raw"
    directory.mkdir()
    parent = tmp_path / "owned-scratch"
    parent.mkdir(mode=0o700)
    original_mode = stat.S_IMODE(parent.stat().st_mode)
    recipient_env[capture.SCRATCH_VARIABLE] = str(parent)
    _install_environment(monkeypatch, recipient_env)
    streams = _raw(directory)
    original_encrypt = capture._encrypt
    inspected = []

    def inspect_scratch(scratch: Path, source: str, destination: str) -> None:
        assert scratch.parent == parent.resolve()
        inspected.append(scratch)
        original_encrypt(scratch, source, destination)

    monkeypatch.setattr(capture, "_encrypt", inspect_scratch)
    arguments = [command, "--directory", str(directory)]
    if command == "seal":
        arguments += ["--whatif-exit-code", "1"]
    assert capture.main(arguments) == 0
    assert capsys.readouterr() == ("", "")
    assert len(inspected) == 1 and not inspected[0].exists()
    assert list(parent.iterdir()) == []
    assert stat.S_IMODE(parent.stat().st_mode) == original_mode
    assert (directory / "foundation-whatif.json").read_bytes() == streams[
        "whatif.stdout"
    ]
    assert (directory / "foundation-whatif.stderr").read_bytes() == streams[
        "whatif.stderr"
    ]
    expected = {"foundation-whatif.json", "foundation-whatif.stderr"}
    if command == "seal":
        expected.add(capture.CAPTURE_NAME)
        assert (directory / capture.CAPTURE_NAME).stat().st_size > 0
    assert {path.name for path in directory.iterdir()} == expected
    assert {path.name for path in tmp_path.iterdir()} == {"raw", "owned-scratch"}


@pytest.fixture(scope="module")
def wrong_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


@pytest.mark.parametrize("damage", ["wrong-key", "gcm-tag", "ciphertext", "truncated"])
def test_authentication_failures_discard_quarantined_plaintext(
    tmp_path: Path,
    recipient_env: dict[str, str],
    synthetic_key: rsa.RSAPrivateKey,
    wrong_key: rsa.RSAPrivateKey,
    damage: str,
) -> None:
    _raw(tmp_path)
    assert (
        _cli(recipient_env, "seal", tmp_path, "--whatif-exit-code", "1").returncode == 0
    )
    ciphertext = tmp_path / capture.CAPTURE_NAME
    encoded = bytearray(ciphertext.read_bytes())
    if damage == "gcm-tag":
        encoded[-1] ^= 1
    elif damage == "ciphertext":
        encoded[len(encoded) // 2] ^= 1
    elif damage == "truncated":
        del encoded[-10:]
    ciphertext.write_bytes(encoded)
    key = wrong_key if damage == "wrong-key" else synthetic_key
    result, plaintext = _decrypt(recipient_env, ciphertext, key, tmp_path)
    assert result.returncode != 0
    assert result.stdout == b""
    assert b"SYNTHETIC_SECRET" not in result.stderr
    assert plaintext is None
    assert not (tmp_path / "quarantine.tar").exists()
    assert not (tmp_path / "synthetic.key.pem").exists()


@pytest.mark.parametrize(
    "invalid",
    [
        "missing-certificate",
        "empty-certificate",
        "missing-pin",
        "uppercase-pin",
        "wrong-pin",
        "short-pin",
        "pin-newline",
        "bad-base64",
        "base64-newline",
        "base64-oversize",
        "noncanonical-padding",
        "trailing-der",
        "two-certificates",
        "not-certificate",
        "pem-not-der",
        "bad-signature",
    ],
)
def test_invalid_public_variables_fail_closed_without_output(
    tmp_path: Path, recipient_env: dict[str, str], invalid: str
) -> None:
    certificate = base64.b64decode(recipient_env[capture.CERTIFICATE_VARIABLE])
    if invalid == "missing-certificate":
        del recipient_env[capture.CERTIFICATE_VARIABLE]
    elif invalid == "empty-certificate":
        recipient_env[capture.CERTIFICATE_VARIABLE] = ""
    elif invalid == "missing-pin":
        del recipient_env[capture.PIN_VARIABLE]
    elif invalid == "uppercase-pin":
        recipient_env[capture.PIN_VARIABLE] = "A" * 64
    elif invalid == "wrong-pin":
        recipient_env[capture.PIN_VARIABLE] = "0" * 64
    elif invalid == "short-pin":
        recipient_env[capture.PIN_VARIABLE] = "0" * 63
    elif invalid == "pin-newline":
        recipient_env[capture.PIN_VARIABLE] += "\n"
    elif invalid == "bad-base64":
        recipient_env[capture.CERTIFICATE_VARIABLE] = "SYNTHETIC_SECRET!"
    elif invalid == "base64-newline":
        recipient_env[capture.CERTIFICATE_VARIABLE] += "\n"
    elif invalid == "base64-oversize":
        recipient_env[capture.CERTIFICATE_VARIABLE] = "A" * 16385
    elif invalid == "noncanonical-padding":
        _set_certificate(recipient_env, b"\x00")
        recipient_env[capture.CERTIFICATE_VARIABLE] = "AB=="
    elif invalid == "trailing-der":
        _set_certificate(recipient_env, certificate + b"\x00SYNTHETIC_SECRET")
    elif invalid == "two-certificates":
        _set_certificate(recipient_env, certificate + certificate)
    elif invalid == "not-certificate":
        _set_certificate(recipient_env, b"SYNTHETIC_SECRET")
    elif invalid == "pem-not-der":
        _set_certificate(
            recipient_env,
            x509.load_der_x509_certificate(certificate).public_bytes(
                serialization.Encoding.PEM
            ),
        )
    elif invalid == "bad-signature":
        _set_certificate(recipient_env, certificate[:-1] + bytes([certificate[-1] ^ 1]))
    _assert_safe_failure(_cli(recipient_env, "prepare", tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("location", ["outer", "issuer"])
def test_nonminimal_certificate_lengths_follow_x509_round_trip(
    tmp_path: Path,
    recipient_env: dict[str, str],
    synthetic_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    """A byte-preserving X.509 round trip is not a strict-DER proof."""
    _install_environment(monkeypatch, recipient_env)
    certificate = base64.b64decode(recipient_env[capture.CERTIFICATE_VARIABLE])
    assert certificate[:2] == certificate[4:6] == b"\x30\x82"
    if location == "outer":
        nonminimal = certificate[:1] + b"\x83\x00" + certificate[2:]
    else:
        structure = capture._text(
            ["asn1parse", "-inform", "DER"], tmp_path, certificate
        )
        sequences = re.findall(
            r"^\s*(\d+):d=2\s+hl=(\d+)\s+l=\s*(\d+)\s+cons: SEQUENCE\s*$",
            structure,
            re.MULTILINE,
        )
        issuer_offset, issuer_header, issuer_length = map(int, sequences[1])
        assert issuer_header == 2 and issuer_length < 128
        modified = bytearray(
            certificate[: issuer_offset + 1]
            + b"\x81"
            + certificate[issuer_offset + 1 :]
        )
        for offset in (2, 6):
            modified[offset : offset + 2] = (
                int.from_bytes(certificate[offset : offset + 2], "big") + 1
            ).to_bytes(2, "big")
        tbs_end = 8 + int.from_bytes(modified[6:8], "big")
        signature = synthetic_key.sign(
            bytes(modified[4:tbs_end]), padding.PKCS1v15(), hashes.SHA256()
        )
        signature_row = re.search(
            r"^\s*(\d+):d=1\s+hl=(\d+)\s+l=\s*(\d+)\s+prim: BIT STRING\s*$",
            structure,
            re.MULTILINE,
        )
        assert signature_row is not None
        signature_offset, signature_header, signature_length = map(
            int, signature_row.groups()
        )
        assert signature_length == len(signature) + 1
        assert signature_offset + signature_header + signature_length == len(
            certificate
        )
        modified[-len(signature) :] = signature
        nonminimal = bytes(modified)
    assert capture._openssl(["asn1parse", "-inform", "DER"], tmp_path, nonminimal)
    round_tripped = capture._openssl(
        ["x509", "-inform", "DER", "-outform", "DER"], tmp_path, nonminimal
    )
    _set_certificate(recipient_env, nonminimal)
    result = _cli(recipient_env, "prepare", tmp_path)
    if round_tripped != nonminimal:
        _assert_safe_failure(result)
    else:
        assert location == "issuer"
        assert result.returncode == 0
        assert result.stdout == result.stderr == b""
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "policy",
    [
        "expired",
        "future",
        "under-24-hours",
        "ca",
        "noncritical-basic-constraints",
        "missing-basic-constraints",
        "missing-key-usage",
        "noncritical-key-usage",
        "extra-key-usage",
        "no-encipherment",
        "missing-ski",
        "other-issuer",
        "not-self-signed",
    ],
)
def test_recipient_policy_is_enforced(
    tmp_path: Path,
    recipient_env: dict[str, str],
    synthetic_key: rsa.RSAPrivateKey,
    wrong_key: rsa.RSAPrivateKey,
    policy: str,
) -> None:
    signer = wrong_key if policy == "not-self-signed" else None
    _set_certificate(recipient_env, _certificate(synthetic_key, policy, signer))
    _assert_safe_failure(_cli(recipient_env, "prepare", tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["rsa-2048", "rsa-4096", "rsa-exponent-3", "ec"])
def test_key_type_size_and_exponent(
    tmp_path: Path, recipient_env: dict[str, str], kind: str
) -> None:
    key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey
    if kind == "ec":
        key = ec.generate_private_key(ec.SECP256R1())
    else:
        key = rsa.generate_private_key(
            public_exponent=3 if kind == "rsa-exponent-3" else 65537,
            key_size=4096
            if kind == "rsa-4096"
            else 2048
            if kind == "rsa-2048"
            else 3072,
        )
    _set_certificate(recipient_env, _certificate(key))
    result = _cli(recipient_env, "prepare", tmp_path)
    if kind == "rsa-4096":
        assert result.returncode == 0 and result.stdout == result.stderr == b""
    else:
        _assert_safe_failure(result)
    assert list(tmp_path.iterdir()) == []


def test_rsa_pss_recipient_is_not_ordinary_rsa(
    tmp_path: Path, recipient_env: dict[str, str]
) -> None:
    executable = shutil.which("openssl", path=recipient_env["PATH"])
    assert executable is not None
    key = tmp_path / "synthetic-pss.key.pem"
    try:
        result = subprocess.run(
            [
                executable,
                "genpkey",
                "-algorithm",
                "RSA-PSS",
                "-pkeyopt",
                "rsa_keygen_bits:3072",
                "-out",
                str(key),
            ],
            env=recipient_env,
            capture_output=True,
            check=False,
            timeout=120,
        )
        assert result.returncode == 0
        result = subprocess.run(
            [
                executable,
                "req",
                "-new",
                "-x509",
                "-key",
                str(key),
                "-days",
                "3",
                "-subj",
                "/CN=synthetic-pss",
                "-outform",
                "DER",
                "-addext",
                "basicConstraints=critical,CA:FALSE",
                "-addext",
                "keyUsage=critical,keyEncipherment",
                "-addext",
                "subjectKeyIdentifier=hash",
            ],
            env=recipient_env,
            capture_output=True,
            check=False,
            timeout=120,
        )
        assert result.returncode == 0
        _set_certificate(recipient_env, result.stdout)
    finally:
        key.unlink(missing_ok=True)
    _assert_safe_failure(_cli(recipient_env, "prepare", tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("GITHUB_REPOSITORY", ""),
        ("GITHUB_REPOSITORY", "owner/repo/extra"),
        ("GITHUB_REPOSITORY", "owner/.."),
        ("GITHUB_REPOSITORY", "owner/repo\n"),
        ("GITHUB_SHA", ""),
        ("GITHUB_SHA", "a" * 39),
        ("GITHUB_SHA", "A" * 40),
        ("GITHUB_RUN_ID", ""),
        ("GITHUB_RUN_ID", "0"),
        ("GITHUB_RUN_ID", "01"),
        ("GITHUB_RUN_ID", "9" * 21),
        ("GITHUB_RUN_ATTEMPT", ""),
        ("GITHUB_RUN_ATTEMPT", "-1"),
        ("GITHUB_RUN_ATTEMPT", "1\n"),
    ],
    ids=[
        "repository-missing",
        "repository-path",
        "repository-dot",
        "repository-lf",
        "sha-missing",
        "sha-short",
        "sha-uppercase",
        "run-missing",
        "run-zero",
        "run-leading-zero",
        "run-long",
        "attempt-missing",
        "attempt-negative",
        "attempt-lf",
    ],
)
def test_provenance_rejection(
    tmp_path: Path, recipient_env: dict[str, str], variable: str, value: str
) -> None:
    _raw(tmp_path)
    recipient_env[variable] = value
    _assert_safe_failure(
        _cli(recipient_env, "seal", tmp_path, "--whatif-exit-code", "1")
    )
    assert {path.name for path in tmp_path.iterdir()} == {
        "foundation-whatif.json",
        "foundation-whatif.stderr",
    }


@pytest.mark.parametrize("exit_code", ["-1", "256", "1.5", "SYNTHETIC_SECRET"])
def test_invalid_exit_code_is_never_echoed(
    tmp_path: Path, recipient_env: dict[str, str], exit_code: str
) -> None:
    _assert_safe_failure(
        _cli(recipient_env, "seal", tmp_path, "--whatif-exit-code", exit_code)
    )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["foundation-whatif.json", "foundation-whatif.stderr"])
@pytest.mark.parametrize("invalid", ["missing", "directory", "symlink", "oversize"])
def test_raw_inputs_must_be_bounded_regular_files(
    tmp_path: Path, recipient_env: dict[str, str], name: str, invalid: str
) -> None:
    _raw(tmp_path)
    source = tmp_path / name
    source.unlink()
    if invalid == "directory":
        source.mkdir()
    elif invalid == "symlink":
        target = tmp_path / "synthetic-target.bin"
        target.write_bytes(b"SYNTHETIC_SECRET")
        try:
            source.symlink_to(target)
        except OSError:
            pytest.skip("Host does not permit unprivileged symlink creation")
    elif invalid == "oversize":
        with source.open("wb") as output:
            output.truncate(capture.MAX_STREAM_BYTES + 1)
    previous = {path.name for path in tmp_path.iterdir()}
    _assert_safe_failure(
        _cli(recipient_env, "seal", tmp_path, "--whatif-exit-code", "1")
    )
    assert {path.name for path in tmp_path.iterdir()} == previous
    if invalid == "oversize":
        assert source.stat().st_size == capture.MAX_STREAM_BYTES + 1


@pytest.mark.parametrize("length", [0, 1, 64 * 1024 * 1024 - 1, 64 * 1024 * 1024])
def test_stream_limit_inclusive_without_truncation(tmp_path: Path, length: int) -> None:
    source, destination = tmp_path / "synthetic.raw", tmp_path / "synthetic.snapshot"
    with source.open("wb") as output:
        output.truncate(length)
    metadata = capture._snapshot(source, destination)
    assert metadata["length"] == length
    assert destination.stat().st_size == source.stat().st_size == length
    with source.open("rb") as original, destination.open("rb") as copied:
        assert metadata["sha256"] == hashlib.file_digest(original, "sha256").hexdigest()
        assert metadata["sha256"] == hashlib.file_digest(copied, "sha256").hexdigest()


@pytest.mark.parametrize("collision", ["file", "directory", "symlink", "dangling"])
def test_output_collision_is_never_reused(
    tmp_path: Path, recipient_env: dict[str, str], collision: str
) -> None:
    _raw(tmp_path)
    destination = tmp_path / capture.CAPTURE_NAME
    if collision == "file":
        destination.write_bytes(b"synthetic-existing-ciphertext")
    elif collision == "directory":
        destination.mkdir()
    else:
        target = tmp_path / "synthetic-target.cms"
        if collision == "symlink":
            target.write_bytes(b"synthetic-existing-ciphertext")
        try:
            destination.symlink_to(target)
        except OSError:
            pytest.skip("Host does not permit unprivileged symlink creation")
    previous = {path.name for path in tmp_path.iterdir()}
    _assert_safe_failure(
        _cli(recipient_env, "seal", tmp_path, "--whatif-exit-code", "1")
    )
    assert {path.name for path in tmp_path.iterdir()} == previous
    if collision in {"file", "symlink"}:
        assert destination.read_bytes() == b"synthetic-existing-ciphertext"
    if collision == "dangling":
        assert destination.is_symlink() and not destination.exists()


@pytest.mark.parametrize("command", ["prepare", "seal"])
@pytest.mark.parametrize("failure", ["openssl", "timeout", "interrupt", "exception"])
@pytest.mark.parametrize("alternate_parent", [False, True])
def test_actual_openssl_failure_is_private_and_cleans_owned_files(
    tmp_path: Path,
    recipient_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    failure: str,
    alternate_parent: bool,
) -> None:
    parent = tmp_path
    if alternate_parent:
        parent = tmp_path / "owned-scratch"
        parent.mkdir(mode=0o700)
        recipient_env[capture.SCRATCH_VARIABLE] = str(parent)
    _install_environment(monkeypatch, recipient_env)
    _raw(tmp_path)

    def fail_encrypt(directory: Path, source: str, destination: str) -> None:
        assert directory.parent == parent.resolve()
        (directory / destination).write_bytes(b"SYNTHETIC_SECRET partial output")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(
                "synthetic-openssl",
                120,
                output=b"SYNTHETIC_SECRET",
                stderr=b"SYNTHETIC_SECRET",
            )
        if failure == "interrupt":
            raise KeyboardInterrupt("SYNTHETIC_SECRET")
        if failure == "exception":
            raise RuntimeError("SYNTHETIC_SECRET")
        capture._openssl(["cms", "-SYNTHETIC_SECRET-unsupported-option"], directory)

    monkeypatch.setattr(capture, "_encrypt", fail_encrypt)
    arguments = [command, "--directory", str(tmp_path)]
    if command == "seal":
        arguments += ["--whatif-exit-code", "1"]
    assert capture.main(arguments) == (130 if failure == "interrupt" else 1)
    assert capsys.readouterr() == ("", capture.SAFE_ERROR + "\n")
    expected = {"foundation-whatif.json", "foundation-whatif.stderr"}
    if alternate_parent:
        assert list(parent.iterdir()) == []
        expected.add("owned-scratch")
    assert {path.name for path in tmp_path.iterdir()} == expected


def test_atomic_publication_refuses_racing_collision(
    tmp_path: Path,
    recipient_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_environment(monkeypatch, recipient_env)
    _raw(tmp_path)
    original_link = os.link

    def collide(source: Path, destination: Path) -> None:
        destination.write_bytes(b"synthetic-racing-owner")
        original_link(source, destination)

    monkeypatch.setattr(os, "link", collide)
    assert (
        capture.main(["seal", "--directory", str(tmp_path), "--whatif-exit-code", "1"])
        == 1
    )
    assert capsys.readouterr() == ("", capture.SAFE_ERROR + "\n")
    assert (tmp_path / capture.CAPTURE_NAME).read_bytes() == b"synthetic-racing-owner"
    assert {path.name for path in tmp_path.iterdir()} == {
        "foundation-whatif.json",
        "foundation-whatif.stderr",
        capture.CAPTURE_NAME,
    }


def test_handled_failure_after_publication_removes_owned_ciphertext(
    tmp_path: Path,
    recipient_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_environment(monkeypatch, recipient_env)
    _raw(tmp_path)
    original_unlink = Path.unlink

    def fail_partial_removal(path: Path, missing_ok: bool = False) -> None:
        if path.name == capture.CAPTURE_NAME + ".partial":
            raise OSError("SYNTHETIC_SECRET removal failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_partial_removal)
    assert (
        capture.main(["seal", "--directory", str(tmp_path), "--whatif-exit-code", "1"])
        == 1
    )
    assert capsys.readouterr() == ("", capture.SAFE_ERROR + "\n")
    assert {path.name for path in tmp_path.iterdir()} == {
        "foundation-whatif.json",
        "foundation-whatif.stderr",
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions require a POSIX host")
@pytest.mark.parametrize("command", ["prepare", "seal"])
@pytest.mark.parametrize("alternate_parent", [False, True])
def test_scratch_and_output_permissions(
    tmp_path: Path,
    recipient_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    alternate_parent: bool,
) -> None:
    parent = tmp_path
    if alternate_parent:
        parent = tmp_path / "owned-scratch"
        parent.mkdir(mode=0o700)
        recipient_env[capture.SCRATCH_VARIABLE] = str(parent)
    _install_environment(monkeypatch, recipient_env)
    _raw(tmp_path)
    original_encrypt = capture._encrypt

    def inspect_permissions(directory: Path, source: str, destination: str) -> None:
        assert directory.parent == parent.resolve()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert all(
            stat.S_IMODE(path.stat().st_mode) == 0o600 for path in directory.iterdir()
        )
        original_encrypt(directory, source, destination)

    monkeypatch.setattr(capture, "_encrypt", inspect_permissions)
    if command == "prepare":
        capture.prepare(tmp_path)
    else:
        capture.seal(tmp_path, 1)
        assert stat.S_IMODE((tmp_path / capture.CAPTURE_NAME).stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="Real SIGTERM requires a POSIX host")
def test_sigterm_before_encryption_confines_plaintext_to_owned_scratch(
    tmp_path: Path, recipient_env: dict[str, str]
) -> None:
    streams = _raw(tmp_path)
    unrelated = tmp_path / "unrelated-root-file"
    unrelated.write_bytes(b"synthetic-unrelated-owner")
    parent = Path(tempfile.mkdtemp(prefix="owned-capture-", dir=tmp_path))
    recipient_env["RUNNER_TEMP"] = str(tmp_path)
    recipient_env[capture.SCRATCH_VARIABLE] = str(parent)
    program = textwrap.dedent("""
        import signal
        import sys
        from unittest.mock import patch
        from scripts import foundation_diagnostic_capture as capture

        def pause_before_encrypt(directory, source, destination):
            if source != "bundle.tar" or not (directory / source).is_file():
                raise RuntimeError("SYNTHETIC_SECRET missing bundle")
            print("BUNDLE_READY", flush=True)
            signal.pause()
            raise RuntimeError("SYNTHETIC_SECRET unexpectedly resumed")

        with patch.object(capture, "_encrypt", pause_before_encrypt):
            sys.exit(capture.main(sys.argv[1:]))
        """)
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            program,
            "seal",
            "--directory",
            str(tmp_path),
            "--whatif-exit-code",
            "1",
        ],
        cwd=SCRIPT.parent.parent,
        env=recipient_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            assert process.stdout is not None
            ready, _, _ = select.select([process.stdout], [], [], 120)
            assert ready, "Capture helper did not reach the bundle marker"
            assert process.stdout.readline() == b"BUNDLE_READY\n"
            process.terminate()
            assert process.wait(timeout=20) == -signal.SIGTERM
            stdout, stderr = process.communicate(timeout=20)
            assert stdout == stderr == b""
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=20)
    assert {path.name for path in tmp_path.iterdir()} == {
        "foundation-whatif.json",
        "foundation-whatif.stderr",
        unrelated.name,
        parent.name,
    }
    children = list(parent.iterdir())
    assert len(children) == 1
    scratch = children[0]
    assert scratch.is_dir() and not scratch.is_symlink()
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
    assert {path.name for path in scratch.iterdir()} == {
        "cert.pem",
        "whatif.stdout",
        "whatif.stderr",
        "bundle.tar",
        capture.CAPTURE_NAME + ".partial",
    }
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in scratch.iterdir())
    for name, content in streams.items():
        assert (scratch / name).read_bytes() == content
    with tarfile.open(scratch / "bundle.tar", mode="r:") as archive:
        for name, content in streams.items():
            member = archive.extractfile(name)
            assert member is not None and member.read() == content
    assert (scratch / (capture.CAPTURE_NAME + ".partial")).read_bytes() == b""
    cleanup = subprocess.run(
        ["rm", "-rf", "--", str(parent)],
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert cleanup.returncode == 0 and cleanup.stdout == cleanup.stderr == b""
    assert not parent.exists()
    assert (tmp_path / "foundation-whatif.json").read_bytes() == streams[
        "whatif.stdout"
    ]
    assert (tmp_path / "foundation-whatif.stderr").read_bytes() == streams[
        "whatif.stderr"
    ]
    assert unrelated.read_bytes() == b"synthetic-unrelated-owner"
    assert {path.name for path in tmp_path.iterdir()} == {
        "foundation-whatif.json",
        "foundation-whatif.stderr",
        unrelated.name,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["SYNTHETIC_SECRET"],
        ["prepare"],
        ["seal"],
        ["prepare", "--unknown", "SYNTHETIC_SECRET"],
    ],
    ids=[
        "missing-command",
        "unknown-command",
        "missing-prepare-directory",
        "missing-seal-options",
        "unknown-option",
    ],
)
def test_argument_errors_never_echo_input(
    recipient_env: dict[str, str], arguments: list[str]
) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        env=recipient_env,
        capture_output=True,
        check=False,
        timeout=120,
    )
    _assert_safe_failure(result)


def test_prepare_requires_openssl(
    tmp_path: Path, recipient_env: dict[str, str]
) -> None:
    recipient_env["PATH"] = str(tmp_path)
    _assert_safe_failure(_cli(recipient_env, "prepare", tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_capture_directory_must_exist_and_be_a_directory(
    tmp_path: Path, recipient_env: dict[str, str], kind: str
) -> None:
    directory = tmp_path / "capture"
    if kind == "file":
        directory.write_bytes(b"SYNTHETIC_SECRET")
    _assert_safe_failure(_cli(recipient_env, "prepare", directory))
    if kind == "file":
        assert directory.read_bytes() == b"SYNTHETIC_SECRET"
    else:
        assert not directory.exists()


@pytest.mark.parametrize("command", ["prepare", "seal"])
@pytest.mark.parametrize(
    "kind", ["empty", "whitespace", "missing", "file", "symlink", "dangling", "fifo"]
)
def test_scratch_parent_must_be_an_existing_nonsymlink_directory(
    tmp_path: Path, recipient_env: dict[str, str], command: str, kind: str
) -> None:
    streams = _raw(tmp_path)
    parent = tmp_path / "SYNTHETIC_SECRET"
    value = str(parent)
    if kind == "empty":
        value = ""
    elif kind == "whitespace":
        value = " \t\n"
    elif kind == "file":
        parent.write_bytes(b"SYNTHETIC_SECRET")
    elif kind in {"symlink", "dangling"}:
        target = tmp_path / "target"
        if kind == "symlink":
            target.mkdir()
        try:
            parent.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("Host does not permit unprivileged symlink creation")
    elif kind == "fifo":
        make_fifo = getattr(os, "mkfifo", None)
        if make_fifo is None:
            pytest.skip("FIFO requires a POSIX host")
        make_fifo(parent)
    previous = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    recipient_env[capture.SCRATCH_VARIABLE] = value
    extra = ("--whatif-exit-code", "1") if command == "seal" else ()
    _assert_safe_failure(_cli(recipient_env, command, tmp_path, *extra))
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == previous
    assert (tmp_path / "foundation-whatif.json").read_bytes() == streams[
        "whatif.stdout"
    ]
    assert (tmp_path / "foundation-whatif.stderr").read_bytes() == streams[
        "whatif.stderr"
    ]
    if kind == "file":
        assert parent.read_bytes() == b"SYNTHETIC_SECRET"


@pytest.mark.parametrize("length", [32768, 32769])
def test_openssl_output_is_bounded(
    tmp_path: Path,
    recipient_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    length: int,
) -> None:
    _install_environment(monkeypatch, recipient_env)

    def oversized_output(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            ["synthetic-openssl"], 0, stdout=b"x" * length
        )

    monkeypatch.setattr(subprocess, "run", oversized_output)
    if length == capture.MAX_OPENSSL_OUTPUT:
        assert len(capture._openssl(["x509"], tmp_path)) == length
    else:
        with pytest.raises(capture.CaptureError):
            capture._openssl(["x509"], tmp_path)


@pytest.mark.parametrize(
    "extension", ["keyUsage", "basicConstraints", "subjectKeyIdentifier"]
)
def test_extension_output_parser_rejects_extra_text(
    tmp_path: Path,
    recipient_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extension: str,
) -> None:
    _install_environment(monkeypatch, recipient_env)
    original = capture._openssl

    def unexpected_output(
        arguments: list[str], directory: Path, data: bytes = b""
    ) -> bytes:
        result = original(arguments, directory, data)
        if "-ext" in arguments and arguments[-1] == extension:
            return result + b"SYNTHETIC_SECRET\n"
        return result

    monkeypatch.setattr(capture, "_openssl", unexpected_output)
    assert capture.main(["prepare", "--directory", str(tmp_path)]) == 1
    assert capsys.readouterr() == ("", capture.SAFE_ERROR + "\n")
    assert list(tmp_path.iterdir()) == []


def test_input_growth_is_not_silently_truncated(
    tmp_path: Path,
    recipient_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_environment(monkeypatch, recipient_env)
    _raw(tmp_path)
    source = tmp_path / "foundation-whatif.json"
    original = os.open

    def grow_before_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == source:
            source.write_bytes(source.read_bytes() + b"SYNTHETIC_GROWTH")
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", grow_before_open)
    assert (
        capture.main(["seal", "--directory", str(tmp_path), "--whatif-exit-code", "1"])
        == 1
    )
    assert capsys.readouterr() == ("", capture.SAFE_ERROR + "\n")
    assert source.read_bytes().endswith(b"SYNTHETIC_GROWTH")
    assert {path.name for path in tmp_path.iterdir()} == {
        "foundation-whatif.json",
        "foundation-whatif.stderr",
    }
