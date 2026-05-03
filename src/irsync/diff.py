"""Diff two inode snapshots to recover the move/rename plan that produced the change."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TypedDict

from irsync.snapshot import Row

_PathSubst = Callable[[str], str]


class NodeInfo(TypedDict):
    """Aggregated info for a single inode (which may have multiple paths via hardlinks)."""

    type: str
    nlink: int
    size: int
    mtime_ns: int
    btime_ns: int
    paths: set[str]


def index_by_inode(rows: Iterable[Row]) -> dict[tuple[int, int], NodeInfo]:
    """Group ``rows`` by ``(dev, ino)``, collecting all paths per inode.

    Args:
        rows: Snapshot rows from :func:`irsync.snapshot.snapshot_tree`.

    Returns:
        A dict mapping ``(dev, ino)`` to a :class:`NodeInfo` aggregate.
    """
    idx: dict[tuple[int, int], NodeInfo] = {}
    for r in rows:
        key = (r["dev"], r["ino"])
        if key not in idx:
            idx[key] = NodeInfo(
                type=r["type"],
                nlink=r["nlink"],
                size=r["size"],
                mtime_ns=r.get("mtime_ns", -1),
                btime_ns=r.get("btime_ns", -1),
                paths=set(),
            )
        idx[key]["paths"].add(r["path"])
    return idx


def normalize_dir_pairs(
    dir_moves: list[tuple[str, str]],
) -> list[tuple[PurePosixPath, PurePosixPath]]:
    """Convert ``(old, new)`` string pairs to :class:`PurePosixPath`, sorted deepest-first."""
    pairs = [(PurePosixPath(o), PurePosixPath(n)) for o, n in dir_moves]
    pairs.sort(key=lambda p: len(p[0].parts), reverse=True)
    return pairs


def make_parent_substituter(
    dir_moves: list[tuple[str, str]],
) -> _PathSubst:
    """Build a fast substituter that rewrites a path as if all ``dir_moves`` had been applied.

    Args:
        dir_moves: List of ``(old_dir, new_dir)`` pairs.

    Returns:
        A callable that maps a POSIX-relative path to its rewritten form.
    """
    if not dir_moves:
        return _identity_subst
    pairs = normalize_dir_pairs(dir_moves)

    def subst(p: str) -> str:
        pp = PurePosixPath(p)
        for old_d, new_d in pairs:
            if pp == old_d:
                return new_d.as_posix()
            if old_d in pp.parents:
                rel = pp.relative_to(old_d)
                return (new_d / rel).as_posix()
        return p

    return subst


def _identity_subst(p: str) -> str:
    return p


def _suppress_file_moves_covered_by_dirs(
    dir_moves: list[tuple[str, str]],
    file_moves: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Drop file moves that are already implied by an enclosing directory move."""
    if not dir_moves:
        return file_moves
    subst = make_parent_substituter(dir_moves)
    return [(o, n) for (o, n) in file_moves if subst(o) != n]


