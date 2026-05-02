"""Path utilities shared across irsync: rsync remote detection and local dir validation."""

from __future__ import annotations

import os
import re
from pathlib import Path

REMOTE_RSYNC_RE: re.Pattern[str] = re.compile(r"^(?:[^@\s:/]+@)?[^:\s/]+:.+")


def is_rsync_remote(spec: str | Path) -> bool:
    """Return True for rsync SSH-style endpoints like ``host:/path`` or ``user@host:/path``.

    Args:
        spec: A path or rsync endpoint string.

    Returns:
        True if ``spec`` matches rsync's remote endpoint syntax, False otherwise.
    """
    return bool(REMOTE_RSYNC_RE.match(os.fspath(spec)))


def ensure_local_dir(pathlike: str | Path) -> Path:
    """Validate that a local path exists and is a directory; return its resolved form.

    Args:
        pathlike: A filesystem path (``~`` is expanded).

    Returns:
        The resolved absolute :class:`pathlib.Path` to the directory.

    Raises:
        FileNotFoundError: If the path does not exist.
        NotADirectoryError: If the path exists but is not a directory.
    """
    p = Path(pathlike).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Path '{p}' does not exist.")
    if not p.is_dir():
        raise NotADirectoryError(f"Path '{p}' is not a directory.")
    return p


def with_trailing_slash(spec: str | Path, *, remote: bool) -> str:
    """Append a trailing slash to ``spec`` while preserving rsync remote syntax.

    Args:
        spec: Either a local path or a remote rsync endpoint.
        remote: True if ``spec`` is a remote endpoint (``host:/path``).

    Returns:
        The endpoint string guaranteed to end with ``/``.
    """
    s = os.fspath(spec)
    if remote:
        userhost, path = s.split(":", 1)
        if not path.endswith("/"):
            path += "/"
        return f"{userhost}:{path}"
    return s if s.endswith(os.sep) else (s + os.sep)
