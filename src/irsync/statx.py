"""ctypes wrapper around Linux ``statx(2)`` to read inode birth time (btime).

Why this exists
---------------
The 5th-pass NEW-H1 attempt to use ``ctime_ns`` as an inode-reuse tiebreaker
failed because ``rename(2)`` updates ctime — the gate refused legitimate
renames whenever the wall clock had advanced. ``btime`` (a.k.a. crtime) is
the right field: it is set when the inode is allocated and never changes
afterward. A genuine rename preserves it; an inode-reuse event always gets
a fresh value.

``btime`` is exposed by ``statx(2)`` (Linux 4.11+, glibc 2.28+) but is NOT
in Python's stdlib ``os.stat`` result. This module provides a tiny ctypes
wrapper around ``libc.statx`` that returns the inode's birth time as
nanoseconds since the epoch.

Fallback behaviour
------------------
If the platform isn't Linux, libc lacks ``statx``, statx fails, or the
filesystem doesn't report btime (older ext4 without ``-O extent``, FAT,
NFSv3, etc.), :func:`btime_ns` returns ``-1``. Callers must treat ``-1``
as "unknown" and fall back to the size+mtime move gate (the pre-5th-pass
behaviour, which is correct for nanosecond-precision local filesystems).
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

# statx flags
_AT_FDCWD: int = -100
_AT_SYMLINK_NOFOLLOW: int = 0x100

# statx mask bit selecting btime
_STATX_BTIME: int = 0x800


class _StatxTimestamp(ctypes.Structure):
    _fields_ = (
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("__reserved", ctypes.c_int32),
    )


class _Statx(ctypes.Structure):
    # Layout per Linux uapi/linux/stat.h. We only read stx_mask + stx_btime
    # but must declare all fields up to that point so the offsets line up.
    _fields_ = (
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("__spare0", ctypes.c_uint16),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("stx_atime", _StatxTimestamp),
        ("stx_btime", _StatxTimestamp),
        ("stx_ctime", _StatxTimestamp),
        ("stx_mtime", _StatxTimestamp),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("stx_mnt_id", ctypes.c_uint64),
        ("stx_dio_mem_align", ctypes.c_uint32),
        ("stx_dio_offset_align", ctypes.c_uint32),
        ("__spare3", ctypes.c_uint64 * 12),
    )


def _load_statx() -> ctypes._NamedFuncPointer | None:
    """Return ``libc.statx`` if available on this platform, else ``None``."""
    if sys.platform != "linux":
        return None
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        return None
    if not hasattr(libc, "statx"):
        return None
    fn = libc.statx
    fn.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(_Statx),
    )
    fn.restype = ctypes.c_int
    return fn


_STATX = _load_statx()


def is_available() -> bool:
    """Return True iff ``statx`` is callable on this platform."""
    return _STATX is not None


def btime_ns(path: Path | str) -> int:
    """Return inode birth time of ``path`` in nanoseconds since the epoch.

    Returns ``-1`` when btime cannot be determined: statx is unavailable,
    the path is missing, or the filesystem doesn't report btime. Callers
    must treat ``-1`` as "unknown" and fall back to coarser gates.

    Args:
        path: Path to stat. Symlinks are NOT followed (parity with
            :func:`pathlib.Path.lstat`).

    Returns:
        Birth time in nanoseconds since the epoch, or ``-1`` if unknown.
    """
    if _STATX is None:
        return -1
    buf = _Statx()
    rc = _STATX(
        _AT_FDCWD,
        os.fsencode(os.fspath(path)),
        _AT_SYMLINK_NOFOLLOW,
        _STATX_BTIME,
        ctypes.byref(buf),
    )
    if rc != 0:
        return -1
    if not (buf.stx_mask & _STATX_BTIME):
        # Kernel/FS combination doesn't report btime for this file.
        return -1
    return int(buf.stx_btime.tv_sec) * 1_000_000_000 + int(buf.stx_btime.tv_nsec)
