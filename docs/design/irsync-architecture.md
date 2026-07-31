# irsync — architecture & design

This is the durable architecture-and-design reference for `irsync`. It stands
alone as the canonical description of the design.

## Overview

`irsync` is a rename-aware Python wrapper around `rsync`, living at
`src/irsync/`. It exists to solve a specific pain point with the user's previous
tool, `srsync` (a thin `rsync --delete-before` wrapper): when files were renamed or moved
on the source drive, plain rsync would **delete-and-retransfer** the data
instead of just renaming the file on the destination. On multi-TB drives that is
brutally expensive.

`irsync` adds an **inode-snapshot diff layer** in front of rsync. On each run it
snapshots the source tree's per-inode metadata
`(dev, ino, type, nlink, size, mtime_ns, btime_ns, path)`, diffs that against the
previous snapshot stored at `<src>/.irsync_snapshot.jsonl`, and **replays the
detected moves on the dest tree using `os.rename` before invoking rsync**. rsync
then only has to move new/changed bytes, not re-copy renamed files.

## Algorithm / data flow

A single run proceeds as follows:

1. **Snapshot the source tree.** `snapshot_tree` walks the source (iterative,
   `lstat`-based, cross-device aware) and records one `Row` per inode.
2. **Diff against the baseline.** The new snapshot is diffed against
   `<src>/.irsync_snapshot.jsonl` (the previous snapshot) via `compute_changes`,
   producing a `Changes` dataclass of dir moves, file moves, modifications,
   creations, and deletions.
3. **Replay detected moves on the dest.** `apply_moves` performs the dir/file
   renames on the dest tree with `os.rename` (no-clobber guard, cross-device
   pre-flight refusal) **before** rsync runs, so rsync sees files already in
   their new locations.
4. **Invoke rsync.** rsync transfers the remaining new/changed content, with the
   three reserved-namespace files anchored-excluded.
5. **Persist a fresh snapshot.** After a successful rsync, the new snapshot is
   written to `<dest>/.irsync_snapshot.jsonl` **first**, then to
   `<src>/.irsync_snapshot.jsonl` (dest-first ordering — see AD-15).

**No-change runs never touch the backup drive.** If the diff detects no source
changes at all, the run short-circuits before any dest access. This invariant is
AD-2, and AD-22 closed a pass-10 hole (early dest tempfile cleanup) that was
secretly waking the backup drive on every run.

## Package layout

### `src/irsync/`

| Module | Responsibility |
|---|---|
| `__init__.py` | `__version__ = "0.1.0"` |
| `__main__.py` | `python -m irsync` entry; calls `cli.main()` |
| `cli.py` | argparse; mirrors srsync's flags plus `--force`, `--no-snapshot`, `--snapshot-only`, `--dry-run`; rejects `--force` + `--no-snapshot` at parse time |
| `options.py` | `Options` dataclass (drive-letter map, `base_dir`, `exclude_dirs`); `resolve_endpoints` maps raw CLI args to `(src, dest, remote flags)` |
| `paths.py` | `is_rsync_remote`, `ensure_local_dir`, `with_trailing_slash` |
| `snapshot.py` | `snapshot_tree` (iterative walk, `lstat`-based, xdev-aware); `Row` TypedDict `(dev, ino, type, nlink, size, mtime_ns, btime_ns, path)`; `write_snapshot`/`read_snapshot` (provenance header + type validation, streaming read, `surrogateescape` encoding); legacy `write_jsonl`/`read_jsonl` (no header, same validator); shared `_validate_row` helper; reserved-namespace constants `SNAPSHOT_FILENAME = ".irsync_snapshot.jsonl"`, `LOCKFILE_NAME = ".irsync.lock"`, `SNAPSHOT_TEMPFILE_PREFIX = ".irsync-snap-"` |
| `statx.py` | ctypes wrapper around Linux `statx(2)` for `btime_ns`; returns `-1` when unavailable (non-Linux, no statx, FS without btime, dangling path) so the caller falls back to the size+mtime gate |
| `diff.py` | `NodeInfo`, `index_by_inode`, `compute_moves` (returns `(dir_moves, file_moves, consumed_keys)`), `prune_redundant_dir_moves`, `plan_directory_moves` (cycle-safe via random temp suffix), `Changes` dataclass `(dir_moves, file_moves, modified, created, deleted)`, `compute_changes` (public API). Move gate: require size + `mtime_ns` match; if both sides have `btime_ns >= 0`, also require `btime_ns` match. `created`/`deleted` filter includes types `l` and `o`; type-change at same path is emitted as both deleted+created |
| `replay.py` | `apply_moves(dir_moves, file_moves, dest_root) -> ReplayResult`; pure `os.rename`, no-clobber guard, refuses EXDEV; `CrossDeviceMoveError` + `_preflight_check_xdev` stats every planned src/dst path against `dest_root.st_dev` before any rename runs |
| `rsync_runner.py` | `build_rsync_command` (anchored excludes for the three reserved-namespace files), `parse_rsync_output`, `run_dry_run`, `run_real_sync` (streams stderr live; `start_new_session=True`; routes cancellation through `os.killpg(getpgid(pid), SIGTERM/SIGKILL)` so ssh children die with rsync; installs a scoped SIGTERM handler that raises `KeyboardInterrupt` so cleanup fires on cron timeout) |
| `backup.py` | Orchestrator: `run_backup` resolves endpoints, acquires `_source_lock`, dispatches `_run_backup_for_endpoints` / `_run_snapshot_only` / `_run_rsync_only`. Also `_atomic_write_snapshot` (fsyncs tempfile before `os.replace`), `_persist_snapshots` (dest first, then src — AD-15), `_format_preview`, `_show_preview`, `_confirm_or_abort`, `_cleanup_orphan_tempfiles` (scans both src and dest; dest call deferred until `apply_moves` — AD-22), `_page_output` (`less` with `start_new_session=True`), `run_all_backups`. Catches `CrossDeviceMoveError` around `apply_moves`; `_run_rsync_only` always runs the dry-run so `--no-snapshot --yes` prints to stdout for cron auditability |

