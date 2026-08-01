# Hygiene notes

Running log of hygiene decisions, so a later pass doesn't re-litigate them.
Each entry records what was looked at, what was deliberately left alone, and
why. Entries are append-only; correct one in place only when the underlying
code changes.

## 2026-07-30 — whole-repo audit (audit-only scope)

Baseline before any edit: `ruff check .` clean, `ruff format --check .` clean,
`mypy .` clean, `167 passed, 1 skipped`.

### Applied

- `refactor: extract duplicated lock-conflict logging into a helper`
  (`_log_lock_conflict` in `src/irsync/backup.py`).
- `refactor: name the non-obvious orchestrator exit codes`
  (`EXIT_REFUSED` / `EXIT_LOCK_HELD` / `EXIT_ABORTED` in `src/irsync/backup.py`).
- `refactor: drop redundant proc.wait() after communicate() in the pager`.
- `docs: correct the project-structure listing in the README`.

### Deliberately kept

- **`_run_backup_for_endpoints` is long (~180 lines).** Every seam in it is a
  safety-critical ordering constraint (AD-2 "no-change run never wakes the
  backup drive", AD-15 dest-snapshot-before-src, 10th-F2 deferred dest
  cleanup). Splitting it would scatter those constraints across functions
  where a future reader can reorder them without noticing. Kept whole and
  commented in place.

- **`statx.is_available()` has no callers.** Introduced in `6579761` alongside
  the statx wrapper. It is importable public API of a shipped package
  (`irsync` publishes `py.typed`), so removing it is an API break for an
  unknown external caller, and it is the natural probe for a test or a future
  "your filesystem has no btime" diagnostic. Kept.

- **`NodeInfo["nlink"]` is populated but never read** (`src/irsync/diff.py`).
  Hardlink handling keys off `len(paths)` instead. The field mirrors the
  snapshot `Row` shape, which is documented in the architecture doc, so
  dropping it from one of the two structures makes them diverge for no gain.
  Kept.

- **Four near-identical `build_rsync_command(...)` call sites in
  `backup.py`.** Same five keyword arguments each time. Wrapping them would
  hide the `dry_run=True` / `dry_run=False` distinction, which is exactly the
  thing a reviewer of this file needs to see at the call site. Textual
  similarity, not a shared decision. Kept.

- **`index_by_inode` uses `r.get("mtime_ns", -1)`** even though `_validate_row`
  already fills the sentinel. Cheap defense for rows built by hand (tests,
  future callers) that bypass the validator. Kept.

- **`_cleanup_orphan_tempfiles` returns a count both callers discard.** The
  return value is meaningful for tests and for a future caller that wants to
  report it. Kept.

### Behavior changes — raised separately, then approved and fixed

These were reported to the user rather than folded into the hygiene commits,
because each changes observable behavior. All three were approved in the same
session and landed with red/green tests.

- **`src/irsync/__main__.py` discarded `main()`'s return value**, so
  `python -m irsync` always exited 0 — even on rsync failure, lock conflict, or
  user abort. The `irsync` console script was unaffected (its generated wrapper
  calls `sys.exit(main())`). Fixed in `ff9417a` with `raise SystemExit(main())`.
  Consequence to remember: cron wrappers now see 75 for "another run holds the
  lock" and 130 for a user abort, both of which are benign — treat them
  separately from real failures.

- **`run_dry_run` runs rsync with `check=True` and neither call site in
  `backup.py` caught `CalledProcessError`**, so rsync's routine 23/24/255
  exits became a traceback with the code flattened to 1. Fixed in `9f932c8`
  via `_dry_run_preview`, which logs rsync's code plus the argv and returns
  that code; a negative returncode (signal-killed) maps to `EXIT_REFUSED`.
  This deliberately keeps the codebase's uniform "any non-zero rsync is a
  failure" policy — `run_real_sync` already refuses to persist the snapshot on
  a non-zero return, and making the preview more permissive than the real run
  would have split that policy in two.

  Still open in the same area: `FileNotFoundError` (rsync not installed)
  escapes as a traceback from both `run_dry_run` and `run_real_sync`. Not
  fixed — it is a wider change than the preview boundary.

