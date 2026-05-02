"""Inode snapshots of a directory tree, written as JSONL for diffing across runs."""

from __future__ import annotations

import json
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, TypedDict

SNAPSHOT_FILENAME: str = ".irsync_snapshot.jsonl"


class Row(TypedDict):
    """One JSONL row capturing inode identity for a single tree entry."""

    dev: int
    ino: int
    type: Literal["f", "d", "l", "o"]
    nlink: int
    size: int
    path: str  # POSIX relative path from snapshot root


def _file_type_char(
    mode: int, is_dir: bool, is_symlink: bool
) -> Literal["f", "d", "l", "o"]:
    """Classify a stat ``mode`` into a single-character type tag."""
    if is_dir:
        return "d"
    if is_symlink:
        return "l"
    if (mode & 0o170000) == 0o100000:
        return "f"
    return "o"


def snapshot_tree(
    root: Path,
    *,
    xdev: bool = True,
    include_other: bool = False,
) -> list[Row]:
    """Walk ``root`` without following symlinks and return JSONL-friendly rows.

    The root entry itself is included as ``path == "."``. The snapshot file
    (:data:`SNAPSHOT_FILENAME`) at the root is excluded so the snapshot remains
    self-consistent across runs.

    Args:
        root: Directory to snapshot.
        xdev: If True, do not cross filesystem boundaries.
        include_other: If True, include sockets/devices/pipes as type ``"o"``.

    Returns:
        A list of :class:`Row` records, one per included entry.

    Raises:
        SystemExit: If ``root`` does not exist or is not a directory.
    """
    root = root.resolve()
    rows: list[Row] = []

    try:
        st_root = root.lstat()
    except FileNotFoundError as e:
        raise SystemExit(f"Root path does not exist: {root}") from e
    if not stat.S_ISDIR(st_root.st_mode):
        raise SystemExit(f"Root path is not a directory: {root}")

    root_dev = st_root.st_dev

    def walk(dir_path: Path) -> None:
        try:
            entries = list(dir_path.iterdir())
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            return
        for entry in entries:
            try:
                st = entry.lstat()
            except (FileNotFoundError, PermissionError):
                continue

            rel = entry.relative_to(root).as_posix()

            # Skip the snapshot file at the source root.
            if rel == SNAPSHOT_FILENAME:
                continue

            is_dir = stat.S_ISDIR(st.st_mode)
            is_symlink = stat.S_ISLNK(st.st_mode)

            ftype = _file_type_char(st.st_mode, is_dir, is_symlink)
            if ftype == "o" and not include_other:
                continue
            if not xdev or st.st_dev == root_dev:
                rows.append(
                    Row(
                        dev=int(st.st_dev),
                        ino=int(st.st_ino),
                        type=ftype,
                        nlink=int(st.st_nlink),
                        size=int(st.st_size),
                        path=rel,
                    )
                )
            # Recurse into directories without following symlinks.
            if is_dir and not is_symlink and (not xdev or st.st_dev == root_dev):
                walk(entry)

    rows.append(
        Row(
            dev=int(st_root.st_dev),
            ino=int(st_root.st_ino),
            type="d",
            nlink=int(st_root.st_nlink),
            size=int(st_root.st_size),
            path=".",
        )
    )

    walk(root)
    return rows


def write_jsonl(rows: Iterable[Row], out_file: Path) -> None:
    """Write ``rows`` as JSONL (one JSON object per line) to ``out_file``.

    Args:
        rows: Iterable of :class:`Row` records.
        out_file: Destination path; parents must already exist.

    Raises:
        OSError: If the file cannot be written.
    """
    with out_file.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False))
            f.write("\n")


def read_jsonl(file: Path) -> list[Row]:
    """Read a JSONL file produced by :func:`write_jsonl` back into rows.

    Args:
        file: JSONL file to read.

    Returns:
        A list of :class:`Row` records.

    Raises:
        SystemExit: If a row is malformed or missing required keys.
        FileNotFoundError: If ``file`` does not exist.
    """
    out: list[Row] = []
    with file.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"Malformed JSONL at {file}:{ln}: {e}") from e
            for k in ("dev", "ino", "type", "nlink", "size", "path"):
                if k not in obj:
                    raise SystemExit(f"Missing key '{k}' in {file}:{ln}")
            out.append(obj)
    return out
