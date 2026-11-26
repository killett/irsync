"""What irsync may ask of a backup destination: look at paths, and replay moves."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from irsync.replay import ReplayResult, apply_moves
from irsync.snapshot import RowType


class DestinationUnreadable(Exception):
    """Raised by :meth:`Destination.look` when a path cannot be examined.

    Not raised for a path that is simply not there: that is an answer, not a
    failure. The orchestrator refuses the run, because a backup that cannot
    be read must not be described as if it held nothing.

    Attributes:
        path: The destination path that could not be examined.
        reason: The operating system's reason, as text.
    """

    def __init__(self, path: str, reason: str) -> None:
        """Record the path and the reason, and build the message from them."""
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class Seen:
    """What a destination holds at one path.

    Attributes:
        type: The entry's type tag, as in a snapshot row.
        size: Its size in bytes.
        mtime_ns: Its modification time in nanoseconds since the epoch.
    """

    type: RowType
    size: int
    mtime_ns: int


class Destination(Protocol):
    """A backup destination irsync can examine and rename entries on.

    The local filesystem implements it today (:class:`LocalDestination`). A
    remote destination has no implementation yet, so the orchestrator holds
    ``None`` for one and neither looks nor replays there.
    """

    def look(self, paths: Iterable[str]) -> dict[str, Seen | None]:
        """Report what the destination holds at each path.

        Takes every path in one call so that an implementation working over
        a network needs a single round trip.

        Args:
            paths: POSIX paths relative to the destination root.

        Returns:
            One entry per distinct path: what is there, or None if nothing
            is.

        Raises:
            DestinationUnreadable: If a path cannot be examined for a reason
                other than its absence.
        """
        ...

    def replay(
        self,
        *,
        dir_moves: list[tuple[str, str]],
        file_moves: list[tuple[str, str]],
    ) -> ReplayResult:
        """Rename entries on the destination, as :func:`~irsync.replay.apply_moves` does.

        Args:
            dir_moves: ``(old, new)`` directory pairs.
            file_moves: ``(old, new)`` file pairs.

        Returns:
            Counts of what was moved and skipped.
        """
        ...


def _type_of(mode: int) -> RowType:
    """Classify an ``lstat`` mode as a snapshot row type tag."""
    if stat.S_ISLNK(mode):
        return "l"
    if stat.S_ISDIR(mode):
        return "d"
    if stat.S_ISREG(mode):
        return "f"
    return "o"


class LocalDestination:
    """A destination on a local filesystem, rooted at ``root``."""

    def __init__(self, root: Path) -> None:
        """Remember the destination root; nothing is read until asked."""
        self._root = root

    def look(self, paths: Iterable[str]) -> dict[str, Seen | None]:
        """Report what the destination holds at each path, by ``lstat``.

        A path is absent when nothing is there, when one of its parents is
        missing or is not a directory, or when one of its parents below the
        root is a symlink: a symlinked directory is never followed, the rule
        :func:`~irsync.replay.apply_moves` has, since it can lead out of the
        backup. Each directory on the way is examined once, so a folder the
        destination lacks costs one ``lstat`` however many paths lie below
        it. No path means no filesystem access at all.

        Args:
            paths: POSIX paths relative to the destination root.

        Returns:
            One entry per distinct path: what is there, or None.

        Raises:
            DestinationUnreadable: If an ``lstat`` fails for a reason other
                than the path's absence, such as a permission or I/O error.
        """
        wanted = sorted(set(paths))
        if not wanted:
            return {}
        # Resolved once, so a symlink on the way to the root (a symlinked
        # mount point) is fine while one below it is not followed.
        root = self._root.resolve()
        # Only what the answer needs is kept per path, not the whole
        # os.stat_result: a first --no-exclude run can ask about a million
        # rows under a cache folder.
        examined: dict[str, Seen | None] = {}
        real_dir: dict[str, bool] = {"": True}

        def lstat(rel: str) -> Seen | None:
            if rel not in examined:
                path = root / PurePosixPath(rel)
                try:
                    st = os.lstat(path)
                except (FileNotFoundError, NotADirectoryError):
                    examined[rel] = None
                except OSError as e:
                    raise DestinationUnreadable(str(path), e.strerror or str(e)) from e
                else:
                    examined[rel] = Seen(
                        type=_type_of(st.st_mode),
                        size=st.st_size,
                        mtime_ns=st.st_mtime_ns,
                    )
            return examined[rel]

        def is_real_dir(rel_dir: str) -> bool:
            # Walk up to the nearest directory already judged, then judge
            # each one on the way back down. Iterative, for trees deeper
            # than the recursion limit (10th-F5).
            pending: list[str] = []
            d = rel_dir
            while d not in real_dir:
                pending.append(d)
                d = d.rpartition("/")[0]
            ok = real_dir[d]
            for p in reversed(pending):
                if ok:
                    there = lstat(p)
                    ok = there is not None and there.type == "d"
                real_dir[p] = ok
            return ok

        return {
            rel: lstat(rel) if is_real_dir(rel.rpartition("/")[0]) else None
            for rel in wanted
        }

    def replay(
        self,
        *,
        dir_moves: list[tuple[str, str]],
        file_moves: list[tuple[str, str]],
    ) -> ReplayResult:
        """Rename entries under the root with :func:`~irsync.replay.apply_moves`.

        Args:
            dir_moves: ``(old, new)`` directory pairs.
            file_moves: ``(old, new)`` file pairs.

        Returns:
            Counts of what was moved and skipped.

        Raises:
            CrossDeviceMoveError: If a planned move would cross filesystems
                on the destination; raised before any rename.
        """
        return apply_moves(
            dir_moves=dir_moves, file_moves=file_moves, dest_root=self._root
        )