def prune_redundant_dir_moves(
    dir_moves: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Keep only top-level dir moves; drop child moves covered by an ancestor move."""
    kept: list[tuple[str, str]] = []
    for o, n in sorted(dir_moves, key=lambda p: len(PurePosixPath(p[0]).parts)):
        if kept:
            subst = make_parent_substituter(kept)
            if subst(o) == n:
                continue
        kept.append((o, n))
    return kept


def compute_moves(
    before: dict[tuple[int, int], NodeInfo],
    after: dict[tuple[int, int], NodeInfo],
    *,
    skip_hardlinks: bool = True,
    include_symlinks: bool = False,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], set[tuple[int, int]]]:
    """Compare two inode indices and return ``(dir_moves, file_moves, consumed_keys)``.

    ``consumed_keys`` is the set of ``(dev, ino)`` pairs that were turned into
    moves; :func:`compute_changes` uses it to figure out which shared inodes
    still need to be reflected as deleted/created/modified in the diff.

    Args:
        before: Inode index of the source tree at the time of the previous snapshot.
        after: Inode index of the source tree now.
        skip_hardlinks: If True, ignore inodes with multiple links on either side.
        include_symlinks: If True, include symlinks in the comparison.

    Returns:
        ``(dir_moves, file_moves, consumed_keys)``.
    """
    dir_moves: list[tuple[str, str]] = []
    file_moves: list[tuple[str, str]] = []
    consumed: set[tuple[int, int]] = set()

    shared = set(before.keys()).intersection(after.keys())
    for key in shared:
        b = before[key]
        a = after[key]

        if b["type"] == "l" and not include_symlinks:
            continue
        if b["type"] == "o":
            continue
        if b["type"] != a["type"]:
            logging.warning(
                "Type change for inode %s: %s -> %s; ignoring",
                key,
                b["type"],
                a["type"],
            )
            continue

        bpaths = b["paths"]
        apaths = a["paths"]

        if bpaths == apaths:
            continue

        if skip_hardlinks and (len(bpaths) != 1 or len(apaths) != 1):
            logging.debug(
                "Skipping inode with multiple links: %s -> %s",
                sorted(bpaths),
                sorted(apaths),
            )
            continue

        if len(bpaths) == 1 and len(apaths) == 1:
            (old_path,) = tuple(bpaths)
            (new_path,) = tuple(apaths)
            # Defend against inode reuse: a kernel that hands out a freed
            # inode number to a brand-new file would otherwise look identical
            # to a rename. The size+mtime_ns gate (H2 from pass 2) catches
            # this on filesystems with nanosecond-precision mtime.
            #
            # The btime_ns tiebreaker (replacing the broken 5th-pass ctime
            # attempt) closes the residual NFS hole: on second-granular FSes
            # it's possible for an unrelated new file to land at the same
            # inode with the same wall-clock-second mtime AND the same size.
            # btime is set when the inode is allocated and never changes
            # afterward, so a real rename preserves it while inode reuse
            # always changes it. We only require btime to match WHEN both
            # snapshots have it (>= 0); when either side reports -1 (statx
            # unavailable, FS without btime support, or legacy snapshot),
            # we fall back to the original size+mtime gate alone.
            #
            # mtime_ns == -1 sentinel (legacy snapshot pre-H2) still forces
            # a conservative refuse-the-move — safe fallback, rsync re-
            # transfers.
            both_have_btime = b["btime_ns"] >= 0 and a["btime_ns"] >= 0
            if (
                b["mtime_ns"] == -1
                or a["mtime_ns"] == -1
                or b["size"] != a["size"]
                or b["mtime_ns"] != a["mtime_ns"]
                or (both_have_btime and b["btime_ns"] != a["btime_ns"])
            ):
                logging.debug(
                    "Inode %s path changed but size/mtime differ "
                    "(possible inode reuse); not treating as a move: %s -> %s",
                    key,
                    old_path,
                    new_path,
                )
                continue
            if b["type"] == "d":
                dir_moves.append((old_path, new_path))
            else:
                file_moves.append((old_path, new_path))
            consumed.add(key)
        else:
            logging.debug(
                "Complex hardlink mapping ignored: %s -> %s",
                sorted(bpaths),
                sorted(apaths),
            )

    dir_moves = sorted({(o, n) for (o, n) in dir_moves if o != n})
    file_moves = sorted({(o, n) for (o, n) in file_moves if o != n})
    return dir_moves, file_moves, consumed


def plan_directory_moves(dir_moves: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Order directory moves so cycles are broken via temp paths.

    Args:
        dir_moves: List of ``(old, new)`` directory pairs.

    Returns:
        Ordered list of ``(src, dst)`` moves (possibly via intermediate temp paths)
        that can be executed sequentially without clobbering.
    """
    if not dir_moves:
        return []

    move_map: dict[str, str] = {o: n for o, n in dir_moves}
    old_set = set(move_map.keys())
    sorted_old_set = sorted(old_set, key=lambda s: (len(PurePosixPath(s).parts), s))
    color: dict[str, int] = {o: 0 for o in old_set}
    temp_moves: list[tuple[str, str, str]] = []

    def temp_name_for(p: str) -> str:
        # Use a random suffix so a real file at the same path on the destination
        # tree (e.g. a backup of in-progress work whose author happened to use
        # this exact suffix) won't collide with a cycle-breaking temp move.
        # 8 hex chars = 32 bits of entropy; collision probability vanishes.
        while True:
            candidate = f"{p}.__mvtmp__{secrets.token_hex(4)}"
            if candidate not in old_set and candidate not in move_map.values():
                return candidate

    def dfs(u: str) -> None:
        color[u] = 1
        v = move_map[u]
        if v in old_set:
            if color.get(v, 0) == 0:
                dfs(v)
            elif color.get(v, 0) == 1:
                tmp = temp_name_for(v)
                final = move_map[v]
                temp_moves.append((v, tmp, final))
                move_map[v] = tmp
        color[u] = 2

    for o in sorted_old_set:
        if color[o] == 0:
            dfs(o)

    ordered: list[tuple[str, str]] = []
    visited: set[str] = set()

    for start in sorted_old_set:
        if start in visited:
            continue
        chain: list[str] = []
        u = start
        while u in move_map and u not in visited:
            chain.append(u)
            visited.add(u)
            v = move_map[u]
            if v in old_set and v not in visited:
                u = v
            else:
                # Either the chain reached a non-old-set tail or a node we've
                # already scheduled. Either way we stop walking.
                break
        for src in reversed(chain):
            ordered.append((src, move_map[src]))

    for _chosen, tmp, final in temp_moves:
        ordered.append((tmp, final))

    seen: set[tuple[str, str]] = set()
    unique_ordered: list[tuple[str, str]] = []
    for m in ordered:
        if m not in seen and m[0] != m[1]:
            seen.add(m)
            unique_ordered.append(m)
    return unique_ordered


@dataclass(frozen=True)
class Changes:
    """The full delta between two snapshots: moves, modifications, creations, deletions."""

    dir_moves: list[tuple[str, str]] = field(default_factory=list)
    file_moves: list[tuple[str, str]] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def any_changes(self) -> bool:
        """Return True if any moves, modifications, creations, or deletions are present."""
        return bool(
            self.dir_moves
            or self.file_moves
            or self.modified
            or self.created
            or self.deleted
        )


def compute_changes(
    before_rows: Iterable[Row],
    after_rows: Iterable[Row],
    *,
    skip_hardlinks: bool = True,
    include_symlinks: bool = False,
) -> Changes:
    """High-level diff: produce moves, prune redundant dir moves, and list created/deleted.

    Args:
        before_rows: Snapshot rows from the previous run.
        after_rows: Snapshot rows from the current run.
        skip_hardlinks: Forwarded to :func:`compute_moves`.
        include_symlinks: Forwarded to :func:`compute_moves`.

    Returns:
        A :class:`Changes` aggregate ready for the orchestrator to act on.
    """
    before = index_by_inode(before_rows)
    after = index_by_inode(after_rows)

    dir_moves, file_moves, consumed_keys = compute_moves(
        before, after, skip_hardlinks=skip_hardlinks, include_symlinks=include_symlinks
    )
    dir_moves = prune_redundant_dir_moves(dir_moves)
    file_moves = _suppress_file_moves_covered_by_dirs(dir_moves, file_moves)

    before_keys = set(before.keys())
    after_keys = set(after.keys())
    deleted_paths = [
        p
        for k, e in before.items()
        if k not in after_keys and e["type"] in ("d", "f")
        for p in e["paths"]
    ]
    created_paths = [
        p
        for k, e in after.items()
        if k not in before_keys and e["type"] in ("d", "f")
        for p in e["paths"]
    ]

    # Inodes that exist in BOTH snapshots but weren't consumed by a move.
    # This block closes the rename-and-edit invisibility (NEW-H3) plus the
    # symlink/hardlink/type-change variants (NEW-H4): when compute_moves
    # declines an inode for any reason, the inode's path-set difference
    # still represents real changes the dest must reflect.
    #   - paths only in before  → add to deleted (rsync removes from dest)
    #   - paths only in after   → add to created (rsync transfers to dest)
    #   - paths in both with size/mtime change → add to modified (rsync
    #     re-transfers content). Skipped for directories, whose mtime ticks
    #     for any child-level change already reported elsewhere.
    modified_paths: list[str] = []
    for key in (before_keys & after_keys) - consumed_keys:
        b = before[key]
        a = after[key]
        only_in_before = b["paths"] - a["paths"]
        only_in_after = a["paths"] - b["paths"]
        shared_paths = b["paths"] & a["paths"]
        deleted_paths.extend(only_in_before)
        created_paths.extend(only_in_after)
        if (
            shared_paths
            and b["type"] != "d"
            and a["type"] != "d"
            and (
                b["size"] != a["size"]
                or b["mtime_ns"] == -1
                or a["mtime_ns"] == -1
                or b["mtime_ns"] != a["mtime_ns"]
            )
        ):
            modified_paths.extend(shared_paths)
    deleted_paths = sorted(set(deleted_paths))
    created_paths = sorted(set(created_paths))
    modified_paths = sorted(set(modified_paths))

    return Changes(
        dir_moves=dir_moves,
        file_moves=file_moves,
        modified=modified_paths,
        created=created_paths,
        deleted=deleted_paths,
    )
