"""Inode snapshots of a directory tree, written as JSONL for diffing across runs."""

from __future__ import annotations

import datetime as dt
import json
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, TypedDict

from irsync import __version__
from irsync.statx import btime_ns as _btime_ns

SNAPSHOT_FILENAME: str = ".irsync_snapshot.jsonl"
LOCKFILE_NAME: str = ".irsync.lock"
SNAPSHOT_TEMPFILE_PREFIX: str = ".irsync-snap-"


class SnapshotMismatch(Exception):
    """Raised when a snapshot's recorded source root doesn't match the caller's."""


class Header(TypedDict):
    """Provenance metadata stored as the first line of a snapshot file."""

    source_root: str
    irsync_version: str
    created_at_utc: str


class Row(TypedDict):
    """One JSONL row capturing inode identity for a single tree entry."""

    dev: int
    ino: int
    type: Literal["f", "d", "l", "o"]
    nlink: int
    size: int
    mtime_ns: int  # st_mtime_ns; combined with size, defends against inode reuse
    btime_ns: (
        int  # statx birth time; tiebreaker that survives rename, -1 if unsupported
    )
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

            # Skip irsync's reserved files at the source root: the snapshot
            # itself, the lockfile, and any orphan tempfiles from a killed
            # _atomic_write_snapshot. Subdirectory files with the same names
            # are still included (this is the H1/NEW-H2 anchored-exclude
            # principle: only the root entries are reserved).
            if rel in (SNAPSHOT_FILENAME, LOCKFILE_NAME):
                continue
            if "/" not in rel and rel.startswith(SNAPSHOT_TEMPFILE_PREFIX):
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
                        mtime_ns=int(st.st_mtime_ns),
                        btime_ns=_btime_ns(entry),
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
            mtime_ns=int(st_root.st_mtime_ns),
            btime_ns=_btime_ns(root),
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


def write_snapshot(
    rows: Iterable[Row],
    *,
    source_root: Path,
    out_file: Path,
) -> None:
    """Write a snapshot with a provenance header line followed by row lines.

    The header records the resolved source root, irsync version, and a UTC
    timestamp so a stale snapshot from a different tree can be rejected
    before being used as a diff baseline.

    Args:
        rows: Snapshot rows.
        source_root: The directory the snapshot covers; resolved to absolute.
        out_file: Destination path; parent must exist.
    """
    header: dict[str, Header] = {
        "_meta": Header(
            source_root=str(source_root.resolve()),
            irsync_version=__version__,
            created_at_utc=dt.datetime.now(dt.UTC).isoformat(),
        )
    }
    with out_file.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(header, separators=(",", ":"), ensure_ascii=False))
        f.write("\n")
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False))
            f.write("\n")


def read_snapshot(
    file: Path,
    *,
    expected_source_root: Path,
) -> tuple[Header | None, list[Row]]:
    """Read a snapshot file written by :func:`write_snapshot`.

    If the first line is a header object (``{"_meta": ...}``), it is parsed
    and validated against ``expected_source_root``. A header from a
    different source tree raises :class:`SnapshotMismatch` so the caller
    can refuse to use it as a diff baseline. Files without a header (legacy
    format) return ``None`` for the header and are otherwise read as rows.

    Args:
        file: Snapshot file path.
        expected_source_root: The current source root; the header's
            ``source_root`` must match this when present.

    Returns:
        ``(header_or_none, rows)``.

    Raises:
        SnapshotMismatch: If the header's source root doesn't match.
        SystemExit: If a row is malformed.
    """
    text = file.read_text(encoding="utf-8")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return None, []

    header: Header | None = None
    first = json.loads(lines[0])
    if isinstance(first, dict) and "_meta" in first:
        meta = first["_meta"]
        header = Header(
            source_root=meta.get("source_root", ""),
            irsync_version=meta.get("irsync_version", ""),
            created_at_utc=meta.get("created_at_utc", ""),
        )
        expected = str(expected_source_root.resolve())
        if header["source_root"] != expected:
            raise SnapshotMismatch(
                f"Snapshot at {file} was taken of {header['source_root']!r}, "
                f"but the current source root is {expected!r}. Refusing to use it "
                "as a diff baseline."
            )
        data_lines = lines[1:]
    else:
        data_lines = lines

    rows: list[Row] = []
    for ln, line in enumerate(data_lines, 1):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit(f"Malformed JSONL at {file}:{ln}: {e}") from e
        for k in ("dev", "ino", "type", "nlink", "size", "path"):
            if k not in obj:
                raise SystemExit(f"Missing key '{k}' in {file}:{ln}")
        if obj["type"] not in ("f", "d", "l", "o"):
            # 7th-M2: corrupted rows with unknown types were silently kept
            # and later filtered out by compute_changes' type whitelist,
            # hiding inode-level corruption. Refuse the whole snapshot.
            raise SystemExit(
                f"Invalid type {obj['type']!r} in {file}:{ln} "
                "(expected one of 'f', 'd', 'l', 'o')"
            )
        obj.setdefault("mtime_ns", -1)
        obj.setdefault("btime_ns", -1)
        rows.append(obj)
    return header, rows


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
            if obj["type"] not in ("f", "d", "l", "o"):
                raise SystemExit(
                    f"Invalid type {obj['type']!r} in {file}:{ln} "
                    "(expected one of 'f', 'd', 'l', 'o')"
                )
            # mtime_ns and btime_ns were added in later passes; tolerate
            # snapshots written by older versions by defaulting to a sentinel
            # (-1). Missing mtime forces a conservative fallback in
            # compute_moves; missing btime just disables the btime tiebreaker.
            obj.setdefault("mtime_ns", -1)
            obj.setdefault("btime_ns", -1)
            out.append(obj)
    return out
