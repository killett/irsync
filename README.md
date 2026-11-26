# irsync

Rename-aware Python wrapper around `rsync`. When files are moved or renamed on
the source drive, the original `rsync` deletes the old path on the backup drive
and re-transfers the (often huge) file from scratch. `irsync` avoids that by
keeping an inode snapshot at the root of the source tree, comparing it against
a fresh snapshot on each run, and replaying just the moves/renames atomically
on the backup tree before invoking `rsync` for the actual content delta. The
replay needs a local backup: on a remote destination (`host:/path`) a moved
file is deleted and sent again, and the preview says so. When no source
changes are detected at all, the backup drive is never accessed.

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

# Also back up what is normally skipped (.Trash-*, .cache,
# unfinished_downloads). This always runs rsync, changed or not.
irsync /mnt/data /mnt/data_backup --no-exclude

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
lazily, and how lazily depends on the invocation:

- **`irsync SOURCE DEST`**, two plain paths, never looks for one. This is the
  only shape that skips discovery entirely.
- **A shorthand** (a drive id, an endpoint name, or `ALL`) always looks for
  one, whether or not a destination was also given, and refuses with exit
  code 2 if none is found, naming every location it tried.
- **A single path with no destination** also always looks for one, even
  though it might turn out to be an ordinary directory rather than a
  configured name — irsync has to check before it can tell the difference —
  and refuses the same way if none is found.
- **`--snapshot-only`** is the one case that looks without requiring: it
  consults a config if one exists (so a configured name still resolves), but
  a missing file is not an error there — it falls back to treating the
  argument as a literal path.

Naming a config explicitly, with `--config` or `$DRIVECFG_CONFIG`, always
triggers a load attempt and always refuses if that specific file is missing,
regardless of which of the above shapes it's attached to.

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

### Previewing drives with a dry run

To see what a backup of some drives would do before running it, give each
drive its own `--dry-run`, one after another, each into its own file:

```bash
for d in A B; do
  irsync $d --dry-run --yes \
    > ~/irsync-dry-run-$d.txt 2>&1
done
```

Then show the results drive by drive:

```bash
for d in A B; do
  echo "===== $d ====="
  grep -A11 'FIRST BACKUP' ~/irsync-dry-run-$d.txt
  grep -e 'Number of deleted' \
    -e 'files transferred' \
    -e 'Total transferred' \
    ~/irsync-dry-run-$d.txt
done
```

For a drive with no snapshot yet, the first `grep` shows the first-backup
summary; its "not on the source (a real run DELETES these)" line names what
a real run would remove from the backup. The second shows rsync's totals:
files it would delete, files it would copy, and bytes. A dry run copies,
deletes, and renames nothing and saves no snapshot; the only thing it writes
is the empty `.irsync.lock` at the top of each source. A drive that is not
plugged in gets an error in its file instead and is left untouched: "does
not exist" when its folder under the base directory is gone, or "probably
not mounted" when the folder is there but no drive is mounted on it.

To see every file and folder a real run would delete from each backup,
drive by drive (rsync marks each one `*deleting`, a folder with a trailing
`/`; press `q` to leave `less`):

```bash
for d in A B; do
  echo "===== $d ====="
  grep '^\*deleting' ~/irsync-dry-run-$d.txt
done | less
```

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
- **The snapshot is corrupt.** A snapshot file with a line that cannot be
  used — cut off mid-write or garbled, not a JSON object, a row missing a
  field, an unknown entry type, a value of the wrong kind (such as text
  where a number belongs), or a path irsync could not have written (an
  absolute path, or one using `..`) — is never partially trusted: the run
  refuses with exit code 2 and names the bad line. The path check matters
  most, since replaying a move from such a path would reach outside the
  backup. A snapshot with no entries at all (empty, blank, or only its
  header line) is refused the same way: irsync never writes one, and read
  as an empty baseline it would slip past both first-backup checks and the
  more-than-half check above. So is a snapshot path that holds a folder, or
  anything else that is not a regular file. An `ALL` run counts such a
  drive as one error and carries on with the rest (an `ALL` run stops once
  five drives have failed); any other unexpected error on one drive is
  logged with its traceback and counted the same way. The file is left in
  place. To recover, move it out of the source tree; the next run is then a
  first backup, which needs `--allow-nonempty-dest` if the destination
  already holds a backup.
- **The backup has to be looked at, and cannot be read.** For the names it
  normally skips (`.Trash-*`, `.cache`, `unfinished_downloads`), irsync does
  not assume the backup holds what the source had. Before listing changes it
  looks at the backup there (file metadata only, never contents): a file is
  listed as a move only when the backup holds it with the same size and the
  same modification time to the second, and is otherwise listed as created
  and copied (under `--no-exclude`, a stale copy at the old path is also
  listed as deleted). If that look fails for any reason other than the file
  not being there (permission denied, an I/O error), the run refuses with
  exit code 2 before writing anything.
- **A remote destination gets no renames.** Moves are replayed only on a
  local destination. With `host:/path` as the destination, anything moved or
  renamed is listed as deleted and created, because rsync deletes it there
  and sends it again, and a note in the preview says so. Those entries count
  toward the more-than-half check above, so a large rename to a remote
  destination is refused until you pass `--allow-massive-delete`.
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
  display.py        # display_name — file names made safe to print in previews
  snapshot.py       # snapshot_tree, write_snapshot, read_snapshot, SNAPSHOT_FILENAME
  statx.py          # ctypes statx(2) wrapper — inode birth time (btime)
  preflight.py      # mount / empty-source / nonempty-dest / rsync-present guards
  diff.py           # compute_changes, plan_directory_moves (cycle-safe)
  replay.py         # apply_moves — atomic os.rename on the dest tree
  destination.py    # Destination, LocalDestination — look at the backup, rename on it
  baseline.py       # what a baseline may claim about the backup under excluded names
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

### `drivecfg` on PyPI

`pyproject.toml` declares `drivecfg>=0.1,<1` as a real runtime dependency.
`drivecfg` [0.1.0 is published on PyPI](https://pypi.org/project/drivecfg/0.1.0/),
so this resolves normally: CI's `pip install .[dev]` and the dev environment's
`pixi install` both install the real package. The formerly-required ordering
("publish drivecfg before pushing or tagging irsync") is satisfied and no
longer something a release needs to check.

The dev environment used to sidestep the missing package with a temporary
`PYTHONPATH` shim pointing at a sibling `../drivecfg/src` checkout, wired into
`pixi.toml` and `pyproject.toml`. That shim has been removed now that
`drivecfg` installs normally; `pixi.toml`'s `[pypi-exclude-newer]` carries a
scoped override so the workspace's cooldown doesn't block picking up
first-party releases the same day they're published.

## License

Apache-2.0. See [LICENSE](LICENSE).