- **`run_all_backups` returns 0 when drives were missing.** Investigated and
  found to be *deliberate*, not a defect: `Options.all_backups` lists 16
  entries covering every drive the user might ever attach, so on any given day
  most are unmounted and a non-zero exit would make every nightly ALL sweep
  look broken. The real problem was that the contract was undocumented while
  the "Finished with issues" warning sat next to exit 0. `044c036` documents
  the contract on `run_all_backups` and in the README, and reports the
  previously-unused `successful` list in both summary log lines. Exit codes
  unchanged. If cron alerting on an absent drive is ever wanted, the right
  shape is a per-drive "expected" list, not a blanket `--strict-missing` flag
  across all 16 entries.

### Environment observations

- The local canonical gate (`pixi run pre-commit run --all-files`) runs
  `ruff check --fix` and `ruff format`, both of which **mutate** files. CI
  (`.github/workflows/test.yml`) runs the non-mutating `ruff check .` and
  `ruff format --check .`. Use the CI form when you need a read-only baseline.

- `tests/test_snapshot.py`'s btime probe skips or runs depending on whether the
  `tmp_path` filesystem reports btime, so the suite legitimately reports either
  `167 passed, 1 skipped` or `168 passed` on the same code.

- The working tree carried uncommitted `pixi.toml` / `pixi.lock` changes adding
  ~18 dependencies unrelated to irsync (`typer`, `pydantic`, `httpx`,
  `openjdk`, `ipython`, …). Left untouched.

## 2026-07-31 — mount-safety pass (hazards A–G closed)

Baseline before this pass: `ruff check .` clean, `ruff format --check .`
clean, `mypy .` clean, `226 passed, 1 skipped`. Same after — this pass is
docs-only (README.md, this file, and the architecture doc).

Design doc: `docs/superpowers/specs/2026-07-31-mount-safety-design.md`
(hazards A–G defined there). Plan: `docs/superpowers/plans/2026-07-31-mount-safety.md`.

### Hazards closed

- **A — unmounted source wipes the backup.** Closed redundantly by two
  independent guards, per the design's "cover the worst outcome twice"
  principle: the mount gate (`d124396`, refined by `df1a850` and `d506890`)
  and the destination gate (`a2c43a4`, clarified by `610f06b`).
- **B — unmounted destination fills the root filesystem.** Closed by the same
  mount gate (`d124396`) — it checks both endpoints, not just the source.
- **C — `ALL` multiplies A.** Closed by the mount gate treating
  `EndpointNotMounted` as a per-drive skip inside `run_all_backups`
  (`d124396`), plus the destination gate (`a2c43a4`) catching the case where
  the mountpoint directory itself resolves. `610f06b` fixed a related bug
  where `UnsafeDestination` was aborting the whole `ALL` batch instead of
  being counted as an error and continuing.
- **D — the first run confirms blind.** Closed by the first-run summary
  (`b7d3bcd`, gaps closed by `4a41bc1`): scale (entry count, byte size) and the
  destination's existing-entry count (or "not checked (remote)") are shown
  before the confirmation prompt, for both no-baseline situations (no snapshot
  at all, and a snapshot rejected by the provenance check).
- **E — recoverable conditions exit via traceback.** Closed by `364e176`
  (missing rsync binary, read-only source root, missing source path, and the
  `ValueError` family from `resolve_endpoints` all become a logged refusal at
  exit 2, no traceback) and `a0b652c` (the rsync-availability check no longer
  applies to `--snapshot-only`, which never invokes rsync).
- **F — nested mounts are skipped silently.** Closed by `4753a32`: one
  aggregated warning per walk, naming the count and first five skipped paths.
- **G — `--force` conflates two meanings.** Closed by `0230ded` (split into
  `--force` for the no-change short-circuit and `--allow-massive-delete` for
  the deletion-ratio guard), with a documentation follow-up in `ea3738e` for
  stale `--force` references the split left behind.

### Coverage gaps — stated honestly, not fixed

