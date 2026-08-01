"""Pre-flight checks that an endpoint is the thing the user meant.

irsync's guards all assume a path's existence proves its identity. That
assumption fails for removable drives: an unmounted drive under
``/media/<user>`` leaves an empty directory behind, which every downstream
check reads as a legitimate empty tree. These functions establish identity
before any file is created.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from irsync.snapshot import Row


class EndpointNotMounted(Exception):
    """An endpoint that should live on its own filesystem is not mounted.

    Base class for :class:`SourceNotMounted` and
    :class:`DestinationNotMounted`. Existing code that catches this base
    class (``cli.main``'s refusal boundary, the ``--allow-unmounted``
    handler in ``run_backup``) keeps working unchanged, since both
    subclasses are also instances of this type.
    """


class SourceNotMounted(EndpointNotMounted):
    """The source endpoint's drive is not mounted.

    ``run_all_backups`` treats this the same as a missing drive: a skip,
    not an error. ``Options.all_backups`` lists every drive the user might
    ever attach, and on any given day most of them are absent, so a source
    that isn't there is the normal case, not a failure.
    """


class DestinationNotMounted(EndpointNotMounted):
    """The destination endpoint's drive is not mounted.

    Unlike :class:`SourceNotMounted`, ``run_all_backups`` counts this as a
    real error, not a skip: if the source drive IS mounted but its backup
    drive is not, the run would back up nothing while looking like success.
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
    role: Literal["source", "destination"] = "source",
) -> None:
    """Raise a not-mounted error unless ``endpoint``'s drive is mounted.

    Args:
        endpoint: A resolved local path, or a string for an rsync remote.
            Remotes are always exempt — there is no local mount to check.
        base_dir: The drive-letter base directory.
        gate_outside_base: When True, endpoints outside ``base_dir`` must
            themselves be mountpoints (the ``--require-mount`` opt-in). However,
            ``base_dir`` itself is always exempt — it is by definition the
            ordinary directory that contains mountpoints, never a mountpoint.
        role: Either ``"source"`` or ``"destination"``, selecting which
            exception subclass is raised so callers (``run_all_backups``)
            can tell the two apart: an unmounted source is a skip, an
            unmounted destination is a real error.

    Raises:
        SourceNotMounted: If ``role="source"`` and the gate path is not a
            mountpoint.
        DestinationNotMounted: If ``role="destination"`` and the gate path
            is not a mountpoint.
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
        message = (
            f"{gate} is not a mountpoint, so {endpoint} is not the drive it "
            "appears to be. The drive is probably not mounted."
        )
        if role == "destination":
            raise DestinationNotMounted(message)
        raise SourceNotMounted(message)


def _non_reserved_names(root: Path) -> list[str]:
    """List ``root``'s top-level entries, excluding irsync's own reserved files.

    Shared by :func:`foreign_dest_entries` (destination side) and
    :func:`source_root_is_empty` (source side) so the reserved-namespace
    list — :data:`~irsync.snapshot.SNAPSHOT_FILENAME`,
    :data:`~irsync.snapshot.LOCKFILE_NAME`,
    :data:`~irsync.snapshot.SNAPSHOT_TEMPFILE_PREFIX` — is spelled out in
    exactly one place rather than duplicated per caller.

    Args:
        root: Directory to list.

    Returns:
        Sorted names of entries that are not part of irsync's namespace.

    Raises:
        FileNotFoundError: If ``root`` does not exist. Left to the caller to
            interpret — an absent destination counts as empty, but an
            absent source root is a different situation entirely.
        NotADirectoryError: If ``root`` exists but is not a directory.
        PermissionError: If ``root`` exists but cannot be listed.
    """
    from irsync.snapshot import (
        LOCKFILE_NAME,
        SNAPSHOT_FILENAME,
        SNAPSHOT_TEMPFILE_PREFIX,
    )

    names = sorted(p.name for p in root.iterdir())
    return [
        name
        for name in names
        if name not in (SNAPSHOT_FILENAME, LOCKFILE_NAME)
        and not name.startswith(SNAPSHOT_TEMPFILE_PREFIX)
    ]


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
    try:
        return _non_reserved_names(dest_root)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except PermissionError as e:
        raise UnsafeDestination(
            f"Cannot read destination {dest_root} to check whether it is empty: {e}"
        ) from e


def source_root_is_empty(src_root: Path) -> bool:
    """Return True if ``src_root`` has no entries besides irsync's own files.

    Used on the ``--no-snapshot`` path, which never calls
    :func:`~irsync.snapshot.snapshot_tree` and so never produces rows for
    :func:`fresh_snapshot_is_empty` to inspect. This performs the same
    reserved-namespace filtering directly against the filesystem, so a
    source holding only a leftover ``.irsync.lock`` still counts as empty.

    Args:
        src_root: The source root to inspect.

    Returns:
        True if ``src_root`` does not exist, or exists with no entries
        outside irsync's reserved namespace.
    """
    try:
        return not _non_reserved_names(src_root)
    except (FileNotFoundError, NotADirectoryError):
        return True


def fresh_snapshot_is_empty(rows: list[Row]) -> bool:
    """Return True if a fresh snapshot walk found nothing but the root.

    :func:`~irsync.snapshot.snapshot_tree` always includes the root
    directory itself as a row with ``path == "."``, so an empty source
    yields exactly one row, never zero — a caller that checked
    ``len(rows) == 0`` would never see this fire.

    Args:
        rows: Rows from a fresh :func:`~irsync.snapshot.snapshot_tree` walk.

    Returns:
        True if ``rows`` contains only the root entry.
    """
    return len(rows) == 1 and rows[0]["path"] == "."


def check_rsync_available() -> None:
    """Raise :class:`RsyncUnavailable` if the rsync binary is not on PATH.

    Raises:
        RsyncUnavailable: If ``shutil.which`` cannot find rsync.
    """
    if shutil.which("rsync") is None:
        raise RsyncUnavailable(
            "rsync was not found on PATH. Install it (on Debian/Ubuntu: "
            "sudo apt install rsync) and try again."
        )
