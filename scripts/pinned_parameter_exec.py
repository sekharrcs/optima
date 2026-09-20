"""Execute an Azure CLI command with the immutable production parameter artifact
pinned to a verified file descriptor instead of a re-openable pathname.

Azure CLI consumes ``--parameters @<path>`` by re-opening ``<path>`` at call
time. Between the digest verification and that re-open, a pathname replacement
could substitute different bytes. This Linux-only helper opens the artifact once
with ``O_RDONLY | O_NOFOLLOW``, validates that it is a regular, owner-held,
non-group/world-writable, single-link, bounded file whose SHA-256 matches the
reviewed digest, keeps that descriptor inheritable, and then execs the command
with ``@/proc/self/fd/<fd>`` in place of a caller-supplied placeholder. The
executed process therefore consumes the exact inode that was verified, and a
later pathname replacement cannot change the consumed bytes.

The parameter artifact never contains the confidential UI client secret (that is
supplied separately at mutation time), and this helper never prints the artifact
contents, so no secret is exposed through the command line or logs.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
from collections.abc import Sequence

EXIT_FAILURE = 1
MAX_PARAMETER_FILE_BYTES = 2 * 1024 * 1024
_PROC_SELF_FD = "/proc/self/fd"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class PinnedParameterError(RuntimeError):
    """The parameter artifact cannot be pinned to a trusted descriptor."""


def _open_verified_descriptor(
    path: str, *, expected_sha256: str, maximum_bytes: int
) -> int:
    """Open and fully validate the artifact, returning an inheritable descriptor.

    ``O_NOFOLLOW`` rejects a symlink swapped in at the final path component. The
    descriptor is validated by ``fstat`` (never by a second path lookup) so the
    checks and the consumed bytes describe the same inode.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PinnedParameterError("parameter artifact is not a regular file")
        owner = getattr(os, "getuid", None)
        if owner is None:
            raise PinnedParameterError("descriptor pinning requires a POSIX owner")
        if info.st_uid != owner():
            raise PinnedParameterError("parameter artifact is not owner-held")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PinnedParameterError("parameter artifact is group or world writable")
        if info.st_nlink != 1:
            raise PinnedParameterError("parameter artifact has extra hard links")
        if info.st_size <= 0 or info.st_size > maximum_bytes:
            raise PinnedParameterError("parameter artifact has an unexpected size")
        data = bytearray()
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > maximum_bytes:
                raise PinnedParameterError("parameter artifact exceeds the bound")
        if len(data) != info.st_size:
            raise PinnedParameterError("parameter artifact changed while reading")
        if hashlib.sha256(bytes(data)).hexdigest() != expected_sha256:
            raise PinnedParameterError("parameter artifact digest does not match")
        # Rewind so the executed command reads the artifact from the start, and
        # clear close-on-exec so the verified descriptor survives the exec.
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.set_inheritable(descriptor, True)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def create_parser() -> argparse.ArgumentParser:
    """Create the descriptor-pinning exec CLI parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Exec a command with a descriptor-pinned production parameter artifact."
        )
    )
    parser.add_argument("--parameter-file", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--placeholder", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate, pin, and exec; only failure paths ever return a value."""
    arguments = create_parser().parse_args(argv)
    command = list(arguments.command)
    if command and command[0] == "--":
        command = command[1:]
    try:
        if not sys.platform.startswith("linux") or not os.path.isdir(_PROC_SELF_FD):
            raise PinnedParameterError(
                "descriptor pinning requires a Linux /proc/self/fd host"
            )
        if _SHA256.fullmatch(arguments.expected_sha256) is None:
            raise PinnedParameterError("expected digest is not a lowercase sha256")
        if not arguments.placeholder:
            raise PinnedParameterError("a parameter placeholder token is required")
        if not command:
            raise PinnedParameterError("no command was supplied to execute")
        if sum(1 for token in command if token == arguments.placeholder) != 1:
            raise PinnedParameterError(
                "exactly one parameter placeholder must appear in the command"
            )
        descriptor = _open_verified_descriptor(
            arguments.parameter_file,
            expected_sha256=arguments.expected_sha256,
            maximum_bytes=MAX_PARAMETER_FILE_BYTES,
        )
        reference = f"@{_PROC_SELF_FD}/{descriptor}"
        resolved = [
            reference if token == arguments.placeholder else token for token in command
        ]
        os.execvp(resolved[0], resolved)
    except (OSError, ValueError, PinnedParameterError) as error:
        print(f"PINNED PARAMETER EXEC FAILED: {error}", file=sys.stderr)
        return EXIT_FAILURE
    # os.execvp only returns on failure to exec.
    return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
