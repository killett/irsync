"""Pre-flight checks that an endpoint is the thing the user meant.

irsync's guards all assume a path's existence proves its identity. That
assumption fails for removable drives: an unmounted drive under
``/media/<user>`` leaves an empty directory behind, which every downstream
check reads as a legitimate empty tree. These functions establish identity
before any file is created.
"""

from __future__ import annotations

import os
from pathlib import Path


class EndpointNotMounted(Exception):
    """An endpoint that should live on its own filesystem is not mounted.

    Callers that iterate several drives (``run_all_backups``) treat this the
    same as a missing drive: a skip, not an error.
    """


class UnsafeDestination(Exception):
    """A run with no baseline would write into a destination holding data."""


class RsyncUnavailable(Exception):
    """The rsync binary is not on PATH."""


def mount_gate_root(path: Path, base_dir: Path) -> Path | None:
    """Return the component below ``base_dir`` that must be a mountpoint.

    The shorthand destinations are subdirectories of a mounted drive rather
    than mountpoints themselves (``mypython`` resolves inside drive ``G``;
    the ``~`` backup lands inside drive ``M``), so the gate is applied to the
    first component below ``base_dir``, not to the endpoint.

    Args:
        path: The resolved endpoint.
        base_dir: The drive-letter base directory, e.g. ``/media/<user>``.

    Returns:
        The path that must be a mountpoint, or ``None`` when ``path`` is
        outside ``base_dir`` or is ``base_dir`` itself.
    """
    try:
        rel = path.relative_to(base_dir)
    except ValueError:
        return None
    if not rel.parts:
        return None
    return base_dir / rel.parts[0]


def check_mounted(
    endpoint: Path | str,
    base_dir: Path,
    *,
    gate_outside_base: bool = False,
) -> None:
    """Raise :class:`EndpointNotMounted` unless ``endpoint``'s drive is mounted.

    Args:
        endpoint: A resolved local path, or a string for an rsync remote.
            Remotes are always exempt — there is no local mount to check.
        base_dir: The drive-letter base directory.
        gate_outside_base: When True, endpoints outside ``base_dir`` must
            themselves be mountpoints (the ``--require-mount`` opt-in). However,
            ``base_dir`` itself is always exempt — it is by definition the
            ordinary directory that contains mountpoints, never a mountpoint.

    Raises:
        EndpointNotMounted: If the gate path is not a mountpoint.
    """
    if not isinstance(endpoint, Path):
        return
    gate = mount_gate_root(endpoint, base_dir)
    if gate is None:
        if not gate_outside_base:
            return
        if endpoint == base_dir:
            return
        gate = endpoint
    if not os.path.ismount(gate):
        raise EndpointNotMounted(
            f"{gate} is not a mountpoint, so {endpoint} is not the drive it "
            "appears to be. The drive is probably not mounted."
        )


def foreign_dest_entries(dest_root: Path) -> list[str]:
    """Return destination-root entries that are not irsync's own files.

    irsync's reserved namespace is excluded so that a destination holding
    only a snapshot from an interrupted first backup still counts as empty
    and can be retried without an override.

    Args:
        dest_root: The destination root to inspect. A destination that does
            not exist yet counts as empty.

    Returns:
        Sorted names of entries that are not part of irsync's namespace.

    Raises:
        UnsafeDestination: If the destination exists but cannot be read. An
            unreadable destination is not a proven-empty one, so this fails
            closed rather than reporting no entries.
    """
    from irsync.snapshot import (
        LOCKFILE_NAME,
        SNAPSHOT_FILENAME,
        SNAPSHOT_TEMPFILE_PREFIX,
    )

    try:
        names = sorted(p.name for p in dest_root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    except PermissionError as e:
        raise UnsafeDestination(
            f"Cannot read destination {dest_root} to check whether it is empty: {e}"
        ) from e
    return [
        name
        for name in names
        if name not in (SNAPSHOT_FILENAME, LOCKFILE_NAME)
        and not name.startswith(SNAPSHOT_TEMPFILE_PREFIX)
    ]