### `tests/`

| File | Contents |
|---|---|
| `conftest.py` | `make_tree`, `basic_options`, `tree_signature`, `inode_for` |
| `test_paths.py` | path-helper coverage |
| `test_snapshot.py` | snapshot round-trip (incl. invalid-type rejection, non-UTF-8 round-trip, streaming read, depth-1500 walk) |
| `test_diff.py` | diff/move detection (incl. symlink add/delete/replace, NFS-sim via real `snapshot_tree` + forged row, real rename, type-"o" add/delete, type-change at same path, real-syscall kernel inode reuse — skips where btime collapses to mtime) |
| `test_replay.py` | `apply_moves` behavior |
| `test_rsync_runner.py` | rsync invocation + subprocess isolation (incl. `start_new_session`, KeyboardInterrupt-during-stream, killpg-on-cancel, SIGTERM handler installed/restored/raises) |
| `test_options.py` | endpoint resolution + drive-letter mapping |
| `test_cli.py` | smoke tests (incl. parse-time rejection of `--force --no-snapshot`) |
| `test_backup.py` | orchestrator coverage (incl. persist ordering, dest orphan cleanup, pager session detachment, dest-untouched-on-no-change, fsync-before-replace ordering) |
| `test_integration.py` | 4 end-to-end tests: umbrella exercising every reproducible bug from passes 1-7 + symlink add/delete + 8 random fuzz moves; cross-device refusal via real preflight; isolated H1 symlink add/delete regression net; `--no-snapshot --yes` audit-trail invariant. Pickers use the 10-attempt no-op retry loops from `make_random_moves.py` |

Total: 168 tests (as of the pass-10 migration; 167 pass, 1 conditionally skipped
— the kernel inode-reuse btime test that skips where btime collapses to mtime,
e.g. tmpfs).

## Architectural decisions

The AD numbers below are **stable anchors** referenced throughout the project's
history; they are preserved exactly.

### 1. Unified replacement for srsync, not a wrapper or pre-step

irsync subsumes srsync entirely; it is a standalone package with no dependency
on the original `srsync` script. Everything the tool needs lives under
`src/irsync/`.

### 2. Source-side snapshot is the source of truth; dest-side is a recovery aid

The diff baseline is always `<src>/.irsync_snapshot.jsonl`. When a backup
completes successfully a copy is written to `<dest>/.irsync_snapshot.jsonl` too,
but that copy is never read by the algorithm. It exists so that if the source
drive dies and the user restores from backup, they get the snapshot back
automatically. Rationale: this lets the cheap "anything changed?" check work
without spinning up the backup drive. See AD-22 for the pass-10 closure of a
hole that was secretly violating this invariant.

### 3. Inode-based move detection only; no content hashing

An inode is treated as a "move" only when:

- It has exactly one path on each side (no hardlinks).
- The paths differ.
- `size` matches **and** `mtime_ns` matches (defends against inode reuse).
- If both sides have `btime_ns >= 0`, `btime_ns` must also match (the NFS
  inode-reuse defense, pass 6).
- It is not a symlink (default; `include_symlinks=False`).
- The type did not change between snapshots.

Rejected alternative: content hashing (rclone-style `--track-renames`). Hashing
every file on multi-TB drives defeats the whole optimization. Inode + size +
mtime + btime is the right balance for the user's use case (single drive per
backup pair, ext4/btrfs).

### 4. Provenance header on snapshots

Every snapshot file starts with a JSONL header line:

```json
{"_meta": {"source_root": "/abs/resolved/path", "irsync_version": "0.1.0", "created_at_utc": "..."}}
```

On read, `read_snapshot` validates that `source_root` matches the current source
root. If it does not (user copied a snapshot from another tree, restored from
elsewhere, or mounted the drive at a different point), `SnapshotMismatch` is
raised and the orchestrator treats the run as a first backup — a line of defense
against using the wrong baseline. `read_snapshot`/`read_jsonl` also validate that
each row's `type` is one of `("f", "d", "l", "o")`; a corrupted row with an
unknown type hard-fails the read instead of being silently dropped. Validation
lives in a shared `_validate_row(obj, file, lineno)` helper used by both readers,
which stream line by line and report **actual file line numbers** so
`sed -n <N>p <file>` lands on the bad row.