- **Hazard F has no end-to-end test.** Creating a real nested mount requires
  root, which neither the test suite nor CI has. The boundary decision is
  unit-tested via the extracted pure helper `_crosses_boundary` in
  `test_snapshot.py`; the integration — an actual mount inside a snapshot
  walk producing the aggregated warning — is not exercised anywhere.
- **The `(and N more)` truncation branch of the nested-mount warning** (more
  than 5 skipped paths) is untested, as is `include_other=True` combined with
  a crossed filesystem boundary. Both are plausible in production and neither
  has a regression test.
- **The destination gate has a `--dry-run` carve-out.** In
  `_run_backup_for_endpoints`, the baseline-less destination check
  (`backup.py:665`) is gated on `not args.dry_run`, so a dry run against a
  non-empty first-run destination previews instead of refusing — `foreign`
  is still computed unconditionally so the first-run preview (Rule 3) can
  report it. The empty-source guard just above it (`backup.py:640`) has no
  such carve-out and runs first, so it still pre-empts the destination
  gate's dry-run exemption whenever the source itself is empty; see AD-28.
- ~~**The `PermissionError` log-and-return block is duplicated verbatim**
  between `run_backup` and `_run_snapshot_only` in `src/irsync/backup.py`.~~
  **Resolved.** It was factored into `_log_source_permission_error`
  alongside the existing `_log_lock_conflict`; both call sites in
  `run_backup` and both in `_run_snapshot_only` now use it. Corrected here
  in the 2026-08-01 pass, which found the bullet still claiming otherwise.

### Post-review additions (found in the final whole-branch review)

Two behavior gaps surfaced only once the whole branch was read end to end
against the "what does a cron job actually see" question, after the pass
above had already closed hazards A–G individually. Both approved by the
project owner and implemented as follow-up commits on this same branch.

- **An unmounted DESTINATION was a silent ALL success.** `check_mounted`
  raised one `EndpointNotMounted` for either endpoint, so `run_all_backups`
  treated "drive `G_backup` isn't mounted" exactly like "drive `H` isn't
  attached today" — a skip, exit 0. That's correct for a source (most of
  `Options.all_backups` is unattached on any given day) but wrong for a
  destination: if `G`'s source is mounted but its backup drive is not, the
  run backs up nothing and a cron job still reports success. Fixed by
  `6847e43`: `check_mounted` gained a keyword-only `role` parameter
  selecting between two new sibling subclasses (`SourceNotMounted`,
  `DestinationNotMounted`, both still `EndpointNotMounted`), and
  `run_all_backups` now handles `DestinationNotMounted` exactly like
  `UnsafeDestination` — counted, batch continues, run exits 1 — instead of
  folding it into the skip path. See AD-29.
- **A baseline-less backup from an EMPTY source had no guard of its own.**
  The destination gate (AD-25) only fires when the *destination* holds
  foreign entries; an equally-empty destination sailed through, even
  though "empty source, no baseline" is itself the unmounted/mistyped-
  source signature that motivated this whole branch (hazard A), independent
  of what the destination looks like. Fixed by `bad36f6`: a new guard
  refuses whenever there is no usable baseline and the source is empty —
  `len(fresh_rows) == 1 and fresh_rows[0]["path"] == "."` for the snapshot
  path (`snapshot_tree` always includes the root, so `== 0` would never
  fire), and a direct `source_root_is_empty` filesystem check, sharing
  `foreign_dest_entries`' reserved-namespace list via a new
  `_non_reserved_names` helper, for the `--no-snapshot` path. New flag
  `--allow-empty-source`, one override per guard as usual. Deliberately
  does not fire when a baseline exists, so a genuine mass-deletion still
  goes through the existing >50% guard instead of this one. See AD-28.

### Hazard H — `--dry-run` ran a real, destructive rsync on `--no-snapshot`

Found in a post-merge review of `_run_rsync_only` after the branch above was
otherwise complete and reviewed (252 passed, 1 skipped): it built a dry-run
command for the preview and then unconditionally built a SECOND, real
(`dry_run=False`) command and called `run_real_sync` — `args.dry_run` was
never consulted on this path. Confirmed by reproduction:
`irsync SRC DEST --no-snapshot --dry-run --yes` against a destination
holding a file not present on the source deleted that file for real, then
reported `rsync completed successfully` and exit 0.

