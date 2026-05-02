"""Apply detected moves directly on the destination tree using ``os.rename``."""

from __future__ import annotations

import errno
import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from irsync.diff import make_parent_substituter, plan_directory_moves


class CrossDeviceMoveError(OSError):
    """Raised by the apply_moves pre-flight when a planned move would cross devices.

    Subclass of :class:`OSError` (with ``errno = EXDEV``) so callers that already
    handle generic OS errors keep working. The orchestrator catches this
    explicitly to surface a clear message and avoid the perpetual-retry trap
    where a mid-flight EXDEV would leave dest in partial state.
    """


@dataclass(frozen=True)
class ReplayResult:
    """Counts of operations performed during a replay pass."""

    dirs_moved: int
    files_moved: int
    skipped: int


def _dev_of(path: Path) -> int:
    """Return ``st_dev`` of ``path`` or its deepest existing ancestor.

    A planned move's new location may not exist yet; in that case the
    filesystem boundary is still determined by the ancestor that does.
    """
    p = path
    while True:
        try:
            return int(p.stat().st_dev)
        except FileNotFoundError:
            parent = p.parent
            if parent == p:
                raise
            p = parent


def _preflight_check_xdev(
    dir_moves: list[tuple[str, str]],
    file_moves: list[tuple[str, str]],
    dest_root: Path,
) -> None:
    """Refuse upfront if any planned move would cross filesystems on dest.

    ``apply_moves`` runs `os.rename` which fails with EXDEV across mounts.
    Without this pre-flight, a mid-flight EXDEV would leave moves 1..k
    applied and k+1..N pending, the orchestrator wouldn't persist the new
    snapshot, and the next run would re-issue the same plan, hit the same
    EXDEV at the same point, and never make forward progress (NEW-H2).
    """
    root_dev = _dev_of(dest_root)
    checked: set[Path] = set()

    def check(rel: str) -> None:
        p = (dest_root / PurePosixPath(rel)).resolve(strict=False)
        if p in checked:
            return
        checked.add(p)
        try:
            dev = _dev_of(p)
        except FileNotFoundError:
            return
        if dev != root_dev:
            raise CrossDeviceMoveError(
                errno.EXDEV,
                f"path {p} is on a different filesystem than dest_root {dest_root} "
                f"(st_dev {dev} vs {root_dev})",
            )

    for old_rel, new_rel in (*dir_moves, *file_moves):
        check(old_rel)
        check(new_rel)


def _move_no_clobber(src: Path, dst: Path) -> str:
    """Atomically rename ``src`` to ``dst`` if ``dst`` does not already exist.

    Returns:
        ``"moved"`` if the rename happened, ``"skipped_missing_src"`` if the source
        was absent (e.g. dest tree hasn't been seeded yet), ``"skipped_dst_exists"``
        if the destination already exists.

    Raises:
        OSError: With ``errno == EXDEV`` for cross-filesystem moves; other OS errors propagate.
    """
    if not src.exists() and not src.is_symlink():
        logging.debug("replay: source missing, skipping: %s", src)
        return "skipped_missing_src"
    try:
        # lexists check via lstat — handles regular files, dirs, and dangling symlinks
        dst.lstat()
        logging.warning("replay: destination exists, skipping: %s", dst)
        return "skipped_dst_exists"
    except FileNotFoundError:
        pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)  # raises OSError(EXDEV) if cross-filesystem
    return "moved"


def apply_moves(
    *,
    dir_moves: list[tuple[str, str]],
    file_moves: list[tuple[str, str]],
    dest_root: Path,
) -> ReplayResult:
    """Apply directory and file moves to ``dest_root`` using atomic renames.

    Directory moves are scheduled via :func:`plan_directory_moves` so cycles are
    broken with temp paths. File moves are then applied with their source path
    rewritten through :func:`make_parent_substituter` so any prior directory
    rename is reflected. Operation is non-clobbering: existing destinations are
    left alone (and counted as ``skipped``).

    Args:
        dir_moves: ``(old, new)`` directory pairs from :class:`irsync.diff.Changes`.
        file_moves: ``(old, new)`` file pairs from :class:`irsync.diff.Changes`.
        dest_root: Root of the destination (backup) tree.

    Returns:
        A :class:`ReplayResult` summarizing the outcome.

    Raises:
        CrossDeviceMoveError: When the pre-flight detects that any planned
            move would cross filesystems on the destination. Raised before
            any rename runs so dest is left untouched.
        OSError: With ``errno == EXDEV`` if a rename unexpectedly crosses
            filesystems (the destination tree must be a single filesystem).
    """
    _preflight_check_xdev(dir_moves, file_moves, dest_root)

    dirs_moved = 0
    files_moved = 0
    skipped = 0

    for old_rel, new_rel in plan_directory_moves(dir_moves):
        src = dest_root / PurePosixPath(old_rel)
        dst = dest_root / PurePosixPath(new_rel)
        outcome = _move_no_clobber(src, dst)
        if outcome == "moved":
            dirs_moved += 1
        else:
            skipped += 1

    subst = make_parent_substituter(dir_moves)
    for old_rel, new_rel in file_moves:
        rewritten = subst(old_rel)
        src = dest_root / PurePosixPath(rewritten)
        dst = dest_root / PurePosixPath(new_rel)
        outcome = _move_no_clobber(src, dst)
        if outcome == "moved":
            files_moved += 1
        else:
            skipped += 1

    return ReplayResult(dirs_moved=dirs_moved, files_moved=files_moved, skipped=skipped)