### 5. Reserved-namespace pattern at the source root

Three filenames at the root of the source tree are owned by irsync and excluded
from both `snapshot_tree` and rsync transfer. **All three excludes are anchored
with a leading `/`** so subdirectory files that happen to share these names ARE
backed up:

| File / pattern | Lives where | Excluded from |
|---|---|---|
| `.irsync_snapshot.jsonl` | source root **and** dest root | `snapshot_tree` (root only); rsync (anchored) |
| `.irsync.lock` | source root only | `snapshot_tree` (root only); rsync (anchored) |
| `.irsync-snap-*` | source root **and** dest root (orphan tempfiles; dest added in pass 8) | `snapshot_tree` (root only); rsync (anchored); also actively cleaned up at startup at both roots, with **dest cleanup deferred per AD-22** |

This pattern is critical: **if you add another irsync internal file at the
source root, it MUST be added to BOTH `snapshot.py` (the walk-time skip) AND
`rsync_runner.py` (the rsync exclude), with the leading-slash anchor.** Don't
repeat the H1 mistake. (Bug IDs like H1 / NEW-H3 / F-numbers refer to fixes
catalogued in Appendix A.)

### 6. Sanity-threshold for catastrophic diffs

`backup.py` refuses to proceed when `len(deleted) / len(before_rows) > 0.5`
unless `--force`. This is a last line of defense against snapshot mismatches
that slipped past provenance checks, swapped source/dest arguments, or partial
source-tree availability (e.g., a network mount went stale). The threshold is on
`deleted` only — not `modified` or `created` — because deletions are the
irreversible operation.

### 7. Cron-friendly: --yes preserves preview as stdout

`--yes` skips the confirmation prompt **and** switches `_show_preview` from
"page through `less`" to "print to stdout". A cron job running `irsync --yes`
therefore gets the full audit trail (every move, modification, creation,
deletion path) in its log without needing a TTY. The `--no-snapshot` path was
fixed in pass 5 to honor this too. This behavior is load-bearing — don't break
it.

### 8. Lockfile uses fcntl.flock, not file existence

The lock at `<src>/.irsync.lock` is held via
`fcntl.flock(fd, LOCK_EX | LOCK_NB)`. The file itself persists between runs (a
0-byte sentinel); the lock is on the open fd. It is non-blocking by design — a
contended run exits with code 75 (`EX_TEMPFAIL`) immediately rather than
waiting. SIGKILL releases the lock automatically (the kernel closes the fd). The
lock is robust against symlink/hardlink/bind-mount indirection because Linux
flock conflict checks happen at the **inode** level, not the path level: two
processes opening the same file via different paths contend on the same lock
correctly.

### 9. apply_moves is in-process, not a generated script

`irsync.replay.apply_moves` does the renames directly in the same Python
process, atomically via `os.rename`. A **pre-flight check refuses cross-device
moves upfront**: it walks every planned source/dest, stats each against
`dest_root.st_dev`, and raises `CrossDeviceMoveError` before any rename runs.
`backup.py` catches this and returns 2 without persisting the snapshot, so a
corrected re-run still has the right baseline. `apply_moves` skips a move when
the destination already exists, so partially-applied replays from a killed
previous run are idempotent.

### 10. Cycle-breaking temp names use random suffixes

`plan_directory_moves` generates `<dir>.__mvtmp__<8-hex-chars>` for cycle
intermediaries (e.g., swapping two directory names). The hex suffix comes from
`secrets.token_hex(4)`, so collisions with real files at the dest are
vanishingly unlikely. `--delete-before` sweeps any cycle-temp stragglers from
prior killed runs (they exist on dest only, never on src).

### 11. Changes dataclass and the any_changes() invariant

`Changes` has five lists: `dir_moves`, `file_moves`, `modified`, `created`,
`deleted`. The orchestrator's "no changes since last backup" short-circuit
checks `changes.any_changes()`. **Critical invariant:** if any change to the
source tree's content or layout occurred, at least one of those five lists must
be non-empty. Multiple review passes have uncovered ways this invariant was
violated; the current code holds it. The path through is: `created`/`deleted`
accept inode types in `{d, f, l, o}` — every type `snapshot_tree` records — and
the type-change defensive branch in `compute_changes` adds the path to both
lists when an inode's type differs between snapshots. If you add a new kind of
change-detection (e.g., permission changes), it MUST plumb through to
`any_changes()` or backups will silently get skipped.

### 12. compute_moves returns consumed_keys

`compute_moves` returns `(dir_moves, file_moves, consumed_keys)`.
`compute_changes` uses `consumed_keys` to identify shared inodes that are NOT
moves and to reflect their path-set differences in `deleted`/`created`/
`modified`. This closes the rename+edit invisibility hole. If you refactor
`compute_moves`, you must keep returning a set of consumed inode keys or the
rename+edit defense (NEW-H3) will regress.

