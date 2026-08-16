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
from collections.abc import Iterator
from pathlib import Path

from irsync.diff import Changes, compute_changes
from irsync.options import Endpoints, Options, resolve_endpoints, resolve_source
from irsync.paths import with_trailing_slash
from irsync.preflight import (
    DestinationNotMounted,
    EndpointNotMounted,
    UnsafeDestination,
    check_mounted,
    check_rsync_available,
    foreign_dest_entries,
    fresh_snapshot_is_empty,
    source_root_is_empty,
)
from irsync.replay import CrossDeviceMoveError, apply_moves
from irsync.rsync_runner import build_rsync_command, run_dry_run, run_real_sync
from irsync.snapshot import (
    LOCKFILE_NAME,
    SNAPSHOT_FILENAME,
    SNAPSHOT_TEMPFILE_PREFIX,
    Row,
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
    fd, tmpname = tempfile.mkstemp(prefix=".irsync-snap-", dir=target.parent)
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


def _format_preview(changes: Changes) -> str:
    """Build the full pre-confirmation preview text — every move, deletion, creation.

    The deletion list is the safety-critical part: rsync's ``--delete-before`` will
    remove these from the backup tree, so the user must see the actual paths (not
    just a count) before saying yes.
    """
    lines: list[str] = []
    lines.append(f"=== Directory moves ({len(changes.dir_moves)}) ===")
    for o, n in changes.dir_moves:
        lines.append(f"  {o}  ->  {n}")
    lines.append(f"=== File moves ({len(changes.file_moves)}) ===")
    for o, n in changes.file_moves:
        lines.append(f"  {o}  ->  {n}")
    lines.append(
        f"=== Modified in place, will be re-transferred ({len(changes.modified)}) ==="
    )
    for p in changes.modified:
        lines.append(f"  ~ {p}")
    lines.append(
        f"=== Created on source, will be transferred ({len(changes.created)}) ==="
    )
    for p in changes.created:
        lines.append(f"  + {p}")
    lines.append(
        f"=== Deleted on source, will be REMOVED FROM BACKUP ({len(changes.deleted)}) ==="
    )
    for p in changes.deleted:
        lines.append(f"  - {p}")
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


def _format_first_run_preview(
    rows: list[Row],
    src_root: Path,
    dest_root: Path | None,
    foreign_count: int,
) -> str:
    """Summarize a backup that has no baseline to diff against.

    With no prior snapshot there is no move/delete plan to show, but the run
    is the most consequential one: it transfers the whole source and removes
    anything at the destination that is not on it. Report the scale of both
    so the confirmation prompt is not answered blind.
    """
    total_bytes = sum(r["size"] for r in rows if r["type"] == "f")
    size_str = _format_size(total_bytes)
    # A remote destination is never scanned for foreign_dest_entries (Task 3
    # only counts a local dest's existing entries), so foreign_count is
    # always 0 here — not because the destination is empty, but because it
    # was never checked. Say so instead of presenting an unchecked 0 as fact.
    if dest_root is None:
        dest_desc = "n/a (remote)"
        existing_desc = "not checked (remote)"
    else:
        dest_desc = str(dest_root)
        existing_desc = f"{foreign_count} existing entries"
    lines = [
        "=== FIRST BACKUP — no prior snapshot ===",
        f"Source:      {src_root}   {len(rows):,} entries, {size_str}",
        f"Destination: {dest_desc}   {existing_desc}",
        "rsync will transfer the source in full and DELETE anything at the",
        "destination that is not on the source.",
    ]
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


def _show_preview(changes: Changes, *, interactive: bool) -> None:
    """Render the preview, paging through ``less`` if interactive."""
    text = _format_preview(changes)
    if interactive:
        _page_output(text)
    else:
        # Non-interactive (--yes): still log it so the run is auditable, but
        # don't bother with the pager.
        print(text)


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
    else:
        logging.info("No prior snapshot; treating as first backup.")

    if have_before:
        changes = compute_changes(before_rows, fresh_rows)
        logging.info(
            "Diff: %d dir moves, %d file moves, %d created, %d deleted.",
            len(changes.dir_moves),
            len(changes.file_moves),
            len(changes.created),
            len(changes.deleted),
        )
        if not changes.any_changes() and not args.force:
            logging.info("No source changes since last backup; backup drive untouched.")
            return 0
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
    if not have_before and dest_root is not None:
        foreign = foreign_dest_entries(dest_root)
        # A dry run writes nothing — it is the natural way to inspect what's
        # at the destination before deciding whether to adopt it — so it must
        # not be blocked by the same refusal that guards the real transfer.
        # `foreign` is still computed unconditionally above: the first-run
        # preview (Rule 3) reports it regardless of --dry-run.
        if foreign and not args.allow_nonempty_dest and not args.dry_run:
            shown = ", ".join(foreign[:10])
            more = f" (and {len(foreign) - 10} more)" if len(foreign) > 10 else ""
            logging.error(
                "Refusing: no prior snapshot, but the destination %s already "
                "contains %d entries: %s%s. rsync would DELETE them. This "
                "usually means the source drive is not mounted. Pass "
                "--allow-nonempty-dest to adopt this destination anyway.",
                dest_root,
                len(foreign),
                shown,
                more,
            )
            return EXIT_REFUSED

    # Always show the preview before confirming. In interactive mode, page it
    # through less so the user can scroll through the deletion list. In --yes
    # mode, dump it once so the run is at least auditable in scrollback / logs.
    if changes is not None:
        _show_preview(changes, interactive=not args.yes)
    else:
        text = _format_first_run_preview(fresh_rows, src_root, dest_root, len(foreign))
        if args.yes:
            print(text)
        else:
            _page_output(text)
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
    if changes is not None and dest_root is not None:
        try:
            result = apply_moves(
                dir_moves=changes.dir_moves,
                file_moves=changes.file_moves,
                dest_root=dest_root,
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
    # page through `less` and prompt for confirmation. Mirrors the snapshot
    # path's _show_preview(..., interactive=not args.yes) behavior so the
    # two code paths are consistent.
    out, preview_rc = _dry_run_preview(dry_cmd)
    if out is None:
        # The preview failed, so we have nothing to show the user and no
        # evidence the real transfer would fare better. Abort before touching
        # the destination.
        return preview_rc
    if args.yes:
        print(out)
    else:
        _page_output(out)
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
    silent-success hazard this branch exists to close. So
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
    the whole batch instead of just the one drive. Only a real backup
    failure, an unmounted/unreadable destination, or an unreadable source
    makes this return 1; a user abort propagates :data:`EXIT_ABORTED` and
    stops the remaining drives.

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
