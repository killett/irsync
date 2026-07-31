# Design: mount safety and silent-surprise removal

Date: 2026-07-31
Status: approved (brainstorm), not yet implemented

## Problem

irsync treats a path's existence as proof that the path is the thing the user
meant. On a system where backup drives live under `/media/<user>/`, that
assumption fails whenever a drive is not mounted: the mountpoint directory
still exists (or is trivially recreated), it is empty, and every irsync guard
reads it as a legitimate, empty tree.

Seven hazards follow from that assumption or sit adjacent to it. Each was
reproduced against the current code before being written down here; the
letters are referenced by the rules below and by the implementation plan.

### A — an unmounted source wipes the backup

Reproduced. With an empty source directory, a destination containing real
data, and no prior snapshot, the run logs `No prior snapshot; treating as
first backup`, rsync's `--delete-before` emits `*deleting old_backup.txt`, and
the run finishes with `rsync completed successfully` and exit code 0. The
destination is left empty.

The existing catastrophic-delete guard (`backup.py`, "diff would delete N of M
previously-recorded entries") **cannot** fire in this scenario, and the reason
is structural rather than incidental: the baseline it needs is
`<source>/.irsync_snapshot.jsonl`, which lives on the drive that is not
mounted. An unmounted source takes its own guard with it, so every run against
it looks like a first backup, forever.

An earlier reproduction appeared to show the guard working. It did not: that
test emptied the source of data files while leaving the snapshot file behind,
which is not what an unmounted drive looks like.

### B — an unmounted destination fills the root filesystem

`resolve_endpoints` accepts a destination that does not exist as long as its
parent does. `/media/<user>` always exists, so `/media/<user>/G_backup`
resolves happily whether or not the backup drive is mounted, and rsync writes
the whole source tree onto the root filesystem.

### C — `ALL` multiplies A

`run_all_backups` catches `FileNotFoundError` and skips the drive, so a
*missing* mountpoint directory is handled. An *empty leftover* mountpoint
directory is not: it resolves, passes every check, and takes the first-backup
path — wiping that drive's backup per hazard A.

### D — the first run confirms blind

When there is no usable baseline, `changes is None`, so `_show_preview` is
skipped and the confirmation prompt appears with nothing above it. Under
`--yes` there is not even a prompt. The user is asked to approve a run whose
consequences are unstated — including, in scenario A, the deletion of the
entire backup.

### E — recoverable conditions exit via traceback

Reproduced: a read-only source root raises an uncaught `PermissionError` from
`_source_lock` opening `.irsync.lock`. Separately, a missing `rsync` binary
raises an uncaught `FileNotFoundError` from the subprocess call. Both are
ordinary operational conditions and both currently produce a Python stack
trace with the exit code flattened to 1.

### F — nested mounts are skipped silently

`snapshot_tree` walks with `xdev=True` and rsync runs with
`--one-file-system`, so a filesystem mounted *inside* the source tree is
excluded from both the snapshot and the transfer. Nothing is logged. The
result is a backup that is quietly partial. (Derived from the flags; not
reproduced, because creating a mount requires privileges this environment does
not have.)

### G — `--force` conflates two meanings

`--force` does exactly two things, at `backup.py:392` and `backup.py:398`:

1. Defeats the no-change short-circuit, so rsync runs even when the diff found
   nothing. This is what `--help` advertises, and it is routine enough that a
   user might reasonably put it in a cron line.
2. Disables the catastrophic-delete guard. This is **not** in the help text;
   it is discoverable only from the guard's own error message.

So a user who adds `--force` to a crontab for reason 1 silently disarms the
only protection standing between a bad diff and a wiped backup.

## Design principles

- **Fail closed.** When irsync cannot establish that an endpoint is the drive
  the user meant, it refuses and exits non-zero rather than proceeding.
- **One override per guard.** Every bypass is a separately named flag, so a
  command line that disarms one protection cannot silently disarm another.
- **Redundant coverage of the worst outcome.** Hazard A — destroying a backup
  — is caught by two independent rules, because each has a blind spot the
  other covers.

## Rules

### Rule 1: the mount gate (hazards A, B, C)

For any endpoint at or below `Options.base_dir`, take its first path
component below `base_dir` and require `os.path.ismount()` on that component.

| Endpoint | Component checked |
|---|---|
| `/media/u/G` | `/media/u/G` |
| `/media/u/G_backup` | `/media/u/G_backup` |
| `/media/u/M/python_backup` | `/media/u/M` |
| `/media/u/G/Documents/Programming/python` | `/media/u/G` |
| `/home/u` (outside `base_dir`) | none — exempt |
| `host:/path` (remote) | none — exempt |

The rule is expressed this way, rather than "shorthand endpoints must be
mountpoints", because the shorthand destinations are *subdirectories* of a
mounted drive: `mypython` resolves to a path inside drive `G`, and the `~` and
`mypython` backups land inside drive `M`. Requiring the endpoint itself to be
a mountpoint would reject every one of them.

Endpoints outside `base_dir` are exempt by default; `--require-mount` opts
them in.

**Ordering constraint.** The gate runs in `run_backup` immediately after
`resolve_endpoints` and **before** `_source_lock`. This is load-bearing:
`_source_lock` calls `src_root.mkdir(parents=True, exist_ok=True)` and opens
`.irsync.lock` for writing, so locking an unmounted source creates files on
precisely the disk the gate exists to protect. `_run_snapshot_only` performs
the same check before its own lock.

### Rule 2: the destination gate (hazards A, C)

Fires only when there is no usable baseline — no source snapshot, or one
rejected by the provenance check. If the destination root contains any entry
outside irsync's reserved namespace (`SNAPSHOT_FILENAME`, `LOCKFILE_NAME`,
`SNAPSHOT_TEMPFILE_PREFIX*`), refuse: report the first ten entries and the
total count, and name the override.

