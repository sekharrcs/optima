"""Tests for the descriptor-pinned production parameter exec helper.

The race regression proves that a pathname replacement performed after the helper
opens and verifies the artifact does not change the bytes the executed command
consumes, because consumption is bound to the verified descriptor via
``/proc/self/fd``. These tests did not exist at PR head e6ed1c0, where every
``az`` what-if and create consumed ``--parameters @<path>`` and Azure CLI
re-opened the pathname after the digest check.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.pinned_parameter_exec import main

HELPER = Path("scripts/pinned_parameter_exec.py")
_LINUX = sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir()
_linux_only = pytest.mark.skipif(
    not _LINUX, reason="descriptor pinning requires a Linux /proc/self/fd host"
)


def test_non_linux_or_missing_proc_fails_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without a Linux /proc/self/fd host the helper fails closed, never weakly."""
    exit_code = main(
        [
            "--parameter-file",
            "does-not-matter.json",
            "--expected-sha256",
            "0" * 64,
            "--placeholder",
            "@@P@@",
            "--",
            "true",
            "@@P@@",
        ]
    )
    if _LINUX:
        # On a Linux host the missing artifact fails at open time, still closed.
        assert exit_code == 1
    else:
        assert exit_code == 1
        assert "Linux /proc/self/fd" in capsys.readouterr().err


def _write_artifact(path: Path, content: bytes) -> str:
    path.write_bytes(content)
    if _LINUX:
        path.chmod(0o600)
    return hashlib.sha256(content).hexdigest()


@_linux_only
def test_pathname_substitution_after_open_does_not_change_consumed_bytes(
    tmp_path: Path,
) -> None:
    """A rename swap after the descriptor is opened cannot change consumed bytes."""
    artifact = tmp_path / "production.parameters.json"
    original = b'{"$schema":"x","contentVersion":"1.0.0.0","parameters":{"a":1}}\n'
    digest = _write_artifact(artifact, original)
    output = tmp_path / "consumed.bytes"
    consumer = tmp_path / "swapping_consumer.py"
    consumer.write_text(
        "import os, sys\n"
        "original = sys.argv[sys.argv.index('--original') + 1]\n"
        "output = sys.argv[sys.argv.index('--output') + 1]\n"
        "pinned = sys.argv[sys.argv.index('--pinned') + 1]\n"
        "assert pinned.startswith('@')\n"
        "replacement = original + '.substitute'\n"
        "with open(replacement, 'wb') as handle:\n"
        "    handle.write(b'SUBSTITUTED-CONTENT-B' * 8)\n"
        "os.replace(replacement, original)\n"
        "with open(pinned[1:], 'rb') as handle:\n"
        "    data = handle.read()\n"
        "with open(output, 'wb') as handle:\n"
        "    handle.write(data)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--parameter-file",
            str(artifact),
            "--expected-sha256",
            digest,
            "--placeholder",
            "@@PINNED@@",
            "--",
            sys.executable,
            str(consumer),
            "--original",
            str(artifact),
            "--output",
            str(output),
            "--pinned",
            "@@PINNED@@",
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    # The pathname was really swapped, yet the descriptor delivered the original.
    assert artifact.read_bytes() != original
    assert output.read_bytes() == original


@_linux_only
def test_digest_mismatch_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An artifact whose bytes do not match the reviewed digest fails closed."""
    artifact = tmp_path / "production.parameters.json"
    _write_artifact(artifact, b"original-bytes\n")
    exit_code = main(
        [
            "--parameter-file",
            str(artifact),
            "--expected-sha256",
            "1" * 64,
            "--placeholder",
            "@@P@@",
            "--",
            "true",
            "@@P@@",
        ]
    )
    assert exit_code == 1
    assert "digest does not match" in capsys.readouterr().err


@_linux_only
def test_symlinked_artifact_is_rejected(tmp_path: Path) -> None:
    """A symlink swapped in at the artifact path is rejected by O_NOFOLLOW."""
    target = tmp_path / "real.json"
    digest = _write_artifact(target, b"real-bytes\n")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    exit_code = main(
        [
            "--parameter-file",
            str(link),
            "--expected-sha256",
            digest,
            "--placeholder",
            "@@P@@",
            "--",
            "true",
            "@@P@@",
        ]
    )
    assert exit_code == 1


@_linux_only
def test_missing_placeholder_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A command without exactly one placeholder fails closed before any exec."""
    artifact = tmp_path / "production.parameters.json"
    digest = _write_artifact(artifact, b"bytes\n")
    exit_code = main(
        [
            "--parameter-file",
            str(artifact),
            "--expected-sha256",
            digest,
            "--placeholder",
            "@@P@@",
            "--",
            "true",
        ]
    )
    assert exit_code == 1
    assert "placeholder" in capsys.readouterr().err
