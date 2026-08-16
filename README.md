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
your `PATH`. Its only Python dependency is
[`drivecfg`](https://github.com/killett/drivecfg), which supplies the optional
drive shorthands described under [Configuration](#configuration).

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

Everything above works on plain paths and needs no configuration at all.

## Configuration

irsync can also take shorthands — a drive id, a named endpoint such as `~` or
`mypython`, or `ALL` for a whole-machine run — instead of a source and
destination. Those names mean nothing on their own, so they are read from a
[`drivecfg`](https://github.com/killett/drivecfg) config file. irsync loads it
lazily: a run on plain paths never looks for one, and a run that uses a
shorthand without one refuses with exit code 2, naming every location it
tried.

The file is discovered from `--config PATH`, then `$DRIVECFG_CONFIG`, then
`$XDG_CONFIG_HOME/drivecfg/drives.toml` (usually
`~/.config/drivecfg/drives.toml`). A short example, with invented drive ids:

```toml
schema_version = 1
base_dir = "/media/alice"

drives = [
  { id = "A", enclosure = "desk-dock" },
  { id = "B", dir = "photos", backup_dir = "photos_backup" },
  { id = "C", backup = false },
]

# What `irsync ALL` runs, in order. "*drives" expands to every drive
# with backup = true, in file order.
backup_order = ["mypython", "*drives", "~"]

[endpoints."~"]
source = { path = "~" }
dest = { drive = "A", path = "home_backup" }

[endpoints.mypython]
source = { drive = "B", path = "projects/python" }
dest = { drive = "A", path = "python_backup" }
```

With that file in place:

```bash
irsync A            # /media/alice/A  ->  /media/alice/A_backup
irsync B            # /media/alice/photos  ->  /media/alice/photos_backup
irsync mypython     # the configured source/destination pair
irsync ALL --yes    # every entry of backup_order, in order
```

A drive id that the config does not define is refused with exit code 2 and a
message naming the drives that *are* configured — irsync never guesses a path
for an unknown name. `base_dir` is used only as the mount-gate root (see
[Safety](#safety)); when no config is loaded it defaults to
`/media/<your-username>`, which can refuse a run but can never redirect one.

## Safety

irsync fails closed: when it cannot establish that an endpoint is the drive
you meant, it refuses with exit code 2 instead of proceeding. The motivating
case is a removable drive that isn't mounted — its mountpoint directory still
exists and is empty, which every other check reads as a legitimate empty
tree. Left unchecked, an empty *source* lets `rsync --delete-before` erase
the destination, and irsync would exit 0 and call it a successful backup.

- **The drive is not mounted.** Any endpoint that resolves under the media
  base directory (e.g. `/media/<user>/A`) must sit on a mounted filesystem;
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
- **A first backup would come from an empty source.** Also gated on there
  being no usable baseline: a source with no entries besides irsync's own
  reserved files is never a legitimate backup — it is the signature of an
  unmounted or mistyped source — so the run refuses regardless of what the
  destination holds. Firing only when there is no baseline keeps this
  independent of the >50% deletion guard below: a genuine "I deleted
  everything" run, with a baseline in place, is handled by that guard
  instead. `--allow-empty-source` proceeds anyway.
- **A run would delete more than half of what's recorded.** Independent of
  the above, if the diff says more than 50% of the previously-recorded
  entries are gone, the run refuses — usually a swapped-source or
  stale-snapshot accident. `--allow-massive-delete` overrides it. `--force`
  does *not* — it only means "run rsync even though the snapshot diff found
  no changes."
- **`--dry-run` changes nothing.** Honored on both the normal (snapshot)
  path and `--no-snapshot`: irsync shows what rsync would do and exits 0
  without touching the destination or persisting a snapshot, on either
  path. Note that the empty-source refusal above is deliberately **not**
  `--dry-run`-exempt (a preview of an empty source is a wall of deletions,
  which conveys less than the refusal message does), so it still fires
  first even under `--dry-run`; the nonempty-destination refusal, by
  contrast, is exempt and merely previews.

| Flag | Effect |
|---|---|
| `--allow-unmounted` | proceed even if the drive is not mounted |
| `--allow-nonempty-dest` | adopt a destination that already holds files, on a baseline-less run |
| `--allow-empty-source` | proceed with a baseline-less run even though the source is empty |
| `--allow-massive-delete` | proceed when the diff would delete more than half of the recorded entries |
| `--require-mount` | also apply the mount check to endpoints outside the media base directory |
| `--force` | run rsync even when the snapshot diff found no changes (nothing else) |

Each flag disarms exactly one guard, so a cron line cannot silently lose a
protection it did not name. If a refusal fires on a drive you expect to be
attached, check that it is actually mounted before reaching for a flag.

An `ALL` run treats an unmounted **source** as a **skip, not an error**: the
configured list names every drive you might ever attach, so missing ones are
expected. An unmounted **destination** is different and counts as a real
**error**: if the source drive is mounted but its backup drive is not, the
run would back up nothing while still looking like success, which is exactly
what this guard exists to prevent. Either way the batch continues to the next
drive rather than stopping. A run with only skips still exits 0, naming both
the skipped and the successfully backed-up drives in its summary; a run with
at least one unmounted destination (or any other real error) exits 1. A
non-zero exit from `ALL` means a backup actually failed (1) or you aborted at
the prompt (130).

## Project structure

```
src/irsync/
  paths.py          # is_rsync_remote, ensure_local_dir, with_trailing_slash
  snapshot.py       # snapshot_tree, write_snapshot, read_snapshot, SNAPSHOT_FILENAME
  statx.py          # ctypes statx(2) wrapper — inode birth time (btime)
  preflight.py      # mount / empty-source / nonempty-dest / rsync-present guards
  diff.py           # compute_changes, plan_directory_moves (cycle-safe)
  replay.py         # apply_moves — atomic os.rename on the dest tree
  rsync_runner.py   # build_rsync_command, run_dry_run, run_real_sync
  options.py        # Options dataclass, drivecfg-backed resolve_endpoints/resolve_source
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

While `drivecfg` is not yet on PyPI, the dev environment picks it up from a
sibling checkout (`../drivecfg/src`) via the `PYTHONPATH` entries in
`pixi.toml` and `pyproject.toml`. Both are marked temporary and should be
removed once `drivecfg` can be installed as a normal dependency.

## License

Apache-2.0. See [LICENSE](LICENSE).
