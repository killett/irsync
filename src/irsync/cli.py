"""Command-line interface for irsync."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from irsync import __version__


def _build_parser(
    options_homedir_backup: str, options_python_backup: str
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="irsync",
        description=(
            f'"Intelligent rsync" version {__version__}. A rename-aware Python wrapper around '
            "rsync. Before each backup, irsync compares an inode snapshot of the source tree "
            "against the previous snapshot to detect moves/renames; those moves are replayed "
            "atomically on the backup tree before rsync runs, so renamed multi-GB files are "
            "not retransferred. If nothing changed since the last backup, the backup drive is "
            "never accessed."
        ),
    )
    parser.add_argument(
        "source_arg",
        nargs="?",
        type=str,
        help=(
            "Source: a full path, a drive letter, '~' for the home directory "
            f"(backed up to {options_homedir_backup}), 'mypython' for the Python directory "
            f"(backed up to {options_python_backup}), 'ALL' to back up every configured drive, "
            "or an rsync remote like host:/path."
        ),
    )
    parser.add_argument(
        "destination_arg",
        nargs="?",
        type=str,
        help=(
            "Destination. Optional when SOURCE is a drive letter, '~', or 'mypython'; "
            "in those cases it defaults to the configured backup location."
        ),
    )
    parser.add_argument(
        "--ssh-port", type=int, metavar="PORT", help="SSH port for remote endpoints."
    )
    parser.add_argument(
        "--ssh-key",
        type=str,
        metavar="PATH",
        help="SSH identity file for remote endpoints.",
    )
    parser.add_argument(
        "--no-exclude",
        action="store_true",
        help="Do not apply the default exclude list.",
    )
    parser.add_argument(
        "--allow-unmounted",
        action="store_true",
        help=(
            "Proceed even when a drive under the media base directory is not "
            "mounted. Without this, irsync refuses, because an unmounted drive "
            "looks like an empty tree and would wipe its own backup."
        ),
    )
    parser.add_argument(
        "--require-mount",
        action="store_true",
        help=(
            "Apply the mountpoint check to endpoints outside the media base "
            "directory too."
        ),
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", help="Skip the confirmation prompt."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run rsync even when the source snapshot shows no changes.",
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Skip the snapshot/diff/replay layer and run rsync directly against the source and destination.",
    )
    parser.add_argument(
        "--snapshot-only",
        action="store_true",
        help="Take a snapshot of SOURCE, write it next to the source, and exit. No backup.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be moved and rsync's --dry-run output, but apply nothing.",
    )
    parser.add_argument(
        "-d", "--debug", action="store_true", help="Enable debug logging."
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    from irsync.backup import run_all_backups, run_backup
    from irsync.options import Options

    options = Options.from_defaults()
    parser = _build_parser(str(options.homedir_backup), str(options.python_backup_dir))

    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        parser.print_help()
        return 0

    args = parser.parse_args(argv)

    if args.ssh_port is not None and not (1 <= args.ssh_port <= 65535):
        parser.error("--ssh-port must be in 1..65535")
    if args.ssh_key is not None and not Path(args.ssh_key).expanduser().is_file():
        parser.error(f"--ssh-key file does not exist: {args.ssh_key}")
    # NEW-M2 (5th-pass). --force only takes effect inside the snapshot diff
    # branch (no-changes short-circuit + 50% deletion threshold). Combined
    # with --no-snapshot it does nothing; reject upfront so the user notices
    # rather than running with one of their flags silently ignored.
    if args.force and args.no_snapshot:
        parser.error(
            "--force has no effect with --no-snapshot (the snapshot diff is "
            "what --force overrides). Drop one of --force or --no-snapshot."
        )

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not args.source_arg:
        parser.print_help()
        return 0

    if args.source_arg.strip().upper() == "ALL":
        if args.destination_arg:
            parser.error("Destination is not allowed when SOURCE is 'ALL'.")
        return run_all_backups(options=options, args=args)

    return run_backup(
        source_arg=args.source_arg,
        destination_arg=args.destination_arg,
        options=options,
        args=args,
    )
