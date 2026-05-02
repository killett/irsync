"""Diff two inode snapshots to recover the move/rename plan that produced the change."""

from __future__ import annotations

import logging
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
                type=r["type"], nlink=r["nlink"], size=r["size"], paths=set()
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
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Compare two inode indices and return ``(dir_moves, file_moves)``.

    Args:
        before: Inode index of the source tree at the time of the previous snapshot.
        after: Inode index of the source tree now.
        skip_hardlinks: If True, ignore inodes with multiple links on either side.
        include_symlinks: If True, include symlinks in the comparison.

    Returns:
        Tuple of ``(dir_moves, file_moves)``, each a list of ``(old_path, new_path)``.
    """
    dir_moves: list[tuple[str, str]] = []
    file_moves: list[tuple[str, str]] = []

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
            if b["type"] == "d":
                dir_moves.append((old_path, new_path))
            else:
                file_moves.append((old_path, new_path))
        else:
            logging.debug(
                "Complex hardlink mapping ignored: %s -> %s",
                sorted(bpaths),
                sorted(apaths),
            )

    dir_moves = sorted({(o, n) for (o, n) in dir_moves if o != n})
    file_moves = sorted({(o, n) for (o, n) in file_moves if o != n})
    return dir_moves, file_moves


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
        i = 0
        while True:
            suffix = f".__mvtmp__{i}" if i else ".__mvtmp__"
            candidate = f"{p}{suffix}"
            if candidate not in old_set and candidate not in move_map.values():
                return candidate
            i += 1

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
    """The full delta between two snapshots: moves, creations, and deletions."""

    dir_moves: list[tuple[str, str]] = field(default_factory=list)
    file_moves: list[tuple[str, str]] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def any_changes(self) -> bool:
        """Return True if any moves, creations, or deletions are present."""
        return bool(self.dir_moves or self.file_moves or self.created or self.deleted)


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

    dir_moves, file_moves = compute_moves(
        before, after, skip_hardlinks=skip_hardlinks, include_symlinks=include_symlinks
    )
    dir_moves = prune_redundant_dir_moves(dir_moves)
    file_moves = _suppress_file_moves_covered_by_dirs(dir_moves, file_moves)

    before_keys = set(before.keys())
    after_keys = set(after.keys())
    deleted_paths = sorted(
        p
        for k, e in before.items()
        if k not in after_keys and e["type"] in ("d", "f")
        for p in e["paths"]
    )
    created_paths = sorted(
        p
        for k, e in after.items()
        if k not in before_keys and e["type"] in ("d", "f")
        for p in e["paths"]
    )
    return Changes(
        dir_moves=dir_moves,
        file_moves=file_moves,
        created=created_paths,
        deleted=deleted_paths,
    )