**Pre-existing, not introduced by this branch.** It survived unnoticed
because every other reproduction in this pass used the snapshot path, where
`--dry-run` was already honored correctly (the `if args.dry_run:` block in
`_run_backup_for_endpoints`); `--no-snapshot`
is comparatively rarely exercised end to end (see "11th-pass dimensions" in
`PROGRESS.md`).

Fixed by adding an `args.dry_run` check to `_run_rsync_only` right after the
preview (which, on this path, already *is* rsync's `--dry-run` output):
return 0 before building the real command, matching the snapshot path's own
`--dry-run` contract. See AD-30. Covered by three tests in
`tests/test_backup.py::TestNoSnapshotDryRunHonored`: one runs the real rsync
binary (no mocking) so a regression shows up as actual data loss and not
merely a changed return code — verified to fail against the pre-fix code
before the fix landed — one confirms `--no-snapshot` without `--dry-run`
still performs the real sync, and one monkeypatches `run_real_sync` to fail
the test if it is called at all during a dry run.

### Minor findings from the same review

- **M1 — `source_root_is_empty` didn't fail closed on `PermissionError`.**
  Its sibling `foreign_dest_entries` deliberately raises `UnsafeDestination`
  when the destination can't be listed, rather than guessing;
  `source_root_is_empty` caught only `FileNotFoundError`/`NotADirectoryError`
  and had no `Raises:` section documenting the gap. In practice
  `run_backup`'s own broad `except PermissionError` around
  `_run_backup_for_endpoints` already converts this into a plain refusal
  before it can reach `run_all_backups` or `cli.main` — so, checked against
  the actual code rather than assumed, this was not currently producing a
  traceback. Documented the `Raises:` section, added `PermissionError` to
  `run_all_backups`' and `cli.main`'s exception handling as a backstop for
  the earlier `resolve_endpoints`/`check_mounted` window that isn't covered
  by that broad catch, and added `test_unreadable_source_fails_closed` in
  `test_preflight.py`.
- **M2 — a comment claimed `--no-snapshot` "has no concept of a prior
  snapshot at all."** False: a snapshot file can exist on disk from an
  earlier run; what's true is that this code path never reads one. Reworded
  at `backup.py:557` to state the actual consequence: a user with a real
  baseline who genuinely emptied their source and runs `--no-snapshot` is
  steered to `--allow-empty-source` rather than `--allow-massive-delete`,
  because the >50% guard is unreachable on this path.
- **M3 — this file's own coverage-gap bullet had gone stale.** It said the
  destination gate refuses on `--dry-run`; the `not args.dry_run` carve-out
  at `backup.py:665` (added in this same pass, see AD-25/AD-28 above)
  contradicts that. Corrected above.
- **M4 — AD-28 didn't record the dry-run asymmetry as intentional.** The
  empty-source guard is deliberately *not* `--dry-run`-exempt, unlike the
  destination gate, and since it runs first it pre-empts the destination
  gate's carve-out whenever the source is empty. Added to AD-28: a dry-run
  preview of an empty source is a wall of deletions that conveys less than
  the refusal message does.

## 2026-08-01 — whole-repo audit (mutation opted into for docs + dead code)

Baseline before any edit: `ruff check .` clean, `ruff format --check .` clean,
`mypy .` clean, `256 passed, 1 skipped`. After: same three clean,
`252 passed, 1 skipped` — four tests removed with the code they were the only
callers of (two for `parse_rsync_output`, two that duplicated a `read_snapshot`
equivalent). No behavior change to any irsync invocation.

Scope was whole-repo, which defaults to audit-only; the project owner opted in
to mutation for the documentation fixes and to deleting the dead public helpers.

### Applied

- `a815995` `docs: list preflight.py in the README project structure` — the
  module holding every refusal guard was the only one missing from the listing.
- `75427c7` `docs: anchor the dry-run cross-reference to a symbol, not a line
  number` — three copies pointed at `backup.py:696`; the check had moved to 703.
  Now they name the `if args.dry_run:` block in `_run_backup_for_endpoints`, so
  editing above it can't invalidate the pointer again. **Prefer symbol
  references to line references in this repo** — this is the second pass to
  find a drifted one.
- `6b1ef5a` `docs: mark the duplicated-PermissionError gap as resolved` — the
  2026-07-31 coverage-gap bullet still said the `PermissionError`
  log-and-return block was duplicated between `run_backup` and
  `_run_snapshot_only` and proposed factoring it out.
  `_log_source_permission_error` already does exactly that.
- `cef7fae` `refactor: remove the unused parse_rsync_output helper` — plus its
  two regexes, the `re` import, and its two tests.
- `4d4f0f0` `refactor: remove the unused legacy JSONL writer and reader` —
  `write_jsonl`/`read_jsonl`, superseded by `write_snapshot`/`read_snapshot`.

### Deleting dead code without deleting coverage

`read_snapshot` still parses the **legacy headerless format** that
`write_jsonl` used to write — a user upgrading across the provenance-header
change has one on disk. So the tests were triaged rather than deleted with the
functions: the two that only duplicated a `read_snapshot` equivalent went, and
the four covering surviving behavior (missing-key rejection, legacy
btime-sentinel default, `header is None` on a headerless file, surrogate-path
round-trip) were ported to `read_snapshot`. Producing a headerless file is now
`_write_headerless` in `test_snapshot.py`: an input format the product must
still *read* but no longer *writes* belongs in the test scaffolding, not in
`irsync.snapshot`.

Generalizable: "delete the function and its tests" is the wrong default
whenever a *surviving* code path shares the deleted function's behavior. Check
what the tests actually pin down before removing them.

### Deliberately kept

- **`statx.is_available()` still has no callers** and is now the only such
  symbol left, which reads as inconsistent with the two removals above. It
  isn't: unlike `parse_rsync_output` (a parser for output irsync prints
  verbatim) and `write_jsonl` (a writer for a format irsync stopped writing),
  `is_available` is a *probe for a runtime capability the diff already depends
  on* — the natural implementation of a "your filesystem doesn't report btime,
  so rename detection is degraded" diagnostic, which is an open item in
  `PROGRESS.md`. Kept for that, not for API-compatibility reasons.

