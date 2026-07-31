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
pip install irsync
```

`irsync` calls the system `rsync` binary, so `rsync` must be installed and on
your `PATH`. It has no other runtime dependencies (standard library only).

## Quick usage

Installation puts an `irsync` command on your `PATH` (equivalently,
`python -m irsync`):

```bash
# First backup of a drive: snapshots both sides, then runs rsync.
irsync /mnt/data /mnt/data_backup --yes

# Subsequent run with files renamed on /mnt/data:
# the inode diff is computed, the renames are replayed on /mnt/data_backup
# using os.rename, and rsync handles only the actual content changes.
irsync /mnt/data /mnt/data_backup --yes

# Subsequent run with no source changes: the backup drive is never touched.
irsync /mnt/data /mnt/data_backup --yes

# Force a full rsync run even when the snapshot says nothing changed.
irsync /mnt/data /mnt/data_backup --yes --force

# Take a baseline snapshot without backing up (e.g. before a big reorg).
irsync /mnt/data --snapshot-only

# Skip the inode logic entirely and run a plain delete-before rsync.
irsync /mnt/data /mnt/data_backup --no-snapshot
```

Drive-letter shorthand (`G`), `~` for home, `mypython` for the Python source
directory, and `ALL` for every configured drive are also supported.

## Safety

irsync fails closed: when it cannot establish that an endpoint is the drive
you meant, it refuses with exit code 2 instead of proceeding. The motivating
case is a removable drive that isn't mounted — its mountpoint directory still
exists and is empty, which every other check reads as a legitimate empty
tree. Left unchecked, an empty *source* lets `rsync --delete-before` erase
the destination, and irsync would exit 0 and call it a successful backup.

- **The drive is not mounted.** Any endpoint that resolves under the media
  base directory (e.g. `/media/<user>/G`) must sit on a mounted filesystem;
  the base directory itself is exempt, since it is the ordinary directory
  that *contains* mountpoints, not a mountpoint itself. `--require-mount`
  extends this check to endpoints outside the base directory too (the base
  directory stays exempt either way). `--allow-unmounted` proceeds anyway.
- **A first backup would destroy data.** When there is no usable snapshot
  baseline — none exists yet, or an existing one is rejected because it
  belongs to a different source tree — a *local* destination that already
  holds files other than irsync's own snapshot/lock is refused, because
  `rsync --delete-before` would remove them. A remote destination is never
  scanned, so this guard only applies locally. `--allow-nonempty-dest`
  adopts the destination anyway.
- **A run would delete more than half of what's recorded.** Independent of
  the above, if the diff says more than 50% of the previously-recorded
  entries are gone, the run refuses — usually a swapped-source or
  stale-snapshot accident. `--allow-massive-delete` overrides it. `--force`
  does *not* — it only means "run rsync even though the snapshot diff found
  no changes."

| Flag | Effect |
|---|---|
| `--allow-unmounted` | proceed even if the drive is not mounted |
| `--allow-nonempty-dest` | adopt a destination that already holds files, on a baseline-less run |
| `--allow-massive-delete` | proceed when the diff would delete more than half of the recorded entries |
| `--require-mount` | also apply the mount check to endpoints outside the media base directory |
| `--force` | run rsync even when the snapshot diff found no changes (nothing else) |

Each flag disarms exactly one guard, so a cron line cannot silently lose a
protection it did not name. If a refusal fires on a drive you expect to be
attached, check that it is actually mounted before reaching for a flag.

An `ALL` run treats a drive that isn't mounted as a **skip, not an error**: the
configured list names every drive you might ever attach, so missing ones are
expected. Such a run still exits 0, naming both the skipped and the
successfully backed-up drives in its summary. A non-zero exit from `ALL` means
a backup actually failed (1) or you aborted at the prompt (130).

## Project structure

```
src/irsync/
  paths.py          # is_rsync_remote, ensure_local_dir, with_trailing_slash
  snapshot.py       # snapshot_tree, write_snapshot, read_snapshot, SNAPSHOT_FILENAME
  statx.py          # ctypes statx(2) wrapper — inode birth time (btime)
  diff.py           # compute_changes, plan_directory_moves (cycle-safe)
  replay.py         # apply_moves — atomic os.rename on the dest tree
  rsync_runner.py   # build_rsync_command, run_dry_run, run_real_sync
  options.py        # Options dataclass, resolve_endpoints
  backup.py         # the orchestrator: snapshot → diff → replay → rsync → persist
  cli.py            # argparse CLI
  __main__.py       # `python -m irsync` entry point
tests/              # pytest unit + end-to-end tests
```

## Development

This project uses [pixi](https://pixi.sh) for the dev environment:

```bash
pixi run test         # pytest
pixi run lint         # ruff check
pixi run format       # ruff format
pixi run typecheck    # mypy --strict
pixi run pre-commit run --all-files
```

## License

Apache-2.0. See [LICENSE](LICENSE).
