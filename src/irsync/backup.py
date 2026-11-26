"""End-to-end orchestrator: snapshot, diff, optionally replay-and-rsync, persist."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import logging
import os
import shutil
import subprocess  # noqa: S404 — needed for paging via less
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path

from irsync.baseline import checked_rows, holds, moved_out_candidates, split_hints
from irsync.destination import Destination, DestinationUnreadable, LocalDestination
from irsync.diff import Changes, compute_changes
from irsync.display import display_name
from irsync.options import Endpoints, Options, resolve_endpoints, resolve_source
from irsync.paths import with_trailing_slash
from irsync.preflight import (
    DestinationNotMounted,
    EndpointNotMounted,
    RsyncUnavailable,
    TopLevelSplit,
    UnsafeDestination,
    check_mounted,
    check_rsync_available,
    foreign_dest_entries,
    fresh_snapshot_is_empty,
    source_root_is_empty,
    split_top_level,
    top_level_names,
)
from irsync.replay import CrossDeviceMoveError
from irsync.rsync_runner import (
    build_rsync_command,
    exclude_matcher,
    run_dry_run,
    run_real_sync,
)
from irsync.snapshot import (
    LOCKFILE_NAME,
    SNAPSHOT_FILENAME,
    SNAPSHOT_TEMPFILE_PREFIX,
    Row,
    SnapshotCorrupt,
    SnapshotMismatch,
    read_snapshot,
    snapshot_tree,
    write_snapshot,
)

# Refuse to proceed when the diff says this fraction of the previously-recorded
# tree is gone — almost always a swapped-args / wrong-snapshot accident.
CATASTROPHIC_DELETE_RATIO: float = 0.5

# Process exit codes. Named so the non-obvious values aren't bare literals
# scattered across the orchestrator's early returns.
EXIT_REFUSED: int = 2  # bad invocation / refused for safety; nothing was written
EXIT_LOCK_HELD: int = 75  # EX_TEMPFAIL — another run holds the lock, try again later
EXIT_ABORTED: int = 130  # 128 + SIGINT, the shell convention for "user aborted"


@contextlib.contextmanager
def _source_lock(src_root: Path) -> Iterator[None]:
    """Hold an exclusive flock on ``<src_root>/.irsync.lock`` for the duration.

    Two concurrent irsync runs against the same source would otherwise race
    on snapshot read/write and apply_moves. This is non-blocking: if the lock
    is held, raise BlockingIOError immediately so the caller can exit cleanly
    rather than wait indefinitely.
    """
    src_root.mkdir(parents=True, exist_ok=True)
    lock_path = src_root / LOCKFILE_NAME
    f = lock_path.open("w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise
    try:
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def _log_lock_conflict(root: Path) -> int:
    """Log the "lock already held" refusal for ``root`` and return the exit code."""
    logging.error(
        "Another irsync run is already in progress against %s "
        "(lock %s is held). Refusing to race.",
        root,
        root / LOCKFILE_NAME,
    )
    return EXIT_LOCK_HELD


def _log_source_permission_error(root: Path, e: PermissionError) -> int:
    """Log the "can't write to the source root" refusal and return the exit code.

    Scoped to callers that know the failure genuinely happened while writing
    to ``root`` itself (lock acquisition, or a snapshot write with no
    destination in play) — never wrap a broader operation in
    ``except PermissionError`` and blame the source root for whatever failed
    inside it. A blanket handler around the orchestrator would misattribute
    downstream (e.g. destination) permission errors to the wrong disk.
    """
    logging.error(
        "Cannot write to the source root %s (%s). irsync needs to create "
        "its lockfile and snapshot there.",
        root,
        e,
    )
    return EXIT_REFUSED


def _atomic_write_snapshot(rows: list[Row], source_root: Path, target: Path) -> None:
    """Write rows + provenance header to ``target`` atomically (tempfile + replace).

    The provenance header records ``source_root`` so a later run that finds a
    snapshot from a different tree at the source root can refuse to use it.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(prefix=SNAPSHOT_TEMPFILE_PREFIX, dir=target.parent)
    os.close(fd)
    tmp_path = Path(tmpname)
    try:
        write_snapshot(rows, source_root=source_root, out_file=tmp_path)
        # 10th-F3: fsync the tempfile before replacing the live snapshot.
        # os.replace is atomic at the directory-entry level, but the
        # tempfile's data blocks may still be sitting in the kernel's
        # page cache. A power loss between rename and flush leaves the
        # new dirent pointing at empty/partial content; the next run's
        # read_snapshot then chokes on malformed JSONL with no recovery.
        with open(tmp_path, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _persist_snapshots(rows: list[Row], src_root: Path, dest_root: Path | None) -> None:
    """Write the fresh snapshot to (if local) the dest root, then the source root.

    Order matters (7th-M1): a failure / SIGKILL between the two writes used
    to leave src updated and dest stale. On a DR restore (source disk dies,
    user copies dest content + dest snapshot back), that asymmetry produced
    a baseline that didn't match the restored content. By writing dest first,
    a dest failure aborts the function before src is touched, leaving the
    src baseline at its OLD value so the next run still reconciles cleanly.
    """
    if dest_root is not None:
        _atomic_write_snapshot(rows, src_root, dest_root / SNAPSHOT_FILENAME)
    _atomic_write_snapshot(rows, src_root, src_root / SNAPSHOT_FILENAME)


def _format_preview(
    changes: Changes, *, unexcluded: Sequence[str] = (), checked: bool = False
) -> str:
    """Build the full pre-confirmation preview text — every move, deletion, creation.

    The deletion list is the safety-critical part: rsync's ``--delete-before`` will
    remove these from the backup tree, so the user must see the actual paths (not
    just a count) before saying yes. Every path goes through
    :func:`~irsync.display.display_name`, so an undecodable byte cannot crash the
    print or the pager and a newline in a name cannot fake a line.

    Args:
        changes: The snapshot diff to list.
        unexcluded: The default exclude patterns this run does not apply
            (``--no-exclude``). When non-empty, a note opens the preview
            saying that rsync also syncs those names on the backup.
        checked: Whether the lists cover those names, checked against the
            backup (a local destination). The note then names the one thing
            the lists cannot show; otherwise it says they show only what
            changed on the source.

    Returns:
        The preview text, ending in a newline.
    """
    lines: list[str] = []
    if unexcluded:
        lines.append(f"=== --no-exclude: rsync also syncs {', '.join(unexcluded)} ===")
        if checked:
            lines.append(
                "The lists below cover these names too, checked against the backup. "
                "They"
            )
            lines.append(
                "cannot show one thing: a file there that only the backup has and no "
                "earlier"
            )
            lines.append(
                "run recorded, which rsync DELETES. --dry-run shows every file."
            )
        else:
            lines.append(
                "On the backup, rsync makes these match the source, copying and "
                "DELETING"
            )
            lines.append(
                "files there. The lists below show only what changed on the source "
                "since"
            )
            lines.append(
                "the last backup; --dry-run shows every file rsync will copy or delete."
            )
    lines.append(f"=== Directory moves ({len(changes.dir_moves)}) ===")
    for o, n in changes.dir_moves:
        lines.append(f"  {display_name(o)}  ->  {display_name(n)}")
    lines.append(f"=== File moves ({len(changes.file_moves)}) ===")
    for o, n in changes.file_moves:
        lines.append(f"  {display_name(o)}  ->  {display_name(n)}")
    lines.append(
        f"=== Modified in place, will be re-transferred ({len(changes.modified)}) ==="
    )
    for p in changes.modified:
        lines.append(f"  ~ {display_name(p)}")
    lines.append(
        f"=== Created on source, will be transferred ({len(changes.created)}) ==="
    )
    for p in changes.created:
        lines.append(f"  + {display_name(p)}")
    lines.append(
        f"=== Deleted on source, will be REMOVED FROM BACKUP ({len(changes.deleted)}) ==="
    )
    for p in changes.deleted:
        lines.append(f"  - {display_name(p)}")
    return "\n".join(lines) + "\n"


def _format_size(total_bytes: int) -> str:
    """Render a byte count as a human-scaled ``value unit`` string.

    A fixed GiB unit reads as "0.0 GiB" for any source under half a
    gibibyte, which is accurate but useless for judging scale on a small
    tree. Scaling to the largest unit under which the value is at least 1
    (falling back to bytes) keeps the summary meaningful across both a
    handful of config files and a multi-terabyte archive, with no added
    dependency.

    The threshold check compares ``round(value, 1)`` rather than the raw
    value: a raw value of e.g. 1023.999... KiB is < 1024 and would render
    as "1024.0 KiB" once formatted to one decimal place, which reads like
    it should have rolled over to the next unit. Rounding before comparing
    makes the displayed number and the unit choice agree at every boundary.
    """
    value = float(total_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if round(value, 1) < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"  # unreachable, satisfies mypy's no-implicit-return


_SUMMARY_INDENT = " " * len("Destination: ")
"""Lines up a summary's detail lines under its ``Source:``/``Destination:`` values."""

_NAMES_SHOWN = 10
"""How many names a summary group lists before ``(and N more)``."""


def _count(n: int, singular: str, plural: str) -> str:
    """Return ``n`` with thousands separators and the noun that agrees with it."""
    return f"{n:,} {singular if n == 1 else plural}"


def _name_list(names: Sequence[str]) -> str:
    """Return the first :data:`_NAMES_SHOWN` names, escaped, and how many more."""
    shown = ", ".join(display_name(name) for name in names[:_NAMES_SHOWN])
    rest = len(names) - _NAMES_SHOWN
    return f"{shown} (and {rest:,} more)" if rest > 0 else shown


def _split_counts(split: TopLevelSplit) -> str:
    """Return a split's three counts as one clause, for a one-line message."""
    return (
        f"{len(split.on_source):,} also on the source, "
        f"{len(split.kept):,} skipped by rsync so kept, "
        f"{len(split.not_on_source):,} not on the source"
    )


def _format_split(split: TopLevelSplit) -> list[str]:
    """Render a destination's top-level split as three right-aligned count lines."""
    groups: list[tuple[str, Sequence[str], bool]] = [
        ("also on the source", split.on_source, False),
        ("skipped by rsync, so kept", split.kept, True),
        ("not on the source (a real run DELETES these)", split.not_on_source, True),
    ]
    width = max(len(f"{len(names):,}") for _, names, _ in groups)
    lines: list[str] = []
    for label, names, list_names in groups:
        line = f"{_SUMMARY_INDENT}  {len(names):>{width},} {label}"
        if list_names and names:
            line += f": {_name_list(names)}"
        lines.append(line)
    return lines


def _format_first_run_preview(
    rows: list[Row],
    src_root: Path,
    source_top_level: int,
    dest_root: Path | None,
    dest_split: TopLevelSplit | None,
    *,
    show_hint: bool,
) -> str:
    """Summarize a backup that has no baseline to diff against.

    With no usable snapshot there is no move/delete plan to show, but the run
    can delete anything at the destination that is not on the source. Both
    sides are counted at their top level, the same way, so an up-to-date
    backup does not look nearly empty beside the source's full entry count,
    and the destination's top-level names are split by what a real run does
    to them. Neither tree is scanned for this beyond the listings the run
    already makes.

    Args:
        rows: The source's fresh snapshot rows, root row included.
        src_root: The source root.
        source_top_level: How many names the source root holds, irsync's own
            files left out.
        dest_root: The local destination root, or None for a remote one,
            which is never listed (AD-25).
        dest_split: The destination's top level split by
            :func:`~irsync.preflight.split_top_level`, or None when the local
            destination does not exist yet.
        show_hint: Whether to point at ``--dry-run``; only useful when a
            prompt follows and the run is not already a dry run.

    Returns:
        The summary text, ASCII apart from the escaped paths and names in it.
    """
    total_bytes = sum(r["size"] for r in rows if r["type"] == "f")
    in_all = sum(1 for r in rows if r["path"] != ".")
    lines = [
        "=== FIRST BACKUP (no usable snapshot) ===",
        f"Source:      {display_name(str(src_root))}",
        _SUMMARY_INDENT + _count(source_top_level, "top-level item", "top-level items"),
        _SUMMARY_INDENT
        + _count(in_all, "file or folder", "files and folders")
        + f" in all, {_format_size(total_bytes)}",
    ]
    if dest_root is None:
        lines.append("Destination: not checked (remote)")
    else:
        lines.append(f"Destination: {display_name(str(dest_root))}")
        if dest_split is None:
            lines.append(f"{_SUMMARY_INDENT}does not exist yet (rsync will create it)")
        else:
            total = (
                len(dest_split.kept)
                + len(dest_split.on_source)
                + len(dest_split.not_on_source)
            )
            if total == 0:
                lines.append(f"{_SUMMARY_INDENT}empty")
            else:
                lines.append(
                    _SUMMARY_INDENT
                    + _count(total, "top-level item", "top-level items")
                    + ":"
                )
                lines.extend(_format_split(dest_split))
    lines += [
        "A real run makes the destination match the source: it copies",
        "whatever differs and DELETES anything there that is not on the source.",
    ]
    if show_hint:
        lines.append("For the exact list of files, answer N and rerun with --dry-run.")
    return "\n".join(lines) + "\n"


def _dry_run_preview(cmd: list[str]) -> tuple[str | None, int]:
    """Run the preview dry-run, converting an rsync failure into an exit code.

    ``run_dry_run`` uses ``check=True``, so a non-zero rsync exit raises
    ``CalledProcessError``. Both preview call sites used to let that escape
    all the way out of :func:`irsync.cli.main`, so a routine rsync failure
    (23 partial transfer, 24 vanished source files, 255 ssh error) surfaced
    as a Python traceback with the exit code flattened to 1.

    Returns:
        ``(output, 0)`` when rsync succeeded, or ``(None, exit_code)`` when it
        failed. A process killed by a signal reports a negative returncode,
        which is not a usable exit status, so those map to
        :data:`EXIT_REFUSED`.
    """
    try:
        return run_dry_run(cmd), 0
    except subprocess.CalledProcessError as e:
        logging.error(
            "rsync dry-run failed (exit %s); refusing to continue. Command: %r",
            e.returncode,
            cmd,
        )
        rc = e.returncode if e.returncode and e.returncode > 0 else EXIT_REFUSED
        return None, rc


def _emit_preview(text: str, *, interactive: bool) -> None:
    """Show pre-confirmation text: paged when interactive, printed under ``--yes``.

    AD-7: a ``--yes`` (cron) run prints the preview to stdout so the run is
    auditable in its log, and never pages. Every preview goes through here,
    so the change preview, the first-run summary, and the ``--no-snapshot``
    dry-run output cannot drift apart on that rule.
    """
    if interactive:
        _page_output(text)
    else:
        print(text)


def _show_preview(
    changes: Changes,
    *,
    interactive: bool,
    unexcluded: Sequence[str] = (),
    checked: bool = False,
) -> None:
    """Render the change preview, paging through ``less`` if interactive."""
    _emit_preview(
        _format_preview(changes, unexcluded=unexcluded, checked=checked),
        interactive=interactive,
    )


def _plan_changes(
    before_rows: list[Row],
    fresh_rows: list[Row],
    *,
    exclude_dirs: Sequence[str],
    no_exclude: bool,
    destination: Destination | None,
) -> tuple[Changes, list[Row]]:
    """Diff the baseline against the fresh snapshot, asking the backup where needed.

    A baseline row under a default-excluded name says what the source had,
    not what the backup holds, since a run with the excludes never copies
    those names. For a local destination such a row enters the diff only as
    far as the destination confirms it (AD-36):

    - a run with the excludes changes nothing under those names, so it asks
      only about a file that has moved out of one, and keeps the row when
      the backup holds that file, which makes it a file move;
    - a ``--no-exclude`` run syncs those names, so it asks about every such
      row and diffs against what the backup holds.

    A remote destination cannot be examined: its runs diff only what rsync
    transfers, as before (AD-32).

    Args:
        before_rows: The baseline's rows.
        fresh_rows: The source's fresh snapshot rows.
        exclude_dirs: The default exclude patterns.
        no_exclude: Whether rsync syncs the excluded names in this run.
        destination: Where to look, or None for a remote destination.

    Returns:
        ``(changes, before)``: the diff, and the baseline rows it was
        computed against, which the >50% guard counts deletions against.

    Raises:
        DestinationUnreadable: If the destination cannot be examined.
    """
    skipped = exclude_matcher([] if no_exclude else exclude_dirs)
    after = [r for r in fresh_rows if not skipped(r["path"])]
    if destination is None:
        before = [r for r in before_rows if not skipped(r["path"])]
        return compute_changes(before, after), before
    trusted, hints = split_hints(before_rows, exclude_matcher(exclude_dirs))
    if no_exclude:
        # No hints, no lookup: with an empty exclude list a run that has
        # nothing to do must still leave the backup drive alone (AD-2).
        seen = destination.look([r["path"] for r in hints]) if hints else {}
        before = trusted + checked_rows(hints, seen)
    else:
        candidates = moved_out_candidates(hints, trusted, after)
        seen = destination.look([r["path"] for r in candidates]) if candidates else {}
        before = trusted + [r for r in candidates if holds(r, seen[r["path"]])]
    return compute_changes(before, after), before


def _confirm_or_abort(args: argparse.Namespace) -> bool:
    """Return True if the user confirmed (or --yes was passed)."""
    if args.yes:
        return True
    answer = input("Proceed with replay + rsync? [yes/N]: ")
    return answer.casefold() == "yes"


def run_backup(
    *,
    source_arg: str,
    destination_arg: str | None,
    options: Options,
    args: argparse.Namespace,
) -> int:
    """Run a single backup and return a process-style exit code.

    Args:
        source_arg: Raw source argument from the CLI.
        destination_arg: Raw destination argument, or None.
        options: Path defaults.
        args: Parsed argparse namespace from :func:`irsync.cli.main`.

    Returns:
        0 on success (or when there was no work to do), non-zero on error.

    Raises:
        SourceNotMounted: If the source's drive is not mounted and
            ``args.allow_unmounted`` is not set. Callers that iterate
            several drives (:func:`run_all_backups`) catch this and treat
            it as a skip.
        DestinationNotMounted: If the destination's drive is not mounted
            and ``args.allow_unmounted`` is not set. Unlike a missing
            source drive, :func:`run_all_backups` counts this as a real
            error, not a skip: the source drive being mounted while its
            backup drive is not means the run would back up nothing while
            looking like success.
        UnsafeDestination: If there is no usable snapshot baseline and the
            destination cannot be read to check whether it is empty.
            Callers that iterate several drives (:func:`run_all_backups`)
            catch this and count it as a real error, not a skip.
        RsyncUnavailable: If the rsync binary is not on PATH and this run
            will actually invoke rsync (``--snapshot-only`` never does, so
            it is exempt). Checked up front so a missing rsync install
            surfaces as a clean refusal rather than a ``FileNotFoundError``
            traceback from deep inside ``run_dry_run``/``run_real_sync``.
        ValueError: If the arguments cannot be resolved — an empty source,
            an unconfigured drive id or endpoint, a shorthand with no config
            loaded, remote-to-remote, or identical or nested paths (from
            :func:`~irsync.options.resolve_endpoints` /
            :func:`~irsync.options.resolve_source`).
        FileNotFoundError: If a local source, or a destination's parent,
            does not exist. :func:`run_all_backups` treats this as a skip.
        NotADirectoryError: If a local endpoint is not a directory. Also a
            skip in :func:`run_all_backups`.
    """
    # --snapshot-only doesn't need a destination at all — it just records the
    # current state of the source for use as a future baseline. Route around
    # resolve_endpoints so the user can snapshot any local directory without
    # having to invent a dest argument.
    if args.snapshot_only and not destination_arg:
        return _run_snapshot_only(source_arg=source_arg, options=options, args=args)

    # --snapshot-only never runs rsync, whether or not a destination was also
    # given (see the early return above and the args.snapshot_only branch in
    # _run_backup_for_endpoints), so it must not be gated on rsync's
    # presence. Every other path below eventually calls run_dry_run or
    # run_real_sync.
    if not args.snapshot_only:
        check_rsync_available()

    endpoints = resolve_endpoints(source_arg, destination_arg, options)

    # The gate runs BEFORE _source_lock: that function mkdirs the source root
    # and opens .irsync.lock for writing, so locking an unmounted source
    # creates files on the disk the gate exists to protect.
    try:
        check_mounted(
            endpoints.source,
            options.base_dir,
            gate_outside_base=args.require_mount,
            role="source",
        )
        check_mounted(
            endpoints.dest,
            options.base_dir,
            gate_outside_base=args.require_mount,
            role="destination",
        )
    except EndpointNotMounted:
        if not args.allow_unmounted:
            raise
        logging.warning("Proceeding past the mount check (--allow-unmounted).")

    src_root = endpoints.source if isinstance(endpoints.source, Path) else None
    if src_root is None:
        # Remote source: nowhere to put the lock. Skip the lock and let
        # rsync's own protocol coordinate with the remote.
        return _run_backup_for_endpoints(
            endpoints=endpoints, options=options, args=args
        )

    # The lock's __enter__/__exit__ are called explicitly (rather than via
    # `with`) so lock ACQUISITION has its own try/except, scoped only to
    # `_source_lock`'s mkdir + lockfile open. Everything downstream
    # (_persist_snapshots writes to dest before src; apply_moves renames
    # inside the dest tree) can also raise PermissionError, but that failure
    # is not about the source root, so it must not be reported as one.
    lock_cm = _source_lock(src_root)
    try:
        lock_cm.__enter__()
    except BlockingIOError:
        return _log_lock_conflict(src_root)
    except PermissionError as e:
        return _log_source_permission_error(src_root, e)

    try:
        return _run_backup_for_endpoints(
            endpoints=endpoints, options=options, args=args
        )
    except PermissionError as e:
        # Deliberately generic: unlike the lock-acquisition case above, this
        # PermissionError could have come from anywhere downstream (most
        # often a read-only destination), so it must not name the source
        # root as the culprit.
        logging.error(
            "Refusing: permission denied during the backup (%s). This may "
            "be the source or the destination — check that irsync can "
            "write to both.",
            e,
        )
        return EXIT_REFUSED
    finally:
        lock_cm.__exit__(None, None, None)


def _run_snapshot_only(
    *, source_arg: str, options: Options, args: argparse.Namespace
) -> int:
    """Take a snapshot of ``source_arg`` and write it next to the source. No dest.

    Raises:
        SourceNotMounted: If the source's drive is not mounted and
            ``args.allow_unmounted`` is not set.
        ValueError: If ``source_arg`` is empty, or is a drive id that no
            loaded config defines (from :func:`~irsync.options.resolve_source`).
        FileNotFoundError: If the resolved source does not exist.
        NotADirectoryError: If the resolved source is not a directory.
    """
    # PermissionError from _source_lock is caught below and turned into
    # EXIT_REFUSED rather than documented here as a raise: it never escapes
    # this function.
    #
    # Drive ids and endpoint names are resolved by the shared resolver in
    # irsync.options rather than re-implemented here: this path used to carry
    # its own copy of the shorthand rules, which is exactly how the two could
    # drift into disagreeing about what a drive id points at.
    src = resolve_source(source_arg, options)

    try:
        check_mounted(
            src, options.base_dir, gate_outside_base=args.require_mount, role="source"
        )
    except EndpointNotMounted:
        if not args.allow_unmounted:
            raise
        logging.warning("Proceeding past the mount check (--allow-unmounted).")

    # Acquire the same lock as a regular backup. Without it, a concurrent
    # `_run_backup_for_endpoints` could call `_cleanup_orphan_tempfiles` and
    # delete this run's `.irsync-snap-*` tempfile mid-write, which would make
    # `os.replace(tmp_path, target)` fail with FileNotFoundError.
    # Same explicit-__enter__/__exit__ split as run_backup, for the same
    # reason: lock acquisition is the only step here proven to be about the
    # source root. There is no destination in this path, so snapshot_tree and
    # _atomic_write_snapshot below also only ever touch src — but keeping the
    # same structure (and the same _log_source_permission_error helper) means
    # a future edit that adds a destination here doesn't have to rediscover
    # this scoping.
    lock_cm = _source_lock(src)
    try:
        lock_cm.__enter__()
    except BlockingIOError:
        return _log_lock_conflict(src)
    except PermissionError as e:
        return _log_source_permission_error(src, e)

    try:
        rows = snapshot_tree(src)
        _atomic_write_snapshot(rows, src, src / SNAPSHOT_FILENAME)
    except PermissionError as e:
        return _log_source_permission_error(src, e)
    finally:
        lock_cm.__exit__(None, None, None)

    logging.info(
        "Snapshot-only: wrote %d rows to %s", len(rows), src / SNAPSHOT_FILENAME
    )
    return 0


def _cleanup_orphan_tempfiles(root: Path) -> int:
    """Delete leftover ``.irsync-snap-*`` tempfiles from prior killed runs.

    ``_atomic_write_snapshot`` uses ``tempfile.mkstemp`` and could leave an
    orphan if the process dies between mkstemp and os.replace. Orphans
    accumulate at the root because rsync's anchored ``/.irsync-snap-*``
    exclude prevents both transfer and ``--delete-before`` cleanup.

    Must be called for both the source root AND the dest root (when
    local): 7th-M1 flipped persist order to dest-first, so a kill
    between mkstemp and os.replace during the dest write strands the
    tempfile there too (8th-NEW-H1).
    """
    removed = 0
    try:
        entries = list(root.iterdir())
    except (FileNotFoundError, PermissionError):
        return 0
    for p in entries:
        if p.name.startswith(SNAPSHOT_TEMPFILE_PREFIX) and p.is_file():
            try:
                p.unlink()
                removed += 1
            except OSError as e:
                logging.warning("Could not remove orphan tempfile %s: %s", p, e)
    if removed:
        logging.info(
            "Cleaned up %d orphan .irsync-snap-* tempfile(s) in %s.", removed, root
        )
    return removed


def _run_backup_for_endpoints(
    *,
    endpoints: Endpoints,
    options: Options,
    args: argparse.Namespace,
) -> int:
    """Run the snapshot, diff, replay, rsync, persist pipeline for one pair.

    Called with the source lock already held (local source) or with no lock
    (remote source). The order of the steps is load-bearing — AD-2's
    no-change short-circuit, the refusal guards (AD-6, AD-25, AD-28),
    AD-22's deferred destination cleanup, and AD-15's persist order — so
    add new work inside the step it belongs to rather than reordering them.

    Args:
        endpoints: The resolved source/destination pair.
        options: Path knobs, including the default exclude list.
        args: Parsed CLI namespace.

    Returns:
        0 on success or when there was nothing to do,
        :data:`EXIT_REFUSED` when a guard refuses (including a corrupt
        snapshot), :data:`EXIT_ABORTED` when the user declines, otherwise
        rsync's own non-zero exit code.

    Raises:
        UnsafeDestination: If there is no usable baseline and the local
            destination cannot be listed.
        PermissionError: If a read or write is denied; :func:`run_backup`
            turns this into a refusal.
    """
    src_str = with_trailing_slash(endpoints.source, remote=endpoints.source_is_remote)
    dest_str = with_trailing_slash(endpoints.dest, remote=endpoints.dest_is_remote)
    logging.info(
        "Backup initiated.\nSource:      %s\nDestination: %s", src_str, dest_str
    )

    src_root: Path | None = (
        endpoints.source if isinstance(endpoints.source, Path) else None
    )
    dest_root: Path | None = (
        endpoints.dest if isinstance(endpoints.dest, Path) else None
    )

    # Sweep up any orphan tempfiles at SRC before snapshotting, so the
    # snapshot doesn't include them and the next diff isn't polluted.
    # The DEST cleanup is deliberately deferred to just before apply_moves
    # below: AD-2 requires that a no-change run never wakes the backup
    # drive (10th-F2). The 8th-NEW-H1 dest-orphan recovery is preserved —
    # any run that actually writes to dest hits the cleanup before the
    # write.
    if src_root is not None:
        _cleanup_orphan_tempfiles(src_root)

    # --snapshot-only: just write the snapshot to the source root and exit.
    if args.snapshot_only:
        if src_root is None:
            logging.error("--snapshot-only requires a local source.")
            return EXIT_REFUSED
        rows = snapshot_tree(src_root)
        _atomic_write_snapshot(rows, src_root, src_root / SNAPSHOT_FILENAME)
        logging.info(
            "Snapshot-only: wrote %d rows to %s",
            len(rows),
            src_root / SNAPSHOT_FILENAME,
        )
        return 0

    # --no-snapshot OR remote source: skip the inode logic and just run rsync.
    if args.no_snapshot or src_root is None:
        if src_root is None:
            logging.warning("Source is remote; inode rename detection disabled.")
        elif args.no_snapshot and not args.allow_empty_source:
            # --no-snapshot never walks the tree, so there is no fresh_rows
            # for fresh_snapshot_is_empty to inspect (that check covers the
            # snapshot path below). A baseline file CAN still exist on disk
            # from an earlier snapshot-taking run — what's true is that this
            # code path never reads or consults one, so it can't tell a
            # genuine "I emptied my source on purpose" run (which a real
            # baseline would prove) from the unmounted/mistyped-source
            # signature. Consequence: a user with a real baseline who
            # genuinely emptied their source and runs --no-snapshot is
            # steered to --allow-empty-source rather than
            # --allow-massive-delete, because the >50% deletion guard is
            # unreachable on this path (it only runs where a baseline is
            # actually read, in the snapshot branch below).
            if source_root_is_empty(src_root):
                logging.error(
                    "Refusing: source %s is empty and --no-snapshot has no "
                    "baseline to compare against. This usually means the "
                    "source drive is not mounted or the path is wrong. Pass "
                    "--allow-empty-source to proceed anyway.",
                    src_root,
                )
                return EXIT_REFUSED
        return _run_rsync_only(
            src_str=src_str, dest_str=dest_str, options=options, args=args
        )

    # Take fresh snapshot of source.
    fresh_rows = snapshot_tree(src_root)

    before_path = src_root / SNAPSHOT_FILENAME
    changes: Changes | None = None
    before_rows: list[Row] = []
    have_before = False
    if before_path.exists():
        try:
            _header, before_rows = read_snapshot(
                before_path, expected_source_root=src_root
            )
            have_before = True
        except SnapshotMismatch as e:
            logging.warning(
                "Existing snapshot at %s does not match source root %s: %s. "
                "Treating as first backup; rsync will reconcile.",
                before_path,
                src_root,
                e,
            )
        except SnapshotCorrupt as e:
            # 7th-M2 refuses a corrupt snapshot whole rather than diffing
            # against part of it. Refuse THIS run with a logged message and
            # a normal return code, so run_all_backups counts it as one
            # drive's error and moves on instead of the whole batch dying.
            logging.error(
                "Refusing: %s. The snapshot cannot be used as a diff baseline. "
                "Move %s out of the source tree to treat the next run as a "
                "first backup (which needs --allow-nonempty-dest if the "
                "destination already holds a backup).",
                e,
                before_path,
            )
            return EXIT_REFUSED
    else:
        logging.info("No prior snapshot; treating as first backup.")

    # rsync's excludes, as a predicate: what the diff covers and, on a first
    # backup, which destination names a real run leaves alone.
    excluded = exclude_matcher([] if args.no_exclude else options.exclude_dirs)
    # What --no-exclude adds back. Runs with the default excludes never
    # copied these, so the backup can lack them however unchanged the source
    # is, and rsync syncs them without the diff showing it.
    unexcluded = options.exclude_dirs if args.no_exclude else []
    # Where the backup can be examined and renamed on; None for a remote one.
    destination: Destination | None = (
        LocalDestination(dest_root) if dest_root is not None else None
    )
    if have_before:
        # Diff only what rsync transfers. A move into an excluded directory,
        # such as a file sent to the desktop trash, is then a deletion from
        # the backup instead of a move replayed where --delete-before never
        # reaches it. Under an excluded name the backup is asked what it
        # holds (AD-36). before_rows becomes the rows the diff was computed
        # against, so the >50% guard below counts deletions against the
        # same tree.
        try:
            changes, before_rows = _plan_changes(
                before_rows,
                fresh_rows,
                exclude_dirs=options.exclude_dirs,
                no_exclude=args.no_exclude,
                destination=destination,
            )
        except DestinationUnreadable as e:
            logging.error(
                "Refusing: cannot read the backup at %s (%s). irsync checks what "
                "the backup holds there before it lists any change; nothing has "
                "been written to it.",
                display_name(e.path),
                e.reason,
            )
            return EXIT_REFUSED
        logging.info(
            "Diff: %d dir moves, %d file moves, %d modified, %d created, %d deleted.",
            len(changes.dir_moves),
            len(changes.file_moves),
            len(changes.modified),
            len(changes.created),
            len(changes.deleted),
        )
        if not changes.any_changes() and not args.force:
            if not unexcluded:
                logging.info(
                    "No source changes since last backup; backup drive untouched."
                )
                return 0
            logging.info(
                "No source changes since last backup, but --no-exclude: running "
                "rsync anyway so the backup gets what the default excludes skip "
                "(%s).",
                ", ".join(unexcluded),
            )
        # Sanity check: a diff that says >50% of the previous tree is gone
        # is almost certainly a wrong-source / stale-snapshot / swapped-args
        # accident. Refuse unless the user explicitly opts in with
        # --allow-massive-delete.
        if before_rows and not args.allow_massive_delete:
            ratio = len(changes.deleted) / len(before_rows)
            if ratio > CATASTROPHIC_DELETE_RATIO:
                logging.error(
                    "Refusing: diff would delete %d of %d previously-recorded "
                    "entries (%.0f%%). This usually means the source tree has "
                    "been swapped, the snapshot is from a different tree, or "
                    "args were reversed. Pass --allow-massive-delete to override.",
                    len(changes.deleted),
                    len(before_rows),
                    ratio * 100,
                )
                return EXIT_REFUSED

    # No usable baseline AND an empty source: never a legitimate backup. A
    # user who genuinely has an empty tree to back up has nothing worth
    # protecting yet; what this actually catches is the unmounted/mistyped
    # source that presents as an empty directory. Deliberately independent
    # of what the destination holds — even an equally-empty destination is
    # refused, because "empty source, no baseline" is itself the signature,
    # not a symptom that requires a non-empty destination to notice. Must
    # not also fire when a baseline EXISTS: that case (a real deletion) is
    # already covered by the catastrophic-delete guard above.
    if (
        not have_before
        and not args.allow_empty_source
        and fresh_snapshot_is_empty(fresh_rows)
    ):
        logging.error(
            "Refusing: source %s is empty and there is no prior snapshot to "
            "use as a baseline. This usually means the source drive is not "
            "mounted or the path is wrong. Pass --allow-empty-source to "
            "proceed anyway.",
            src_root,
        )
        return EXIT_REFUSED

    # No usable baseline: this run will transfer the whole source and delete
    # everything at the destination that is not on it. If the destination
    # already holds data, that is the unmounted-source scenario — refuse.
    foreign: list[str] = []
    source_names: list[str] = []
    dest_split: TopLevelSplit | None = None
    if not have_before:
        source_names = top_level_names(src_root)
    if not have_before and dest_root is not None:
        foreign = foreign_dest_entries(dest_root)
        split = split_top_level(foreign, source_names, excluded)
        # Names found prove the destination exists even if it vanished since;
        # only a destination that is not there is reported as missing.
        if foreign or dest_root.exists():
            dest_split = split
        # A dry run writes nothing — it is the natural way to inspect what's
        # at the destination before deciding whether to adopt it — so it must
        # not be blocked by the same refusal that guards the real transfer.
        # `foreign` is still computed unconditionally above: the first-run
        # preview (``_format_first_run_preview``) reports it regardless of
        # --dry-run.
        if foreign and not args.allow_nonempty_dest and not args.dry_run:
            deleted = (
                " Not on the source (a real run DELETES these): "
                f"{_name_list(split.not_on_source)}."
                if split.not_on_source
                else ""
            )
            logging.error(
                "Refusing: no usable snapshot, but the destination %s already "
                "holds %s (%s).%s A first backup makes the destination match "
                "the source and DELETES anything there that is not on the "
                "source. Check that the source drive is mounted and that this "
                "is its backup, then pass --allow-nonempty-dest to adopt it.",
                display_name(str(dest_root)),
                _count(len(foreign), "top-level item", "top-level items"),
                _split_counts(split),
                deleted,
            )
            return EXIT_REFUSED

    # Always show the preview before confirming. In interactive mode, page it
    # through less so the user can scroll through the deletion list. In --yes
    # mode, dump it once so the run is at least auditable in scrollback / logs.
    if changes is not None:
        _show_preview(
            changes,
            interactive=not args.yes,
            unexcluded=unexcluded,
            checked=destination is not None,
        )
    else:
        _emit_preview(
            _format_first_run_preview(
                fresh_rows,
                src_root,
                len(source_names),
                dest_root,
                dest_split,
                show_hint=not args.yes and not args.dry_run,
            ),
            interactive=not args.yes,
        )
    if not _confirm_or_abort(args):
        logging.info("Aborted by user.")
        return EXIT_ABORTED

    # --dry-run: show the rsync dry-run; don't touch dest tree or persist snapshot.
    if args.dry_run:
        cmd = build_rsync_command(
            source=src_str,
            dest=dest_str,
            dry_run=True,
            exclude_dirs=options.exclude_dirs,
            no_exclude=args.no_exclude,
            ssh_port=args.ssh_port,
            ssh_key=args.ssh_key,
        )
        out, rc = _dry_run_preview(cmd)
        if out is None:
            return rc
        # rsync's own --dry-run output, shown after confirmation; not an AD-7
        # preview, so never paged.
        print(out)
        return 0

    # 10th-F2: dest cleanup deferred to here so a no-change run (which
    # already returned at the any_changes() short-circuit above) never
    # touches the backup drive. 8th-NEW-H1's "orphan-on-dest" recovery
    # path is still intact: every run that reaches apply_moves / rsync
    # cleans dest first.
    if dest_root is not None:
        _cleanup_orphan_tempfiles(dest_root)

    # Apply moves on dest tree first (only if we have a local dest and a diff).
    if changes is not None and destination is not None:
        try:
            result = destination.replay(
                dir_moves=changes.dir_moves, file_moves=changes.file_moves
            )
        except CrossDeviceMoveError as e:
            # NEW-H2 (5th-pass). Refuse upfront so we don't leave dest in
            # the partial state that would otherwise loop forever (next run
            # would re-issue the same plan and hit the same EXDEV at the
            # same point). Snapshot is intentionally NOT persisted so the
            # corrected re-run still has the right baseline.
            logging.error(
                "Refusing to apply moves: dest tree spans multiple "
                "filesystems (%s). Move every cross-FS path under one "
                "filesystem, or pass --no-snapshot to skip the rename "
                "optimization.",
                e,
            )
            return EXIT_REFUSED
        logging.info(
            "Replay: %d dir moves applied, %d file moves applied, %d skipped.",
            result.dirs_moved,
            result.files_moved,
            result.skipped,
        )

    # Run real rsync.
    cmd = build_rsync_command(
        source=src_str,
        dest=dest_str,
        dry_run=False,
        exclude_dirs=options.exclude_dirs,
        no_exclude=args.no_exclude,
        ssh_port=args.ssh_port,
        ssh_key=args.ssh_key,
    )
    rc = run_real_sync(cmd)
    if rc != 0:
        logging.error("rsync failed; not updating snapshot.")
        return rc

    # Persist the fresh snapshot to source and (if local) dest.
    _persist_snapshots(fresh_rows, src_root, dest_root)
    return 0


def _run_rsync_only(
    *,
    src_str: str,
    dest_str: str,
    options: Options,
    args: argparse.Namespace,
) -> int:
    """Run rsync directly, with no snapshot layer.

    Used for ``--no-snapshot`` and for a remote source. Always runs the
    dry-run first so there is a preview to show (NEW-M1), stops after it
    under ``--dry-run`` (hazard H, AD-30), and otherwise runs the real
    transfer.

    Args:
        src_str: Source endpoint, with its trailing slash.
        dest_str: Destination endpoint, with its trailing slash.
        options: Path knobs, including the default exclude list.
        args: Parsed CLI namespace.

    Returns:
        0 on success (including a completed ``--dry-run``),
        :data:`EXIT_ABORTED` when the user declines, otherwise the failing
        rsync run's exit code (:data:`EXIT_REFUSED` for a signal-killed
        preview).
    """
    dry_cmd = build_rsync_command(
        source=src_str,
        dest=dest_str,
        dry_run=True,
        exclude_dirs=options.exclude_dirs,
        no_exclude=args.no_exclude,
        ssh_port=args.ssh_port,
        ssh_key=args.ssh_key,
    )
    # NEW-M1 (5th-pass). Always run the dry-run so a preview exists. Under
    # --yes (cron), print to stdout for auditability (AD-7); interactively,
    # page through `less` and prompt for confirmation. Uses the same
    # _emit_preview as the snapshot path, so the two code paths cannot drift
    # apart on AD-7.
    out, preview_rc = _dry_run_preview(dry_cmd)
    if out is None:
        # The preview failed, so we have nothing to show the user and no
        # evidence the real transfer would fare better. Abort before touching
        # the destination.
        return preview_rc
    _emit_preview(out, interactive=not args.yes)
    if not _confirm_or_abort(args):
        return EXIT_ABORTED

    # Hazard H fix: --dry-run was never consulted on this path, so
    # `--no-snapshot --dry-run` fell through to a real, destructive rsync
    # despite the flag. The dry-run preview above already IS rsync's
    # --dry-run output, so honoring the flag here is just: stop before the
    # real transfer and report success, changing nothing — the same
    # contract the snapshot path applies at its own `if args.dry_run:`
    # check in _run_backup_for_endpoints.
    if args.dry_run:
        return 0

    cmd = build_rsync_command(
        source=src_str,
        dest=dest_str,
        dry_run=False,
        exclude_dirs=options.exclude_dirs,
        no_exclude=args.no_exclude,
        ssh_port=args.ssh_port,
        ssh_key=args.ssh_key,
    )
    return run_real_sync(cmd)


def _page_output(text: str) -> None:
    """Pipe ``text`` through ``less`` if available, else print directly."""
    pager = shutil.which("less")
    if pager:
        # 8th-M1: detach the pager from the parent's tty/session so a
        # SIGKILL of the parent doesn't leave less zombied on the
        # controlling terminal. Parity with run_real_sync's 7th-pass
        # session isolation.
        proc = subprocess.Popen(  # noqa: S603
            [pager], stdin=subprocess.PIPE, text=True, start_new_session=True
        )
        # communicate() already writes stdin, closes it, and reaps the child.
        proc.communicate(input=text)
    else:
        print(text)


def run_all_backups(*, options: Options, args: argparse.Namespace) -> int:
    """Iterate :data:`Options.all_backups` and back up each in turn.

    Exit-code contract: **an unmounted SOURCE is a skip, not an error; an
    unmounted DESTINATION is a real error.**
    :data:`Options.all_backups` is the configured backup order, which lists
    every drive the user might ever attach, and on any given day most of
    them are absent, so a source that isn't there is the normal case and an
    ALL run that skips it still reports success
    (:class:`~irsync.preflight.SourceNotMounted`, alongside a plain missing
    directory). A destination that isn't mounted is different: if a drive is
    mounted but its backup drive is not, the run
    would back up nothing while still exiting 0, which is exactly the
    silent success the destination mount gate exists to prevent (AD-29). So
    :class:`~irsync.preflight.DestinationNotMounted` — like an unreadable
    destination (:class:`~irsync.preflight.UnsafeDestination`) — is counted
    as a real error, same as a non-zero ``run_backup`` return, and the
    batch continues to the next drive rather than aborting. A bare
    :class:`PermissionError` is caught alongside them as a backstop: most
    permission failures (e.g. :func:`~irsync.preflight.source_root_is_empty`
    on the ``--no-snapshot`` path) are already turned into a plain non-zero
    return inside ``run_backup`` itself, but one raised earlier — from
    ``resolve_endpoints``/``check_mounted``, before ``run_backup``'s own
    try/except is entered — would otherwise escape uncaught here and abort
    the whole batch instead of just the one drive. Any other exception
    raised for one drive is logged with its traceback and counted the same
    way, so one drive cannot abort the batch; the exception is
    :class:`~irsync.preflight.RsyncUnavailable`, which is the whole run's
    problem and still propagates for ``cli.main`` to report. Only a real backup
    failure (including a drive refused by a guard, such as one whose
    snapshot is corrupt), an unmounted/unreadable destination, an
    unreadable source, or such an unexpected exception makes this return 1;
    a user abort propagates :data:`EXIT_ABORTED` and stops the remaining
    drives.

    Both the skipped and the successfully-backed-up entries are named in the
    summary log line so a cron log records what actually happened.

    Returns:
        0 when every attempted backup succeeded (whether or not drives were
        skipped), 1 when at least one drive failed, or :data:`EXIT_ABORTED`
        when the user aborted.
    """
    logging.info("Backing up all drives: %s", ", ".join(options.all_backups))
    total_errors = 0
    successful: list[str] = []
    missing: list[str] = []
    for backup in options.all_backups:
        try:
            rc = run_backup(
                source_arg=backup,
                destination_arg=None,
                options=options,
                args=args,
            )
        except (DestinationNotMounted, UnsafeDestination, PermissionError) as e:
            # Caught BEFORE the (broader) EndpointNotMounted clause below:
            # DestinationNotMounted is a subclass of it, and a subclass must
            # be caught before its parent or this handler is dead code.
            # Unlike a missing/unmounted SOURCE, none of these is a benign
            # absence — the run would silently back up nothing (an
            # unmounted destination) or the destination couldn't be checked
            # for safety at all (unreadable). PermissionError is caught here
            # too as a backstop: run_backup already converts a
            # PermissionError raised inside _run_backup_for_endpoints (e.g.
            # from preflight.source_root_is_empty on the --no-snapshot path)
            # into a plain non-zero return, so in practice this clause only
            # fires for a PermissionError raised *before* that point
            # (resolve_endpoints/check_mounted, which run outside
            # run_backup's own try/except). Either way, an unreadable path
            # is a real misconfiguration, not "drive absent today", so it
            # fails closed the same way an unreadable destination does: all
            # three count as a real error (never silently a skip) but do
            # not abort the batch; the next drive still gets a chance.
            logging.error("Error backing up %r: %s", backup, e)
            total_errors += 1
            if total_errors >= options.max_errors:
                logging.error("Max errors (%d) reached; stopping.", options.max_errors)
                return 1
            continue
        except (FileNotFoundError, NotADirectoryError, EndpointNotMounted) as e:
            # A bare EndpointNotMounted (not a DestinationNotMounted
            # instance) and SourceNotMounted both land here, alongside a
            # plain missing/wrong-type path: all are the benign
            # "drive absent today" case commit 044c036 established as a
            # skip, not an error.
            logging.error("Skipping %r (missing/unmounted drive): %s", backup, e)
            missing.append(backup)
            continue
        except RsyncUnavailable:
            # Missing rsync is the run's problem, not this drive's: let
            # cli.main report it once (exit 2) rather than once per drive.
            raise
        except Exception:
            # Backstop for anything the clauses above do not name: an
            # unforeseen bug, or a failure such as a directory where the
            # destination's snapshot copy belongs. It is that drive's error,
            # not the batch's. logging.exception keeps the traceback so a
            # real bug stays visible; KeyboardInterrupt and SystemExit are
            # not Exceptions and still stop the run.
            logging.exception(
                "Unexpected error backing up %r; counting it as an error.", backup
            )
            rc = 1
        if rc == 0:
            successful.append(backup)
        elif rc == EXIT_ABORTED:
            logging.info("User aborted; stopping ALL processing.")
            return rc
        else:
            total_errors += 1
            if total_errors >= options.max_errors:
                logging.error("Max errors (%d) reached; stopping.", options.max_errors)
                return 1
    if total_errors == 0 and not missing:
        logging.info(
            "All backups completed successfully: %s.", ", ".join(successful) or "none"
        )
        return 0
    logging.warning(
        "Finished with issues. backed up=%d (%s), errors=%d, missing/skipped=%d (%s).",
        len(successful),
        ", ".join(successful) or "none",
        total_errors,
        len(missing),
        ", ".join(missing) or "none",
    )
    return 1 if total_errors else 0