Excluding the reserved namespace means a destination holding only a snapshot
file from an interrupted run still counts as empty, so an interrupted first
backup can be retried without an override.

This rule covers explicit-path runs (`irsync /mnt/data /mnt/backup`), which
never touch `base_dir` and are therefore invisible to Rule 1.

### Rule 3: first-run preview (hazard D)

When there is no usable baseline, render a first-run summary through the
existing `_show_preview`, so interactive runs page it and `--yes` runs print it
to stdout for the cron audit trail (AD-7):

```
=== FIRST BACKUP — no prior snapshot ===
Source:      /media/u/G          12,431 entries, 88.2 GiB
Destination: /media/u/G_backup   3 existing entries
rsync will transfer the source in full and DELETE anything at the
destination that is not on the source.
```

Both figures are already available: the entry count and total size come from
`fresh_rows`, held in memory; the destination count comes from the same
top-level `iterdir` Rule 2 performs. No additional tree scan.

### Rule 4: clean refusals (hazard E)

- `preflight.check_rsync_available()` uses `shutil.which("rsync")` and is the
  first check in `run_backup`. `FileNotFoundError` is additionally mapped to a
  logged error inside `run_dry_run` and `run_real_sync`, so the subprocess path
  cannot traceback either.
- `PermissionError` is converted to a named refusal at exactly two sites: the
  `_source_lock` acquisition and the snapshot writes. The narrow scope is
  deliberate — a blanket handler around the orchestrator would mask genuine
  defects.

### Rule 5: nested-mount warning (hazard F)

`snapshot_tree` already evaluates `st.st_dev != root_dev` and skips; it simply
says nothing. Collect the skipped boundaries during the walk and emit one
aggregated warning at the end (count plus the first few paths), not one line
per entry. No signature change, so no caller or test churn.

Known limitation, stated rather than papered over: `--no-snapshot` runs never
walk the tree and so produce no warning, even though rsync's
`--one-file-system` skips the same boundaries.

### Rule 6: split `--force` (hazard G)

`--force` reverts to its single documented meaning: run rsync even when the
snapshot shows no changes. Bypassing the catastrophic-delete guard moves to
`--allow-massive-delete`. Clean split, no compatibility alias — the guard's
error message names the new flag, and no existing automation depends on the
old behaviour.

## Flags

