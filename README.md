# irsync

Rename-aware Python wrapper around `rsync`. When files are moved or renamed on
the source drive, the original `rsync` deletes the old path on the backup drive
and re-transfers the (often huge) file from scratch. `irsync` avoids that by
keeping an inode snapshot at the root of the source tree, comparing it against
a fresh snapshot on each run, and replaying just the moves/renames atomically
on the backup tree before invoking `rsync` for the actual content delta. When
no source changes are detected at all, the backup drive is never accessed.

## Installation

```bash
pixi install
```

`rsync` is provided via the pixi environment. To run irsync on a system where
rsync is installed natively, just point `python -m irsync` at it.

## Quick usage

```bash
# First backup of a drive: snapshots both sides, then runs rsync.
pixi run python -m irsync /mnt/data /mnt/data_backup --yes

# Subsequent run with files renamed on /mnt/data:
# inode diff is computed, the renames are replayed on /mnt/data_backup using
# os.rename, and rsync handles only the actual content changes.
pixi run python -m irsync /mnt/data /mnt/data_backup --yes

# Subsequent run with no source changes: backup drive is never touched.
pixi run python -m irsync /mnt/data /mnt/data_backup --yes

# Force a full rsync run even when the snapshot says nothing changed.
pixi run python -m irsync /mnt/data /mnt/data_backup --yes --force

# Take a baseline snapshot without backing up (e.g. before a big reorg).
pixi run python -m irsync /mnt/data --snapshot-only

# Skip the inode logic entirely and behave like the original srsync.
pixi run python -m irsync /mnt/data /mnt/data_backup --no-snapshot
```

Drive-letter shorthand (`G`), `~` for home, `mypython` for the Python source
directory, and `ALL` for every configured drive are also supported, mirroring
the original `srsync` script.

## Project structure

```
src/irsync/
  paths.py          # is_rsync_remote, ensure_local_dir, with_trailing_slash
  snapshot.py       # snapshot_tree, write_jsonl, read_jsonl, SNAPSHOT_FILENAME
  diff.py           # compute_changes, plan_directory_moves (cycle-safe)
  replay.py         # apply_moves — atomic os.rename on the dest tree
  rsync_runner.py   # build_rsync_command, run_dry_run, run_real_sync
  options.py        # Options dataclass, resolve_endpoints
  backup.py         # the orchestrator: snapshot → diff → replay → rsync → persist
  cli.py            # argparse CLI
tests/              # pytest unit + end-to-end tests
srsync              # legacy reference script (not imported)
inode_compare.py    # legacy reference script (not imported)
```

## Dev tasks

```bash
pixi run test         # pytest
pixi run lint         # ruff check
pixi run format       # ruff format
pixi run typecheck    # mypy --strict
pixi run pre-commit run --all-files
```
