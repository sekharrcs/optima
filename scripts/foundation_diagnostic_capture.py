"""Prepare a pinned recipient and seal private foundation diagnostics with OpenSSL.

Usage: python scripts/foundation_diagnostic_capture.py prepare --directory PATH
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO, Never

CERTIFICATE_VARIABLE = "OPTIMA_DIAGNOSTIC_CERTIFICATE_BASE64"
PIN_VARIABLE = "OPTIMA_DIAGNOSTIC_CERTIFICATE_SHA256"
SCRATCH_VARIABLE = "OPTIMA_DIAGNOSTIC_SCRATCH_DIRECTORY"
MAX_CERTIFICATE_BASE64 = 16384
MAX_OPENSSL_OUTPUT = 32768
MAX_STREAM_BYTES = 64 * 1024 * 1024
CAPTURE_NAME = "foundation-private-capture.cms"
SAFE_ERROR = "Foundation private capture failed."


class CaptureError(Exception):
    """Reject an unsafe or unsupported capture without disclosing its inputs."""


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise CaptureError


def _private_write(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)


def _openssl(arguments: list[str], directory: Path, data: bytes = b"") -> bytes:
    executable = shutil.which("openssl")
    if executable is None:
        raise CaptureError
    result = subprocess.run(
        [executable, *arguments],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=directory,
        timeout=120,
        check=False,
    )
    if result.returncode != 0 or len(result.stdout) > MAX_OPENSSL_OUTPUT:
        raise CaptureError
    return result.stdout


def _text(arguments: list[str], directory: Path, data: bytes = b"") -> str:
    return _openssl(arguments, directory, data).decode("ascii").replace("\r\n", "\n")


def _certificate() -> tuple[bytes, str]:
    encoded = os.environ.get(CERTIFICATE_VARIABLE, "")
    pin = os.environ.get(PIN_VARIABLE, "")
    if not 1 <= len(encoded) <= MAX_CERTIFICATE_BASE64:
        raise CaptureError
    if re.fullmatch(r"[0-9a-f]{64}", pin) is None:
        raise CaptureError
    certificate = base64.b64decode(encoded, validate=True)
    if base64.b64encode(certificate).decode("ascii") != encoded:
        raise CaptureError
    if hashlib.sha256(certificate).hexdigest() != pin:
        raise CaptureError
    return certificate, pin


def _encrypt(directory: Path, source: str, destination: str) -> None:
    _openssl(
        [
            "cms",
            "-encrypt",
            "-binary",
            "-outform",
            "DER",
            "-aes-256-gcm",
            "-keyid",
            "-recip",
            "cert.pem",
            "-keyopt",
            "rsa_padding_mode:oaep",
            "-keyopt",
            "rsa_oaep_md:sha256",
            "-keyopt",
            "rsa_mgf1_md:sha256",
            "-in",
            source,
            "-out",
            destination,
        ],
        directory,
    )


def _validate_recipient(directory: Path) -> str:
    certificate, pin = _certificate()
    round_tripped = _openssl(
        ["x509", "-inform", "DER", "-outform", "DER"], directory, certificate
    )
    if round_tripped != certificate:
        raise CaptureError
    pem = _openssl(["x509", "-inform", "DER"], directory, certificate)
    _private_write(directory / "cert.pem", pem)
    public_key = _openssl(["x509", "-in", "cert.pem", "-pubkey", "-noout"], directory)
    key_details = _text(["rsa", "-pubin", "-text", "-noout"], directory, public_key)
    if (
        re.fullmatch(
            r"Public-Key: \((3072|4096) bit\)\nModulus:\n"
            r"(?:    [0-9a-f:]{1,48}\n){20,40}Exponent: 65537 \(0x10001\)\n",
            key_details,
        )
        is None
    ):
        raise CaptureError
    spki = _openssl(["pkey", "-pubin", "-outform", "DER"], directory, public_key)
    ordinary_rsa = _openssl(["rsa", "-pubin", "-outform", "DER"], directory, public_key)
    if spki != ordinary_rsa:
        raise CaptureError
    if (
        _text(["pkey", "-pubin", "-pubcheck", "-noout"], directory, public_key)
        != "Key is valid\n"
    ):
        raise CaptureError
    extensions = {
        "keyUsage": r"X509v3 Key Usage: critical\n    Key Encipherment\n",
        "basicConstraints": r"X509v3 Basic Constraints: critical\n    CA:FALSE\n",
        "subjectKeyIdentifier": (
            r"X509v3 Subject Key Identifier: ?\n    [0-9A-F]{2}"
            r"(?::[0-9A-F]{2}){0,63}\n"
        ),
    }
    for extension, pattern in extensions.items():
        value = _text(
            ["x509", "-in", "cert.pem", "-noout", "-ext", extension], directory
        )
        if re.fullmatch(pattern, value) is None:
            raise CaptureError
    subject = _text(
        ["x509", "-in", "cert.pem", "-noout", "-subject", "-nameopt", "RFC2253"],
        directory,
    )
    issuer = _text(
        ["x509", "-in", "cert.pem", "-noout", "-issuer", "-nameopt", "RFC2253"],
        directory,
    )
    if not subject.startswith("subject=") or issuer != "issuer=" + subject[8:]:
        raise CaptureError
    if (
        _text(
            [
                "verify",
                "-check_ss_sig",
                "-CAfile",
                "cert.pem",
                "-no-CApath",
                "-no-CAstore",
                "-purpose",
                "any",
                "cert.pem",
            ],
            directory,
        )
        != "cert.pem: OK\n"
    ):
        raise CaptureError
    if (
        _text(["x509", "-in", "cert.pem", "-noout", "-checkend", "86400"], directory)
        != "Certificate will not expire\n"
    ):
        raise CaptureError
    return pin


def _capture_directory(directory: Path) -> Path:
    if not stat.S_ISDIR(directory.lstat().st_mode) or directory.is_symlink():
        raise CaptureError
    return directory.resolve(strict=True)


def _scratch_directory(directory: Path) -> Path:
    parent = os.environ.get(SCRATCH_VARIABLE)
    if parent is None:
        return directory
    if not parent.strip():
        raise CaptureError
    return _capture_directory(Path(parent))


def prepare(directory: Path) -> None:
    """Validate the public recipient and crypto support, retaining no output."""
    directory = _capture_directory(directory)
    with tempfile.TemporaryDirectory(
        prefix=".foundation-capture-", dir=_scratch_directory(directory)
    ) as name:
        scratch = Path(name)
        scratch.chmod(0o700)
        _validate_recipient(scratch)
        _private_write(
            scratch / "trial.bin", b"optima-cms-synthetic-preflight-v1\x00\xff\n"
        )
        _private_write(scratch / "trial.cms", b"")
        _encrypt(scratch, "trial.bin", "trial.cms")
        if (scratch / "trial.cms").stat().st_size == 0:
            raise CaptureError


def _provenance(whatif_exit_code: int, recipient_sha256: str) -> dict[str, object]:
    if type(whatif_exit_code) is not int or not 0 <= whatif_exit_code <= 255:
        raise CaptureError
    patterns = {
        "GITHUB_REPOSITORY": (
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
            r"[A-Za-z0-9_.-]{1,100}"
        ),
        "GITHUB_SHA": r"[0-9a-f]{40}",
        "GITHUB_RUN_ID": r"[1-9][0-9]{0,19}",
        "GITHUB_RUN_ATTEMPT": r"[1-9][0-9]{0,19}",
    }
    values = {}
    for variable, pattern in patterns.items():
        value = os.environ.get(variable, "")
        if len(value) > 140 or re.fullmatch(pattern, value) is None:
            raise CaptureError
        values[variable] = value
    if values["GITHUB_REPOSITORY"].split("/")[1] in {".", ".."}:
        raise CaptureError
    return {
        "schema": "optima-foundation-private-capture-v1",
        "promotable": False,
        "repository": values["GITHUB_REPOSITORY"],
        "commit_sha": values["GITHUB_SHA"],
        "run_id": values["GITHUB_RUN_ID"],
        "run_attempt": values["GITHUB_RUN_ATTEMPT"],
        "whatif_exit_code": whatif_exit_code,
        "recipient_sha256": recipient_sha256,
    }


def _snapshot(source: Path, destination: Path) -> dict[str, object]:
    """Snapshot a regular stream without following links or truncating bytes."""
    original = source.lstat()
    if not stat.S_ISREG(original.st_mode) or original.st_size > MAX_STREAM_BYTES:
        raise CaptureError
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(source, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(original, opened):
            raise CaptureError
        output_descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        digest = hashlib.sha256()
        length = 0
        with os.fdopen(output_descriptor, "wb") as output:
            while block := stream.read(min(1024 * 1024, MAX_STREAM_BYTES - length + 1)):
                length += len(block)
                if length > MAX_STREAM_BYTES:
                    raise CaptureError
                digest.update(block)
                output.write(block)
        finished = os.fstat(stream.fileno())
        if (
            length != original.st_size
            or finished.st_size != original.st_size
            or finished.st_mtime_ns != original.st_mtime_ns
            or not os.path.samestat(original, source.lstat())
        ):
            raise CaptureError
    return {"length": length, "sha256": digest.hexdigest()}


def _tar_member(
    archive: tarfile.TarFile, name: str, size: int, content: BinaryIO
) -> None:
    member = tarfile.TarInfo(name)
    member.size = size
    member.mode = 0o600
    archive.addfile(member, content)


def _bundle(directory: Path, scratch: Path, manifest: dict[str, object]) -> None:
    streams = {
        "whatif.stdout": _snapshot(
            directory / "foundation-whatif.json", scratch / "whatif.stdout"
        ),
        "whatif.stderr": _snapshot(
            directory / "foundation-whatif.stderr", scratch / "whatif.stderr"
        ),
    }
    manifest["streams"] = streams
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    descriptor = os.open(
        scratch / "bundle.tar", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "wb") as output:
        with tarfile.open(
            fileobj=output, mode="w", format=tarfile.USTAR_FORMAT
        ) as archive:
            _tar_member(archive, "manifest.json", len(encoded), io.BytesIO(encoded))
            for name in ("whatif.stdout", "whatif.stderr"):
                path = scratch / name
                with path.open("rb") as stream:
                    _tar_member(archive, name, path.stat().st_size, stream)


def seal(directory: Path, whatif_exit_code: int) -> None:
    """Publish only ciphertext; leave raw stream cleanup to the calling workflow."""
    directory = _capture_directory(directory)
    destination = directory / CAPTURE_NAME
    if destination.exists() or destination.is_symlink():
        raise CaptureError
    owned: os.stat_result | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix=".foundation-capture-", dir=_scratch_directory(directory)
        ) as name:
            scratch = Path(name)
            scratch.chmod(0o700)
            pin = _validate_recipient(scratch)
            manifest = _provenance(whatif_exit_code, pin)
            _bundle(directory, scratch, manifest)
            partial = scratch / (CAPTURE_NAME + ".partial")
            _private_write(partial, b"")
            _encrypt(scratch, "bundle.tar", partial.name)
            with partial.open("r+b") as encrypted:
                information = os.fstat(encrypted.fileno())
                if not stat.S_ISREG(information.st_mode) or information.st_size == 0:
                    raise CaptureError
                os.fsync(encrypted.fileno())
            os.link(partial, destination)
            owned = information
            partial.unlink()
    except BaseException:
        if owned is not None and destination.exists():
            if os.path.samestat(owned, destination.lstat()):
                destination.unlink()
        raise


def create_parser() -> argparse.ArgumentParser:
    """Build the private capture CLI without echoing rejected arguments."""
    parser = _SafeParser(prog="foundation-diagnostic-capture", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare", allow_abbrev=False)
    command.add_argument("--directory", type=Path, required=True)
    command = commands.add_parser("seal", allow_abbrev=False)
    command.add_argument("--directory", type=Path, required=True)
    command.add_argument("--whatif-exit-code", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Keep exception details and all OpenSSL diagnostics off public output."""
    try:
        arguments = create_parser().parse_args(argv)
        if arguments.command == "prepare":
            prepare(arguments.directory)
        else:
            seal(arguments.directory, arguments.whatif_exit_code)
        return 0
    except KeyboardInterrupt:
        print(SAFE_ERROR, file=sys.stderr)
        return 130
    except Exception:
        print(SAFE_ERROR, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
