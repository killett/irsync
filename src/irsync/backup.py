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
from irsync.options import Endpoints, Options, resolve_endpoints
from irsync.paths import with_trailing_slash
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
    """
    # --snapshot-only doesn't need a destination at all — it just records the
    # current state of the source for use as a future baseline. Route around
    # resolve_endpoints so the user can snapshot any local directory without
    # having to invent a dest argument.
    if args.snapshot_only and not destination_arg:
        return _run_snapshot_only(source_arg=source_arg, options=options)

    endpoints = resolve_endpoints(source_arg, destination_arg, options)
    src_root = endpoints.source if isinstance(endpoints.source, Path) else None
    if src_root is None:
        # Remote source: nowhere to put the lock. Skip the lock and let
        # rsync's own protocol coordinate with the remote.
        return _run_backup_for_endpoints(
            endpoints=endpoints, options=options, args=args
        )
    try:
        with _source_lock(src_root):
            return _run_backup_for_endpoints(
                endpoints=endpoints, options=options, args=args
            )
    except BlockingIOError:
        logging.error(
            "Another irsync run is already in progress against %s "
            "(lock %s is held). Refusing to race.",
            src_root,
            src_root / LOCKFILE_NAME,
        )
        return 75  # EX_TEMPFAIL — try again later


def _run_snapshot_only(*, source_arg: str, options: Options) -> int:
    """Take a snapshot of ``source_arg`` and write it next to the source. No dest."""
    from irsync.paths import ensure_local_dir

    # Resolve drive-letter / ~ / mypython shortcuts manually so the shorthand
    # still works without requiring a dest.
    arg = source_arg.strip()
    src: Path
    if len(arg) == 1 and arg.isalpha():
        src = ensure_local_dir(options.base_dir / arg.upper())
    elif arg == "~":
        src = ensure_local_dir(options.homedir)
    elif arg == "mypython":
        src = ensure_local_dir(options.python_dir)
    else:
        src = ensure_local_dir(arg)

    # Acquire the same lock as a regular backup. Without it, a concurrent
    # `_run_backup_for_endpoints` could call `_cleanup_orphan_tempfiles` and
    # delete this run's `.irsync-snap-*` tempfile mid-write, which would make
    # `os.replace(tmp_path, target)` fail with FileNotFoundError.
    try:
        with _source_lock(src):
            rows = snapshot_tree(src)
            _atomic_write_snapshot(rows, src, src / SNAPSHOT_FILENAME)
    except BlockingIOError:
        logging.error(
            "Another irsync run is already in progress against %s "
            "(lock %s is held). Refusing to race.",
            src,
            src / LOCKFILE_NAME,
        )
        return 75  # EX_TEMPFAIL — try again later

    logging.info(
        "Snapshot-only: wrote %d rows to %s", len(rows), src / SNAPSHOT_FILENAME
    )
    return 0


def _cleanup_orphan_tempfiles(src_root: Path) -> int:
    """Delete leftover ``.irsync-snap-*`` tempfiles from prior killed runs.

    ``_atomic_write_snapshot`` uses ``tempfile.mkstemp`` and could leave an
    orphan if the process dies between mkstemp and os.replace. Without this
    cleanup the orphans accumulate at the source root, get included in the
    next snapshot, and get backed up to dest as garbage.
    """
    removed = 0
    try:
        entries = list(src_root.iterdir())
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
        logging.info("Cleaned up %d orphan .irsync-snap-* tempfile(s).", removed)
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

    # Sweep up any orphan tempfiles BEFORE snapshotting, so the snapshot
    # doesn't include them and the next diff isn't polluted.
    if src_root is not None:
        _cleanup_orphan_tempfiles(src_root)

    # --snapshot-only: just write the snapshot to the source root and exit.
    if args.snapshot_only:
        if src_root is None:
            logging.error("--snapshot-only requires a local source.")
            return 2
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
        # accident. Refuse unless the user explicitly opts in with --force.
        if before_rows and not args.force:
            ratio = len(changes.deleted) / len(before_rows)
            if ratio > CATASTROPHIC_DELETE_RATIO:
                logging.error(
                    "Refusing: diff would delete %d of %d previously-recorded "
                    "entries (%.0f%%). This usually means the source tree has "
                    "been swapped, the snapshot is from a different tree, or "
                    "args were reversed. Pass --force to override.",
                    len(changes.deleted),
                    len(before_rows),
                    ratio * 100,
                )
                return 2

    # Always show the preview before confirming. In interactive mode, page it
    # through less so the user can scroll through the deletion list. In --yes
    # mode, dump it once so the run is at least auditable in scrollback / logs.
    if changes is not None:
        _show_preview(changes, interactive=not args.yes)
    if not _confirm_or_abort(args):
        logging.info("Aborted by user.")
        return 130

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
        out = run_dry_run(cmd)
        print(out)
        return 0

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
            return 2
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
    out = run_dry_run(dry_cmd)
    if args.yes:
        print(out)
    else:
        _page_output(out)
        if not _confirm_or_abort(args):
            return 130
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
        proc = subprocess.Popen([pager], stdin=subprocess.PIPE, text=True)  # noqa: S603
        proc.communicate(input=text)
        proc.wait()
    else:
        print(text)


def run_all_backups(*, options: Options, args: argparse.Namespace) -> int:
    """Iterate :data:`Options.all_backups` and back up each in turn."""
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
        except (FileNotFoundError, NotADirectoryError) as e:
            logging.error("Skipping %r (missing drive/path): %s", backup, e)
            missing.append(backup)
            continue
        if rc == 0:
            successful.append(backup)
        elif rc == 130:
            logging.info("User aborted; stopping ALL processing.")
            return rc
        else:
            total_errors += 1
            if total_errors >= options.max_errors:
                logging.error("Max errors (%d) reached; stopping.", options.max_errors)
                return 1
    if total_errors == 0 and not missing:
        logging.info("All backups completed successfully.")
        return 0
    logging.warning(
        "Finished with issues. errors=%d, missing/skipped=%d (%s).",
        total_errors,
        len(missing),
        ", ".join(missing),
    )
    return 1 if total_errors else 0
