"""Inode snapshots of a directory tree, written as JSONL for diffing across runs."""

from __future__ import annotations

import datetime as dt
import json
import logging
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


def _crosses_boundary(*, entry_dev: int, root_dev: int, xdev: bool) -> bool:
    """Return True if this entry sits on another filesystem and will be skipped.

    Extracted so the boundary decision is directly testable: creating a real
    nested mount requires privileges the test suite does not have.

    Args:
        entry_dev: The entry's ``st_dev``.
        root_dev: The snapshot root's ``st_dev``.
        xdev: If True, filesystem boundaries are enforced.

    Returns:
        True if ``xdev`` is enabled and ``entry_dev`` differs from ``root_dev``.
    """
    return xdev and entry_dev != root_dev


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
    skipped_mounts: list[str] = []

    try:
        st_root = root.lstat()
    except FileNotFoundError as e:
        raise SystemExit(f"Root path does not exist: {root}") from e
    if not stat.S_ISDIR(st_root.st_mode):
        raise SystemExit(f"Root path is not a directory: {root}")

    root_dev = st_root.st_dev

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

    # 10th-F5: explicit-stack DFS instead of recursive walk(). The
    # recursive form added one Python frame per directory level, so a
    # tree deeper than sys.getrecursionlimit() (default ~1000) raised
    # RecursionError mid-walk and left the user with no snapshot.
    stack: list[Path] = [root]
    while stack:
        dir_path = stack.pop()
        try:
            entries = list(dir_path.iterdir())
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue
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
            # principle: only the root entries are reserved). Naturally
            # limited to root since `relative_to(root)` of a subdir entry
            # always contains "/".
            if rel in (SNAPSHOT_FILENAME, LOCKFILE_NAME):
                continue
            if "/" not in rel and rel.startswith(SNAPSHOT_TEMPFILE_PREFIX):
                continue

            is_dir = stat.S_ISDIR(st.st_mode)
            is_symlink = stat.S_ISLNK(st.st_mode)

            ftype = _file_type_char(st.st_mode, is_dir, is_symlink)
            if ftype == "o" and not include_other:
                continue

            crosses = _crosses_boundary(
                entry_dev=int(st.st_dev), root_dev=int(root_dev), xdev=xdev
            )
            if crosses:
                if is_dir and not is_symlink:
                    skipped_mounts.append(rel)
                continue

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
            # Descend into directories without following symlinks.
            if is_dir and not is_symlink:
                stack.append(entry)

    if skipped_mounts:
        shown = ", ".join(sorted(skipped_mounts)[:5])
        more = (
            f" (and {len(skipped_mounts) - 5} more)" if len(skipped_mounts) > 5 else ""
        )
        logging.warning(
            "Skipped %d path(s) on a separate filesystem; they are NOT backed "
            "up (rsync runs with --one-file-system too): %s%s",
            len(skipped_mounts),
            shown,
            more,
        )

    return rows


def write_jsonl(rows: Iterable[Row], out_file: Path) -> None:
    """Write ``rows`` as JSONL (one JSON object per line) to ``out_file``.

    Args:
        rows: Iterable of :class:`Row` records.
        out_file: Destination path; parents must already exist.

    Raises:
        OSError: If the file cannot be written.
    """
    # 10th-F1: errors="surrogateescape" lets paths containing arbitrary
    # non-UTF-8 bytes (legal on Linux, surfaced by os.listdir as surrogate
    # codepoints) round-trip the file boundary instead of raising
    # UnicodeEncodeError mid-snapshot.
    with out_file.open(
        "w", encoding="utf-8", errors="surrogateescape", newline="\n"
    ) as f:
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
    # 10th-F1: see write_jsonl — same surrogateescape rationale.
    with out_file.open(
        "w", encoding="utf-8", errors="surrogateescape", newline="\n"
    ) as f:
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
    # 10th-F4: stream line by line instead of read_text + split("\n"). The
    # old approach materialized the whole file as one str plus a list of
    # line strs (~2× file size in RAM); for a multi-million-row tree that
    # was a hard cap before compute_changes even started building its dict
    # indexes. errors="surrogateescape" mirrors the writer so non-UTF-8
    # bytes in paths round-trip back to the same surrogate codepoints
    # (10th-F1).
    header: Header | None = None
    rows: list[Row] = []
    with file.open("r", encoding="utf-8", errors="surrogateescape") as f:
        first_line = ""
        first_lineno = 0
        for ln, line in enumerate(f, 1):
            if line.strip():
                first_line = line
                first_lineno = ln
                break
        if not first_line:
            return None, []

        try:
            first = json.loads(first_line)
        except json.JSONDecodeError as e:
            raise SystemExit(f"Malformed JSONL at {file}:{first_lineno}: {e}") from e

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
        else:
            # Legacy headerless: the first line is itself a row.
            rows.append(_validate_row(first, file, first_lineno))

        for ln, line in enumerate(f, first_lineno + 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"Malformed JSONL at {file}:{ln}: {e}") from e
            rows.append(_validate_row(obj, file, ln))
    return header, rows


def _validate_row(obj: dict[str, object], file: Path, lineno: int) -> Row:
    """Validate a parsed row dict and apply legacy-snapshot defaults.

    Shared between :func:`read_snapshot` (10th-F4 streaming refactor) and
    :func:`read_jsonl`. Raises :class:`SystemExit` with a precise location
    on missing keys or an invalid type tag (7th-M2).
    """
    for k in ("dev", "ino", "type", "nlink", "size", "path"):
        if k not in obj:
            raise SystemExit(f"Missing key '{k}' in {file}:{lineno}")
    if obj["type"] not in ("f", "d", "l", "o"):
        # 7th-M2: corrupted rows with unknown types were silently kept
        # and later filtered out by compute_changes' type whitelist,
        # hiding inode-level corruption. Refuse the whole snapshot.
        raise SystemExit(
            f"Invalid type {obj['type']!r} in {file}:{lineno} "
            "(expected one of 'f', 'd', 'l', 'o')"
        )
    # mtime_ns and btime_ns were added in later passes; tolerate snapshots
    # written by older versions by defaulting to a sentinel (-1). Missing
    # mtime forces a conservative fallback in compute_moves; missing btime
    # just disables the btime tiebreaker.
    obj.setdefault("mtime_ns", -1)
    obj.setdefault("btime_ns", -1)
    return obj  # type: ignore[return-value]


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
    # 10th-F1: see write_jsonl — surrogateescape on read so non-UTF-8
    # bytes in legacy snapshots also round-trip cleanly.
    with file.open("r", encoding="utf-8", errors="surrogateescape") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"Malformed JSONL at {file}:{ln}: {e}") from e
            out.append(_validate_row(obj, file, ln))
    return out
