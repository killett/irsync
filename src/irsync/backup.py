"""End-to-end orchestrator: snapshot, diff, optionally replay-and-rsync, persist."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess  # noqa: S404 — needed for paging via less
import tempfile
from pathlib import Path

from irsync.diff import Changes, compute_changes
from irsync.options import Endpoints, Options, resolve_endpoints
from irsync.paths import with_trailing_slash
from irsync.replay import apply_moves
from irsync.rsync_runner import build_rsync_command, run_dry_run, run_real_sync
from irsync.snapshot import (
    SNAPSHOT_FILENAME,
    Row,
    read_jsonl,
    snapshot_tree,
    write_jsonl,
)


def _atomic_write_jsonl(rows: list[Row], target: Path) -> None:
    """Write rows to a JSONL file atomically (tempfile + replace)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(prefix=".irsync-snap-", dir=target.parent)
    os.close(fd)
    tmp_path = Path(tmpname)
    try:
        write_jsonl(rows, tmp_path)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _persist_snapshots(rows: list[Row], src_root: Path, dest_root: Path | None) -> None:
    """Write the fresh snapshot to the source root and (if local) the dest root."""
    _atomic_write_jsonl(rows, src_root / SNAPSHOT_FILENAME)
    if dest_root is not None:
        _atomic_write_jsonl(rows, dest_root / SNAPSHOT_FILENAME)


def _show_preview(changes: Changes) -> None:
    """Print a human-readable summary of what irsync is about to do."""
    print(f"Directory moves: {len(changes.dir_moves)}")
    for o, n in changes.dir_moves:
        print(f"  {o!s}  ->  {n!s}")
    print(f"File moves:      {len(changes.file_moves)}")
    for o, n in changes.file_moves:
        print(f"  {o!s}  ->  {n!s}")
    print(f"Created entries: {len(changes.created)}")
    print(f"Deleted entries: {len(changes.deleted)}")


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
    endpoints = resolve_endpoints(source_arg, destination_arg, options)
    return _run_backup_for_endpoints(endpoints=endpoints, options=options, args=args)


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

    # --snapshot-only: just write the snapshot to the source root and exit.
    if args.snapshot_only:
        if src_root is None:
            logging.error("--snapshot-only requires a local source.")
            return 2
        rows = snapshot_tree(src_root)
        _atomic_write_jsonl(rows, src_root / SNAPSHOT_FILENAME)
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
    if before_path.exists():
        before_rows = read_jsonl(before_path)
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
    else:
        logging.info("No prior snapshot; treating as first backup.")

    # Show preview unless --yes (the preview is for the human to inspect deletions).
    if changes is not None:
        _show_preview(changes)
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
        result = apply_moves(
            dir_moves=changes.dir_moves,
            file_moves=changes.file_moves,
            dest_root=dest_root,
        )
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
    cmd = build_rsync_command(
        source=src_str,
        dest=dest_str,
        dry_run=False,
        exclude_dirs=options.exclude_dirs,
        no_exclude=args.no_exclude,
        ssh_port=args.ssh_port,
        ssh_key=args.ssh_key,
    )
    if not args.yes:
        dry_cmd = build_rsync_command(
            source=src_str,
            dest=dest_str,
            dry_run=True,
            exclude_dirs=options.exclude_dirs,
            no_exclude=args.no_exclude,
            ssh_port=args.ssh_port,
            ssh_key=args.ssh_key,
        )
        out = run_dry_run(dry_cmd)
        _page_output(out)
        if not _confirm_or_abort(args):
            return 130
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
