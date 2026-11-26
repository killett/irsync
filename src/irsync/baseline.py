"""Decide what a baseline may say about the backup under the default-excluded names.

A snapshot records every row of the source, but a run with the default
excludes never copies the excluded names, so a baseline row under one of
them says what the source had, not what the backup holds. Such a row is a
hint. The functions here pick the hints worth asking the destination about
and turn its answers into rows the diff can trust. They do no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from irsync.destination import Seen
from irsync.diff import index_by_inode, same_file
from irsync.snapshot import Row

_NS_PER_SECOND = 1_000_000_000


def split_hints(
    rows: Iterable[Row], unvouched: Callable[[str], bool]
) -> tuple[list[Row], list[Row]]:
    """Split baseline rows into those the backup is known to hold and the hints.

    Args:
        rows: The baseline's rows.
        unvouched: A predicate true for a path under a default-excluded
            name (:func:`~irsync.rsync_runner.exclude_matcher` on the default
            exclude list, whatever ``--no-exclude`` says).

    Returns:
        ``(trusted, hints)``, each in the baseline's order.
    """
    trusted: list[Row] = []
    hints: list[Row] = []
    for row in rows:
        (hints if unvouched(row["path"]) else trusted).append(row)
    return trusted, hints


def holds(row: Row, there: Seen | None) -> bool:
    """Return True if the destination holds what a baseline row describes.

    For a regular file this is rsync's own quick check: the same size, and
    the same mtime to the whole second. irsync then never lists a move that
    rsync would follow with a transfer, and never refuses one rsync would
    have left alone. Any other entry only has to be of the row's type.

    Args:
        row: A baseline row.
        there: What the destination holds at the row's path, or None.

    Returns:
        True if the destination's entry matches the row.
    """
    if there is None or there.type != row["type"]:
        return False
    if row["type"] != "f":
        return True
    return (
        row["mtime_ns"] != -1
        and there.size == row["size"]
        and there.mtime_ns // _NS_PER_SECOND == row["mtime_ns"] // _NS_PER_SECOND
    )


def checked_rows(hints: Iterable[Row], seen: Mapping[str, Seen | None]) -> list[Row]:
    """Replace each hint by what the destination holds at its path.

    Used when rsync syncs the excluded names (``--no-exclude``), so that the
    diff's "before" side describes the backup:

    - nothing there: the hint is dropped, and the diff reports the path as
      created if the source still has it;
    - an entry matching the hint (:func:`holds`): the hint is kept as is,
      and can become a move;
    - anything else: the hint is kept with the destination's type, size and
      mtime, so the diff reports the path as modified, or as deleted and
      created.

    Args:
        hints: Baseline rows under a default-excluded name.
        seen: The destination's answer for each hint's path.

    Returns:
        The rows to put on the diff's "before" side, in the hints' order.
    """
    out: list[Row] = []
    for row in hints:
        there = seen.get(row["path"])
        if there is None:
            continue
        if holds(row, there):
            out.append(row)
            continue
        out.append(
            Row(
                dev=row["dev"],
                ino=row["ino"],
                type=there.type,
                nlink=row["nlink"],
                size=there.size,
                mtime_ns=there.mtime_ns,
                btime_ns=row["btime_ns"],
                path=row["path"],
            )
        )
    return out


def moved_out_candidates(
    hints: Iterable[Row],
    trusted: Iterable[Row],
    after: Iterable[Row],
    *,
    source: Iterable[Row],
) -> list[Row]:
    """Pick the hints that can become a file move in a run with the excludes.

    Such a run leaves the excluded names on the destination alone, so the
    only thing irsync can do there is take a file out. A hint qualifies when
    it is certain to become a move if the destination holds it: a regular
    file whose inode has exactly one path in the baseline (the hint's) and
    exactly one path on the source, that one in ``after``, where it is still
    a regular file and the same file (:func:`~irsync.diff.same_file`). A
    hard link left under the excluded name disqualifies it, since renaming
    the backup's copy would take it from a path the source still has.
    Anything else under an excluded name stays out of the diff, so no path
    there can be listed as modified, created, or deleted.

    Args:
        hints: Baseline rows under a default-excluded name.
        trusted: The other baseline rows.
        after: The fresh rows rsync will sync; none is under an excluded
            name.
        source: Every fresh row, excluded names included. An inode with
            more than one path here is hard-linked on the source and never
            a candidate. Paths are counted, not ``nlink``: a second link
            outside the source tree does not stop a move.

    Returns:
        The qualifying hints, in the hints' order.
    """
    hints = list(hints)
    hint_keys = {(h["dev"], h["ino"]) for h in hints}
    now = index_by_inode(r for r in after if (r["dev"], r["ino"]) in hint_keys)
    if not now:
        return []
    before = index_by_inode(
        r for r in (*trusted, *hints) if (r["dev"], r["ino"]) in now
    )
    on_source = index_by_inode(r for r in source if (r["dev"], r["ino"]) in now)
    out: list[Row] = []
    for h in hints:
        key = (h["dev"], h["ino"])
        a = now.get(key)
        if a is None or h["type"] != "f" or a["type"] != "f":
            continue
        b = before[key]
        if (
            b["paths"] == {h["path"]}
            and len(a["paths"]) == 1
            and len(on_source[key]["paths"]) == 1
            and same_file(b, a)
        ):
            out.append(h)
    return out