### 13. btime via statx, not ctime, for the inode-reuse tiebreaker

The pass-5 inode-reuse tiebreaker used `ctime_ns`. It was wrong on both ends:
Linux's `rename(2)` updates ctime on both same-dir and cross-dir renames, so the
gate refused legitimate renames whenever the wall clock had advanced; and on a
truly second-granular FS, ctime is also second-granular and would collide for
the false-move case anyway. The test only passed by luck — its rename happened
within the same nanosecond clock tick.

Pass 6 replaced ctime with `btime` (statx's `stx_btime`, "birth time"). btime is
set when the inode is allocated and never updated by rename or write, so a real
rename preserves it while an inode-reuse event always gets a fresh value. The
gate becomes: require size + `mtime_ns` match; **if both sides have
`btime_ns >= 0`, also require `btime_ns` match**. When btime is unavailable
(statx missing, non-Linux, FS without btime, legacy snapshot), the gate falls
back to size+mtime alone — correct for nanosecond-precision local FS. Note:
tmpfs reports btime equal to mtime, defeating the gate's distinction in some CI
environments; the pass-8 real-syscall reuse test detects this and skips.
`src/irsync/statx.py` is a small ctypes wrapper around `libc.statx` (no new
dependencies) that returns `-1` when btime is unknown.

### 14. EXDEV pre-flight in apply_moves

Originally `os.rename` raised `OSError(EXDEV)` mid-flight if the dest spanned a
mount boundary, escaping uncaught past the snapshot-persist step. The next run
re-issued the same plan and hit the same EXDEV at the same point — a perpetual
retry loop. The pre-flight in `_preflight_check_xdev` walks all planned
source/dest paths against `dest_root.st_dev` before any rename runs and raises
`CrossDeviceMoveError` (an `OSError` subclass) if any would cross. `backup.py`
catches it, logs a clear message, returns code 2, and intentionally does NOT
persist the snapshot, so a re-run with corrected dest config still uses the
right baseline.

### 15. Snapshot persist order: dest first, src second

`_persist_snapshots` writes the **dest snapshot first**, then the source
snapshot. Reasoning: if the dest write fails, src stays at the old baseline and
the next run reconciles cleanly. If the src write fails after dest succeeded, the
dest has the new snapshot but it is never read in the normal flow — only on DR
restore, where having the newer snapshot on the restore-source is the safer
asymmetry. Gotcha for future fixes: because dest is written first, a kill
mid-dest-write strands a `.irsync-snap-*` tempfile at dest root, so
`_cleanup_orphan_tempfiles` MUST run for both src and dest (rsync's anchored
exclude prevents both transfer and `--delete-before` sweep, so without explicit
cleanup the orphan accumulates forever).

### 16. rsync subprocess isolation

`run_real_sync` runs rsync in its own session/pgrp via `start_new_session=True`.
On any exception leaving the function — `KeyboardInterrupt`, `SystemExit`, or
the SIGTERM-as-`KeyboardInterrupt` the handler raises — the cleanup path:

1. Calls `os.killpg(os.getpgid(proc.pid), signal.SIGTERM)` so the **whole
   process group** dies, including any ssh subprocess rsync forked for remote
   endpoints (plain `proc.terminate()` only signals the rsync leader).
2. Waits up to 5 seconds, then escalates with `os.killpg(pgid, signal.SIGKILL)`
   and waits another 2 seconds.
3. Best-effort: `ProcessLookupError` / `OSError` on already-exited children is
   suppressed.

On entry, `run_real_sync` also installs a SIGTERM handler that raises
`KeyboardInterrupt`. Without this, Python's default SIGTERM handler kills the
process without raising and the cleanup never runs — exactly what left the
earlier isolation incomplete on cron timeout / `systemctl stop`. The handler is
restored on exit via try/finally, and installation is skipped on non-main
threads (`signal.signal` would raise `ValueError` there — defensive).

### 17. Pager subprocess detachment

`_page_output`'s `less` subprocess is also spawned with `start_new_session=True`
for parity with rsync. Same reasoning: a parent SIGKILL while paging shouldn't
leave `less` zombied on the controlling tty.

### 18. Type filter consistency in compute_changes

The `created` and `deleted` comprehensions in `compute_changes` accept every
type that `snapshot_tree` records: `{d, f, l, o}`. Without `l` (closed in pass
7), an isolated symlink add / delete / replace silently short-circuited the
backup. Without `o` (closed in pass 8), the same silent-skip pattern was latent
for sockets / FIFOs / devices when `include_other=True`. **Rule:** any type
added to `snapshot_tree`'s output must also be in `compute_changes`' filter.

### 19. Type-change defensive branch

When the same inode key has a different `type` in before vs after (physically
impossible in real life — changing inode type requires unlink+creat, which gets
a new inode — but the code has a defensive `continue` in `compute_moves`),
`compute_changes` now adds the shared path to **both** `deleted` and `created`
so rsync re-syncs it. Previously the modification gate was skipped whenever
either side was a directory, leaving the path silently invisible.

### 20. surrogateescape for arbitrary-byte filenames

Linux filenames are arbitrary byte sequences (any byte except `/` and `\0`).
`os.listdir` / `Path.iterdir` surface non-UTF-8 names as **surrogate-escaped**
Python strs (codepoints `\udc80–\udcff`). Without `errors="surrogateescape"` on
the writer's `open()`, `f.write(...)` of any path containing such a surrogate
raises `UnicodeEncodeError` mid-snapshot — the user can't even baseline a tree
containing arbitrary-byte names. All four file `open()`s in `snapshot.py`
(`write_jsonl`, `write_snapshot`, `read_snapshot`, `read_jsonl`) now pass
`errors="surrogateescape"`, round-tripping arbitrary bytes losslessly.
`json.dumps`/`json.loads` operate at the str level and accept lone surrogates.
The provenance header's `source_root` field goes through the same encoder, so
non-UTF-8 source-root paths also round-trip.

### 21. fsync before os.replace in atomic snapshot writes

`_atomic_write_snapshot` writes via `tempfile.mkstemp` + Python text I/O, then
`os.replace(tmp, target)`. `os.replace` is atomic at the directory-entry level
(one VFS operation), but the tempfile's data blocks may still be in the kernel
page cache when replace runs. A power loss between rename and the kernel's
delayed writeback leaves a directory entry pointing at empty/partial content;
the next run's `read_snapshot` then chokes on malformed JSONL with no recovery.
The fix opens the tempfile read-only and `os.fsync`s its fd between
`write_snapshot` and `os.replace`. Parent-directory fsync is deliberately
skipped — the user's filesystems (ext4/btrfs/xfs) handle the dirent flush
implicitly under the journal, and explicit dir-fsync adds filesystem-specific
complexity for a vanishingly rare crash window.

### 22. Dest cleanup deferred to preserve the no-change-no-touch invariant

AD-2 requires that a no-change run never accesses the backup drive. Through pass
9, `_run_backup_for_endpoints` called `_cleanup_orphan_tempfiles(dest_root)`
early — before `snapshot_tree`, before the `any_changes()` short-circuit. A
sleeping backup drive therefore got woken on every run just to scan for dest
orphans, even when nothing had changed. The fix moves the dest-cleanup call to
**just before `apply_moves`** (after the `any_changes()` early return AND after
the catastrophic-delete guard). The src-cleanup call stays early because src is
always touched anyway by `snapshot_tree`. The dest-orphan recovery invariant
(orphan-on-dest from a killed mid-dest-write must be reaped before the next dest
write) is preserved: every run that reaches `apply_moves` also runs the cleanup.

### 23. Iterative explicit-stack walk in snapshot_tree

`snapshot_tree`'s inner `walk(dir_path)` was recursive, adding one Python stack
frame per directory level. The default `sys.getrecursionlimit()` is 1000; trees
deeper than that (artificial but legal — bazel/make namespace simulators
routinely produce them) raised `RecursionError` mid-walk and left the user with
no snapshot. The fix replaces the recursive walk with an explicit-stack DFS
loop. Order changes from "DFS in-order" to "DFS reverse-order", which doesn't
matter — snapshot rows aren't sorted at write time and `compute_moves` keys by
inode, not row position. Verified with a depth-1500 test (under PATH_MAX with
single-char dir names) and 10/10 deterministic integration runs.

### 24. Mount gate: base_dir is exempt, and the gate runs before any write

`check_mounted` (`src/irsync/preflight.py`) doesn't test the endpoint itself —
it tests the first path component below `base_dir` (`mount_gate_root`), because
the drive-letter shorthand destinations (`mypython`, `~`) are subdirectories of
a mounted drive rather than mountpoints themselves. `base_dir` (e.g.
`/media/<user>`) is always exempt, even under `--require-mount`: it is by
definition the ordinary directory that *contains* mountpoints, never one
itself. The gate runs in `run_backup` **before** `_source_lock`, which
`mkdir`s the source root and opens `.irsync.lock` for writing — gating after
the lock would let an unmounted source get files created on it by the very
check meant to protect it. `--snapshot-only` runs the same gate on its
(source-only) endpoint; an earlier version of this task omitted that and was
corrected. Remote endpoints (strings, not `Path`) are always exempt — there is
no local mount to check.

### 25. Destination gate is baseline-driven, not mount-driven, and remote-aware

`_run_backup_for_endpoints` calls `foreign_dest_entries` only when there is no
usable baseline (no snapshot, or one rejected by the provenance check), and
only for a **local** destination — a remote one is never scanned. The
first-run summary (`_format_first_run_preview`) reports a remote destination's
existing-entries line as "not checked (remote)", not "0 existing entries": it
was never scanned, and presenting an unchecked 0 as fact would be worse than
saying so. `run_all_backups` treats `EndpointNotMounted` as a skip (exit 0
still possible) but `UnsafeDestination` (destination exists but can't be read)
as a real error: counted, batch continues to the remaining drives, run exits 1.
These are deliberately different outcomes for deliberately different failure
shapes.

### 26. `--force` overrides one guard; `--allow-massive-delete` overrides the other

`--force` means only "run rsync despite the snapshot diff finding no changes."
Bypassing the >50% deletion guard (AD-6) requires the separate
`--allow-massive-delete`. One override per guard, so a cron line that adds
`--force` for routine reason 1 cannot silently disarm the catastrophic-delete
protection too. Both flags are rejected at parse time when combined with
`--no-snapshot`, since neither has an effect there (there is no snapshot diff
to override).

### 27. Nested-mount warning is aggregated, not per-path, and walk-only

`snapshot_tree` logs one `logging.warning` per walk when `xdev=True` causes it
to skip a directory on another filesystem, naming the count and the first five
skipped paths (`(and N more)` beyond that) rather than staying silent or
logging per-path noise. The boundary decision itself is extracted into the
pure `_crosses_boundary(entry_dev, root_dev, xdev)` helper specifically so it
is unit-testable without root: creating a real nested mount to exercise the
warning end to end requires privileges neither the suite nor CI has. The
warning never fires for `--no-snapshot` runs, which skip the snapshot/diff
layer entirely and never call `snapshot_tree`.

## Appendix A — bug catalog

28 bugs fixed across 10 review passes (passes 1-8 plus pass 10; pass 9 was
test-density tightening only, with no source bug). Listed roughly by severity
within each pass.

### Pass 1 — initial implementation (commit 2e81995)

- **Cycle-emitter bug** in `inode_compare.py:543-558` that lost a chain when its
  tail was a visited node. Fixed in `src/irsync/diff.py`'s
  `plan_directory_moves`. Still latent in `inode_compare.py` itself (not fixed
  there because that file is reference-only).

### Pass 2 — first review (commit 0cfcaef, 8 fixes)

- **H1**: rsync `--exclude .irsync_snapshot.jsonl` was unanchored, matched at
  every depth.
- **H2**: inode reuse made unrelated files look like renames; gated moves on
  size + `mtime_ns`.
- **H3**: snapshots had no provenance, so a stale snapshot from another tree
  could become the diff baseline. Added header + verification.
- **H4**: preview only showed counts, not deletion paths. Added paged per-path
  preview.
- **H5**: cycle-temp names were deterministic and could collide with real files.
  Randomized with `secrets.token_hex(4)`.
- **M1**: `--snapshot-only` required a destination it didn't use. Routed past
  `resolve_endpoints`.
- **M2**: no protection against concurrent runs. Added `fcntl.flock` at
  `<src>/.irsync.lock`.
- **M3**: no sanity check for catastrophic diffs. Added 50% deletion threshold +
  `--force` override.

### Pass 3 — second review (commit afa5137, 3 fixes)

- **NEW-H1**: in-place file modifications were invisible to `compute_changes`
  (same path, same inode, different content). Backup got silently skipped. Added
  `Changes.modified`.
- **NEW-H2**: the lockfile from pass 2 was being included in snapshots and
  transferred by rsync. Added to the reserved-namespace exclusions.
- **NEW-M1**: orphan `.irsync-snap-*` tempfiles from killed runs accumulated at
  source root. Added to reserved namespace + startup cleanup.

### Pass 4 — third review (commit fad0890, 2 fixes)

- **NEW-H3**: a file simultaneously renamed AND edited fell through three
  defenses and was invisible. Restructured `compute_moves` to return
  `consumed_keys` so `compute_changes` can backfill unaccounted path-set
  differences into `deleted`/`created`/`modified`.
- **NEW-H4** (same fix as H3): the same invisibility for renamed symlinks,
  hardlinks with one path dropped, and type-change inodes.
- **NEW-M1 (4th-pass)**: `_run_snapshot_only` didn't acquire the source lock, so
  a concurrent regular run's tempfile cleanup could delete its mid-write
  tempfile. Now wrapped in `_source_lock`.

### Pass 5 — fourth review (commit 62b3415, 4 fixes + 3 verification tests)

- **NEW-H1 (5th — broken, fixed in pass 6)**: NFS-style silent data loss with
  second-granular mtime + same-second inode reuse. Original fix added a
  `ctime_ns` tiebreaker; wrong, because rename also ticks ctime (see pass 6).
- **NEW-H2**: `apply_moves` raised uncaught `OSError(EXDEV)` mid-flight if dest
  spanned a mount boundary. Added `_preflight_check_xdev` +
  `CrossDeviceMoveError`; backup catches and returns 2 without persisting the
  snapshot.
- **NEW-M1**: `--no-snapshot --yes` silently ran without producing a dry-run
  preview, breaking AD-7's cron-auditability invariant. `_run_rsync_only` now
  always runs the dry-run; routes to stdout under `--yes`, through `less`
  interactively.
- **NEW-M2**: `--force` was silently inert when paired with `--no-snapshot`.
  Reject the combination at parse time.
- **3 verification tests** for previously-claimed-but-untested self-healing
  properties.

### Pass 6 — corrective + integration (commit 9a980b1, 1 fix + integration test)

- **NEW-H1 (5th-pass take 2)**: replaced the broken `ctime_ns` gate with
  `btime_ns` via a new `src/irsync/statx.py` ctypes wrapper. Gate requires btime
  equality only when both sides have it (`>= 0`); falls back to size+mtime alone
  when statx is unavailable, the FS doesn't expose btime, or the snapshot is
  legacy.
- **`tests/test_integration.py`**: end-to-end test building a deterministic
  30-file random tree, baselining, then applying one labeled mutation per
  reproducible bug from passes 1-5 (anchored excludes, in-place edit,
  lockfile-not-on-dest, orphan tempfile cleanup, rename+edit, symlink rename,
  hardlink path drop, cycle swap, plus a genuine rename for the new btime gate),
  layering 8 random fuzz moves on top, running irsync again, and asserting both
  global tree convergence and each bug's specific outcome.

### Pass 7 — symlink + subprocess + persistence (commit 69db124, 4 fixes + 4 integration tests)

- **NEW-H1 (HIGH)**: `compute_changes` filtered `created`/`deleted` to `{d, f}`,
  so an isolated symlink add / delete / replace left `any_changes()` False and
  the dest silently went stale. Extended the filter to include `l`.
- **NEW-H2 (MEDIUM)**: `subprocess.Popen` lacked `start_new_session=True`, so on
  parent SIGKILL rsync orphaned and a next run could race it. Added session
  isolation + `try/except BaseException` that terminates the child before
  propagating.
- **M1 (MEDIUM-LOW)**: `_persist_snapshots` wrote src first; flipped to
  dest-first so a dest-write failure leaves src at the old baseline (DR-restore
  safety).
- **M2 (LOW)**: `read_snapshot` / `read_jsonl` accepted unknown row types and
  let `compute_changes` drop them silently. Now rejects up front.
- **+4 integration tests**: cross-device replay refusal via real preflight;
  `--no-snapshot --yes` audit invariant end-to-end; real-syscall NFS-sim via
  real `snapshot_tree` + surgical row mutation; standalone H1 isolation test
  that fails closed if `"l"` is dropped.

### Pass 8 — subprocess hardening + consistency (commit 3b6c96d, 3 MEDIUM + 2 LOW + 1 defensive)

- **NEW-H1 (MEDIUM)**: pass-7's persist-flip wrote dest first, so a kill
  mid-dest-write left an orphan `.irsync-snap-*` tempfile at the backup drive
  that the anchored rsync exclude refused to sweep. Extended
  `_cleanup_orphan_tempfiles` to scan both roots.
- **NEW-H2 (MEDIUM)**: `proc.terminate()` only signaled the rsync leader; with
  `start_new_session=True` and rsync forking ssh for remote endpoints, ssh kept
  writing to dest after cancellation. Replaced terminate/kill with
  `os.killpg(os.getpgid(proc.pid), SIGTERM/SIGKILL)`.
- **NEW-H3 (MEDIUM)**: Python's default SIGTERM handler killed the process
  without raising, so pass-7's `try/except BaseException` cleanup never ran on
  cron timeout / `systemctl stop`. Installed a scoped SIGTERM-→-
  `KeyboardInterrupt` handler in `run_real_sync`, restored on exit, main-thread
  only.
- **M1 (LOW)**: `_page_output`'s less subprocess inherited the parent's session.
  Added `start_new_session=True` for parity with rsync.
- **M2 (LOW)**: pass-7 added `"l"` to the `{d, f}` whitelist; type `"o"` was
  still excluded. Latent today (`include_other=False` by default) but same
  silent-skip class. Extended filter to all types `snapshot_tree` records.
- **Defensive type-change fix**: `compute_changes`' shared loop silently lost a
  path if an inode's type differed between snapshots. Now treats it as both
  deleted AND created.
- **+5 new tests**: dest orphan tempfile cleanup; killpg on cancel; SIGTERM
  handler installed/restored; SIGTERM handler raises `KeyboardInterrupt`; pager
  `start_new_session=True`. Plus a real-syscall kernel-inode-reuse test (skips
  where btime collapses to mtime, like tmpfs).

### Pass 9 — fuzz density + diminishing returns (commit 890a2a9, no source changes)

- **Bug audit returned empty** for the first time. A strict-mode agent with hard
  rules confirmed no genuine remaining bugs justified a fix. Findings considered
  and rejected were documented so they aren't re-litigated.
- **Test-density tightening**: the integration test's `_pick_file_move` and
  `_pick_dir_move` now use the 10-attempt no-op retry loops from
  `make_random_moves.py:135-148, 169-178`. Pure test-code change;
  sort-before-choice determinism preserved; 10/10 deterministic runs confirmed.

### Pass 10 — encoding + invariants + durability + scalability (commit a889673, 5 fixes + 10 new tests)

The user asked specifically about parallelism/threading, encoding/locale-
specific paths, very-deep-tree behavior, and extremely-large-snapshot memory
pressure. Three Explore agents flagged 6 candidate bugs; **3 were false
positives** caught only by reading the source.

- **F1 (HIGH for affected users)**: `snapshot.py` opened its 4 file I/O
  endpoints with default `errors="strict"`. Linux-legal arbitrary-byte
  filenames surface as surrogate-escaped strs (`\udc80–\udcff`); `f.write(...)`
  on these raised `UnicodeEncodeError` mid-snapshot. Added
  `errors="surrogateescape"` to all 4 `open()`s for lossless round-trip.
- **F2 (MEDIUM, AD-2 invariant)**: `_cleanup_orphan_tempfiles(dest_root)` was
  called before the no-changes short-circuit, waking the backup drive every run.
  Deferred the dest cleanup to just before `apply_moves`; src cleanup stays
  early. Pass-8 dest-orphan recovery preserved.
- **F3 (LOW, durability)**: `_atomic_write_snapshot` did `write_snapshot` then
  `os.replace` with no `fsync` between them. Power loss in that window leaves
  the live snapshot pointing at empty/partial content. Added explicit
  `os.fsync(tmp_fd)` before `os.replace`.
- **F4 (MEDIUM, performance)**: `read_snapshot` did
  `text = file.read_text(...); lines = text.split("\n")`, peaking at ~2× file
  size in RAM. Switched to streaming line-by-line read mirroring `read_jsonl`.
  Extracted shared `_validate_row(obj, file, lineno)` helper; both readers use
  it; error messages now reference real file line numbers.
- **F5 (LOW-MEDIUM, scalability)**: `snapshot_tree`'s inner `walk(dir_path)` was
  recursive — one Python frame per directory level. Trees deeper than
  `sys.getrecursionlimit()` (~1000) raised `RecursionError` mid-walk. Replaced
  with explicit-stack DFS. Order flipped from in-order to reverse-order
  (downstream is sort-stable by inode, not row order). Verified depth-1500 and
  10/10 deterministic integration runs.

**Rejected after source verification**: symlink lock race (flock keys on inode,
not path); literal-newline JSONL corruption (`json.dumps` escapes per RFC 8259);
rsync exit 23/24 persisting snapshot (code returns `rc != 0` before persist).

## Appendix B — review-pass workflow

The workflow that produced the ten passes, condensed to "how to add a new fix":

1. **Dispatch parallel Explore agents AND do your own walk-through.** For each
   pass, run one or more Explore agents in parallel with detailed prompting
   about what to look at, while also doing your own read of the source.
   **Cross-check every claimed bug against the actual source at the cited
   file:line BEFORE adding it to the plan** — agents invent plausible-sounding
   bugs (3 of 6 findings in pass 10 were false positives). The verification step
   is fast (read 20-30 lines) and saves implementing a fix for a non-bug. When
   broad sweeps return empty, switch to dimension-targeted sweeps (parallelism,
   encoding, deep-tree, memory, data loss) — different question shapes find
   different bugs. Document rejected findings with WHY, so the next pass doesn't
   re-litigate them.
2. **AskUserQuestion to scope (1-3 focused questions) before exiting plan
   mode.** Offer "(Recommended)" on the option you'd pick but make the
   alternatives genuine; include a "stop here" option when returns are
   diminishing. A good question reshapes the plan; a bad one is a survey. Don't
   ask plan-approval questions — that's ExitPlanMode.
3. **TDD red/green per fix, with bug-ID test names.** Write a failing test whose
   name includes the bug ID (`test_<pass>_<fid>_<descriptor>`, e.g.
   `test_5th_h2_preflight_refuses_crossing_devices_before_any_rename`), run it,
   confirm RED, write the minimum implementation, run it, confirm GREEN, and keep
   the test as a regression marker. For any fix involving filesystem semantics
   (mtime/ctime/btime/dev), back the unit test with a **real-syscall** test
   before trusting it — synthesized rows demonstrate the intended defense, not
   the actual one (the pass-5 ctime trap).
4. **Verify the round.** `pixi run test` (all green), `pixi run lint`,
   `pixi run typecheck`, stage only irsync files (never user scratchpads),
   `pre-commit run --all-files`, commit with a Conventional Commits message.
5. **Run the integration test 5× to catch flakiness — 10× after any change that
   affects the fuzz-seed trajectory or the snapshot walk order** (e.g., the
   pass-9 picker changes, the pass-10 F5 walk flip). The integration test pulls
   triple duty — it caught the pass-6 broken-ctime discovery and three harness
   determinism bugs the unit tests missed.
6. **Report honestly, including diminishing returns.** Calibrated honesty beats
   apparent productivity.

## Appendix C — how to invoke

```bash
# First backup of a drive (creates snapshots on both sides):
pixi run python -m irsync /path/to/source /path/to/dest --yes

# Subsequent backup with no changes: backup drive never accessed.
pixi run python -m irsync /path/to/source /path/to/dest --yes

# Drive-letter shortcuts (per Options.from_defaults defaults — /media/$USER/<letter>):
pixi run python -m irsync G --yes

# Backup all configured drives in one go:
pixi run python -m irsync ALL --yes

# Snapshot-only baseline (no destination needed):
pixi run python -m irsync /any/local/path --snapshot-only

# Force backup even when "no changes":
pixi run python -m irsync /src /dest --yes --force

# Skip the inode logic entirely (behave like srsync):
pixi run python -m irsync /src /dest --yes --no-snapshot
```