- **`_run_snapshot_only` and the `args.snapshot_only` branch of
  `_run_backup_for_endpoints` duplicate the snapshot-write sequence**
  (`snapshot_tree` → `_atomic_write_snapshot` → an identical
  `"Snapshot-only: wrote %d rows to %s"` log). This is a real DRY violation —
  one decision, two copies, they will change together — not mere textual
  similarity. Not merged in this pass: the two arrive with different
  invariants already established (the second has taken the lock and run
  `_cleanup_orphan_tempfiles`; the first has neither, and has no destination
  at all), so a shared helper has to take those as parameters and the merge
  stops being behavior-obvious. Worth doing deliberately, with tests, not as a
  hygiene drive-by.

- **`_run_snapshot_only` re-implements the drive-letter / `~` / `mypython`
  shorthand** that `resolve_endpoints` owns (`options.py`). Two places to edit
  to add a shortcut. Flagged, not merged: `resolve_endpoints` is built around
  producing a source *and* a destination, and `--snapshot-only` has no
  destination — the honest fix is extracting a shorthand-resolution function
  both call, which is a design change rather than a cleanup.

### Environment observations

- **`cli.py` reports 13% coverage; this is an artifact, not a gap.**
  `tests/test_cli.py` drives the CLI through `subprocess.run`, so the child
  process isn't instrumented and none of `_build_parser`/`main` is counted,
  despite ten real end-to-end tests. Whole-repo total is 87%. Fixing the number
  needs `coverage`'s subprocess support (`COVERAGE_PROCESS_START` plus
  `--parallel` and a combine step); do not "fix" it by converting those tests
  to in-process calls, since running the actual installed entry point is the
  point of them.

- The uncommitted `pixi.toml` / `pixi.lock` dependency additions noted in the
  2026-07-30 entry are still present and still untouched.