| Flag | Disarms |
|---|---|
| `--allow-unmounted` | Rule 1, the mount gate |
| `--allow-nonempty-dest` | Rule 2, the destination gate |
| `--allow-massive-delete` | the catastrophic-delete guard (was `--force`) |
| `--require-mount` | *opts in* to Rule 1 outside `base_dir` |
| `--force` | the no-change short-circuit only |

All refusals return the existing `EXIT_REFUSED` (2). No new exit codes.

## Components

New module `src/irsync/preflight.py`, keeping the checks unit-testable without
running a backup and keeping them out of `_run_backup_for_endpoints`, already
recorded in `docs/hygiene-notes.md` as over-long:

```python
class EndpointNotMounted(Exception):   # skippable — ALL treats it as a missing drive
class UnsafeDestination(Exception):    # hard refusal — never auto-skipped

def mount_gate_root(path: Path, base_dir: Path) -> Path | None
def check_mounted(
    endpoint: Path | str, base_dir: Path, *, gate_outside_base: bool
) -> None
def foreign_dest_entries(dest_root: Path) -> list[str]
def check_rsync_available() -> None
```

`check_mounted` accepts `Path | str` because `resolve_endpoints` returns a
plain string for remote endpoints; a `str` endpoint is remote and is always
exempt, returning without a check. `gate_outside_base` carries the
`--require-mount` opt-in: when False (the default) an endpoint outside
`base_dir` is exempt; when True it must be a mountpoint in its own right,
since there is no `base_dir` component to walk up to.

Two exception types, because `ALL` must distinguish them: `run_all_backups`
catches `EndpointNotMounted` alongside `FileNotFoundError` and records the
drive as missing/skipped, preserving the exit-code contract documented in
commit `044c036` (a drive that is not mounted is a skip, not an error).
`UnsafeDestination` is never swallowed and counts as a real error.

## Data flow

```
resolve_endpoints
  → check_rsync_available
  → check_mounted(source)          # Rule 1, before any file is created
  → check_mounted(dest)            # Rule 1
  → _source_lock                   # may raise PermissionError → refusal
  → snapshot_tree                  # may warn about nested mounts (Rule 5)
  → read baseline / compute diff
  → if no baseline: foreign_dest_entries → Rule 2 gate, then Rule 3 preview
  → confirm → replay → rsync → persist
```

## Testing

- **`preflight` units.** Path math for `mount_gate_root`: under `base_dir`,
  exactly `base_dir`, outside it, remote strings. `check_mounted` with
  `os.path.ismount` monkeypatched — a true OS boundary, so patching it is
  appropriate rather than over-mocking.
- **Hazard A, end to end.** Empty source, no snapshot, destination holding a
  real file: assert the run refuses *and* that the file still exists
  afterward. This test fails on today's code, where the file is deleted.
- **`ALL` semantics.** `EndpointNotMounted` counts as missing and keeps exit
  0; `UnsafeDestination` counts as an error and yields exit 1.
- **Rule 6.** `--force` alone still refuses on a >50% deletion diff;
  `--allow-massive-delete` proceeds.
- **Rule 3.** The first-run summary reaches stdout under `--yes` and states
  both the entry count and the deletion consequence.
- **Rule 4.** `shutil.which` returning `None` yields `EXIT_REFUSED`; a
  read-only source root yields `EXIT_REFUSED` and no traceback.
- **Rule 5.** The boundary decision is extracted into a small pure helper and
  tested directly.

**Coverage gap, stated explicitly:** hazard F has no end-to-end test. Creating
a nested mount requires root, which neither the test suite nor CI has. The
helper is tested; the integration is not. Writing a test that mocks its way to
a green result would assert nothing about the real behaviour.

## Out of scope

Deliberate omissions, recorded so a later pass does not re-litigate them:

- Filesystem-UUID pinning. Would catch a *different* disk mounted at the right
  path, which the mount gate cannot. Rejected for now as heavier than the
  problem warrants; the snapshot header already tolerates new fields, so this
  is the natural follow-on if the mount gate proves insufficient.
- Free-space and device-sanity heuristics.
- Per-drive configuration of expectations (e.g. "G must always be mounted").
- A destination-side lockfile.
