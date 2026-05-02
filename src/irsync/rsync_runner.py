"""Run rsync on behalf of the orchestrator: build commands, execute, stream output."""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shlex
import subprocess  # noqa: S404 — invoking rsync is the whole point of this module
import sys
from pathlib import Path

from irsync.snapshot import (
    LOCKFILE_NAME,
    SNAPSHOT_FILENAME,
    SNAPSHOT_TEMPFILE_PREFIX,
)

_FILES_RE = re.compile(r"Number of files:\s+([\d,]+)")
_SIZE_RE = re.compile(r"(?:total size is|Total file size:)\s+([^\s]+)")


def build_rsync_command(
    *,
    source: str,
    dest: str,
    dry_run: bool,
    exclude_dirs: list[str] | None = None,
    no_exclude: bool = False,
    ssh_port: int | None = None,
    ssh_key: str | Path | None = None,
) -> list[str]:
    """Construct the rsync command line that mirrors the original ``srsync`` defaults.

    Always passes ``--exclude=<SNAPSHOT_FILENAME>`` so the inode snapshot is
    managed by irsync, not by rsync.

    Args:
        source: Source endpoint (local path with trailing slash, or ``host:/path/``).
        dest: Destination endpoint.
        dry_run: If True, append ``--dry-run``.
        exclude_dirs: Directory names to pass with ``--exclude``.
        no_exclude: If True, ignore ``exclude_dirs`` (mirrors srsync's ``--no-exclude``).
        ssh_port: Optional SSH port for remote endpoints.
        ssh_key: Optional SSH key path for remote endpoints.

    Returns:
        The full argv list for ``subprocess.Popen``.
    """
    cmd: list[str] = [
        "rsync",
        "--sparse",
        "-a",
        "-v",
        "-h",
        "-P",
        "-i",
        "--stats",
        "--one-file-system",
        "--delete-before",
    ]
    if dry_run:
        cmd.append("--dry-run")

    is_remote_source = ":" in source and not source.startswith("/")
    is_remote_dest = ":" in dest and not dest.startswith("/")
    if is_remote_source or is_remote_dest:
        ssh_cmd = "ssh"
        if ssh_port is not None:
            ssh_cmd += f" -p {ssh_port}"
        if ssh_key is not None:
            ssh_cmd += f" -i {shlex.quote(os.fspath(ssh_key))}"
        cmd.extend(["-e", ssh_cmd])

    # Anchor each pattern with a leading "/" so only the file at the source
    # root is excluded; without the anchor, rsync's pattern matches the
    # basename at every depth and would silently skip user files that happen
    # to share the name. These three are irsync's reserved namespace at the
    # source root: the snapshot itself, the lockfile, and any orphan
    # tempfiles from a killed _atomic_write_snapshot.
    cmd.extend(["--exclude", f"/{SNAPSHOT_FILENAME}"])
    cmd.extend(["--exclude", f"/{LOCKFILE_NAME}"])
    cmd.extend(["--exclude", f"/{SNAPSHOT_TEMPFILE_PREFIX}*"])

    if not no_exclude and exclude_dirs:
        for d in exclude_dirs:
            cmd.extend(["--exclude", d])

    cmd.extend([source, dest])
    return cmd


def parse_rsync_output(output: str) -> tuple[int | None, str | None]:
    """Extract the file count and total size from rsync's ``--stats`` output.

    Args:
        output: Captured stdout text from a ``--stats`` rsync run.

    Returns:
        ``(file_count, total_size)``; either may be ``None`` if not present.
    """
    m_files = _FILES_RE.search(output)
    m_total = _SIZE_RE.search(output)
    file_count: int | None = None
    if m_files:
        file_count = int(m_files.group(1).replace(",", ""))
    total_size = m_total.group(1) if m_total else None
    return file_count, total_size


def run_dry_run(cmd: list[str]) -> str:
    """Run an rsync ``--dry-run`` command and capture stdout for preview.

    Args:
        cmd: The full rsync command (must already include ``--dry-run``).

    Returns:
        The captured stdout text.

    Raises:
        subprocess.CalledProcessError: If rsync exits non-zero.
    """
    logging.info("Dry run: %r", cmd)
    result = subprocess.run(  # noqa: S603 — cmd is built internally, not from user shell
        cmd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout


def run_real_sync(cmd: list[str]) -> int:
    """Run a real (non-dry-run) rsync command, streaming stderr live.

    Args:
        cmd: The full rsync command without ``--dry-run``.

    Returns:
        The rsync exit code (0 on success).
    """
    logging.info("Executing: %r", cmd)
    started = dt.datetime.now()
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        stdout=None,
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr_buf: list[str] = []
    if proc.stderr is None:
        raise RuntimeError("rsync subprocess did not expose stderr")
    for line in proc.stderr:
        sys.stderr.write(line)
        sys.stderr.flush()
        stderr_buf.append(line)
    ret = proc.wait()
    if proc.stderr:
        proc.stderr.close()
    elapsed = dt.datetime.now() - started
    if ret != 0:
        logging.error(
            "rsync failed (code %s) after %s. stderr:\n%s",
            ret,
            elapsed,
            "".join(stderr_buf),
        )
    else:
        logging.info("rsync completed successfully in %s.", elapsed)
    return ret
