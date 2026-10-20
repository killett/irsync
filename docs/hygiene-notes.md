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
- ~~**The `(and N more)` truncation branch of the nested-mount warning** (more
  than 5 skipped paths) is untested.~~ **Corrected 2026-10-18:** covered since
  `b342f60` (21 minutes after this entry was written) by
  `test_walk_reports_truncated_count_past_five_skipped_dirs`. Still true:
  `include_other=True` combined with a crossed filesystem boundary has no
  regression test.
- **The destination gate has a `--dry-run` carve-out.** In
  `_run_backup_for_endpoints`, the baseline-less destination check is gated
  on `not args.dry_run`, so a dry run against a non-empty first-run
  destination previews instead of refusing — `foreign` is still computed
  unconditionally so the first-run preview (Rule 3) can report it. The
  empty-source guard just above it has no such carve-out and runs first, so
  it still pre-empts the destination gate's dry-run exemption whenever the
  source itself is empty; see AD-28.
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
  in `_run_backup_for_endpoints`' `--no-snapshot` branch to state the actual
  consequence: a user with a real baseline who genuinely emptied their
  source and runs `--no-snapshot` is steered to `--allow-empty-source` rather
  than `--allow-massive-delete`, because the >50% guard is unreachable on
  this path.
- **M3 — this file's own coverage-gap bullet had gone stale.** It said the
  destination gate refuses on `--dry-run`; the `not args.dry_run` carve-out
  in `_run_backup_for_endpoints` (added in this same pass, see AD-25/AD-28
  above) contradicts that. Corrected above.
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
the five covering surviving behavior (one-JSON-object-per-line output,
missing-key rejection, legacy btime-sentinel default, `header is None` on a
headerless file, surrogate-path round-trip) were ported to
`write_snapshot`/`read_snapshot`. Producing a headerless file is now
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

## 2026-09-04 — whole-repo audit (mutation opted into)

Baseline before any edit: `pixi run pre-commit run --all-files` green
(`ruff`, `ruff-format`, `mypy` all pass), `322 passed, 1 skipped`. After:
same gate green, `323 passed` — 323 collected either way, the btime probe
simply ran on the final invocation instead of skipping (the tmpfs variation
the 2026-07-30 entry records). Every change in this pass is strictly
behavior-preserving, and no test was added or removed.

Scope was whole-repo, which defaults to audit-only; the project owner opted
in to mutation. The owner also asked that the long-standing uncommitted
`pixi.toml` / `pixi.lock` dependency additions (noted as untouched in both
the 2026-07-30 and 2026-08-01 entries) be committed first — they landed as
`b5cfb63`, ahead of the pass proper, and are not part of it.

### Applied

- `988c690` `refactor: name the snapshot tempfile prefix at the one place
  that writes it` — `_atomic_write_snapshot` passed the literal
  `".irsync-snap-"` to `tempfile.mkstemp` while every *consumer* of that
  prefix matches against `SNAPSHOT_TEMPFILE_PREFIX`, which `backup.py`
  already imports: `_cleanup_orphan_tempfiles`, `snapshot_tree`'s
  reserved-name skip, and `build_rsync_command`'s anchored `--exclude`. Same
  string today, so nothing observable changes; the point is that the
  reserved namespace is one decision and the only *writer* of it was the one
  place not referring to the constant.

- `9c444fb` `refactor: share the one _args CLI-namespace factory from
  conftest` — `test_backup.py` and `test_integration.py` each carried a
  byte-identical 14-key `argparse.Namespace` factory (they differed only in
  type annotations) behind 94 call sites. Now `conftest.cli_args`, imported
  as `from .conftest import cli_args as _args` in both so no call site
  moves. This one qualifies under the DRY rule the earlier entries apply
  strictly: it is not textual similarity but a mirror of
  `cli._build_parser`'s flag surface, and a new flag that `backup.py` reads
  had to be added to both copies or the missed one fails with
  `AttributeError`. Same reason to change, changing together.

- `ba0e34f` `docs: correct subprocess_pythonpath's stale reason for listing
  drivecfg` and the `PROGRESS.md` gotcha fixed in the same pass — both still
  described `drivecfg` as importable only through a temporary
  `PYTHONPATH=../drivecfg/src` shim, with the gotcha instructing a future
  session to "delete all three once the real dependency installs." `5b99aee`
  deleted the shims and `drivecfg` 0.1.0 has been on PyPI since.
  `README.md` and `RELEASING.md` were checked and were already correct, so
  this was a two-file drift, not a repo-wide one.

  **Third pass running, third stale cross-reference.** 2026-08-01 concluded
  "prefer symbol references to line references"; the generalization this
  pass adds is that the same drift hits *state* claims, not just locations.
  A note that says "until X happens, Y is true" is a landmine the moment X
  happens — the commit that makes X true is the one that has to sweep for
  those notes, and `5b99aee` updated `README.md` and `RELEASING.md` but not
  `PROGRESS.md` or the conftest docstring. When retiring a workaround, grep
  for its distinctive strings (`drivecfg/src`, `PYTHONPATH`) across the
  whole repo rather than the files you remember writing.

### Deliberately kept

Everything the 2026-07-30 and 2026-08-01 entries already settled was
re-checked against the current code and left as those entries describe:
`statx.is_available()` (still uncalled, still the natural home for the
btime-diagnostic open item), the four near-identical `build_rsync_command`
call sites, `NodeInfo["nlink"]`, `_cleanup_orphan_tempfiles`' discarded
return, `index_by_inode`'s defensive `.get`, `_run_backup_for_endpoints`'
length, and the two `--snapshot-only` duplications (snapshot-write sequence
and shorthand resolution). Not re-litigated here.

New to this pass:

- **`_format_size`'s trailing `return f"{value:.1f} TiB"` is unreachable**
  (`backup.py`) — the loop's `or unit == "TiB"` guarantees the last
  iteration returns. It is already commented as existing to satisfy mypy's
  no-implicit-return. Documented intent; kept.

- **`_INTERNAL_FILES` is defined in both `test_backup.py` and
  `test_integration.py`**, which looks like the `_args` duplication above
  but is not the same case. The two predicates genuinely differ:
  `test_integration`'s `_is_internal` compares a *basename* and also matches
  `SNAPSHOT_TEMPFILE_PREFIX`, while `test_backup` compares whole relative
  paths against the two-name set. Merging them would change what
  `test_backup` filters out of its tree signatures, so it is not
  behavior-preserving and does not belong in a hygiene pass. Kept.

- **`ruff --select C901` reports six functions over the complexity
  threshold** (`_run_backup_for_endpoints` 26, `plan_directory_moves` 19,
  `cli.main` 15, `resolve_endpoints` 15, `run_real_sync` 14,
  `snapshot_tree` 14). `C901` is not in the project's `select` list, so this
  is not a gate failure — it was run only as an audit probe.
  `_run_backup_for_endpoints` is covered by the 2026-07-30 decision; the
  other five are cohesive single decisions (argument resolution, the
  cycle-breaking scheduler, CLI dispatch, the rsync signal/cleanup ladder,
  the explicit-stack walk) whose score is branch count, not tangling — and
  two of them, `run_real_sync` and `snapshot_tree`, score high precisely
  *because* of hard-won hazard fixes (7th-NEW-H2 / 8th-NEW-H2 / 8th-NEW-H3,
  and 10th-F5) that a split would scatter. Kept, and C901 deliberately not
  added to the lint config.

### Reported, not fixed

Per "bugs are separate," these were recorded as findings rather than folded
into a hygiene commit. Both are also in `PROGRESS.md`'s Open questions.

- **`plan_directory_moves`' `dfs` is recursive** (`diff.py`) — one Python
  frame per link in a rename chain, so a diff containing ~1000+ *chained*
  directory renames (`a`→`b`, `b`→`c`, …) raises `RecursionError` and kills
  the run before rsync. This is precisely the failure 10th-F5 removed from
  `snapshot_tree` by switching to an explicit stack; the same hazard is
  still present in the other traversal. Not reproduced against real input —
  it needs a synthetic chain that long — and fixing it changes behavior, so
  it wants its own red/green test.

- **The config-requirement rule is duplicated between `options.py` and
  `cli.py`.** `needs_drive_config` is documented (in `PROGRESS.md`'s
  gotcha) as "the single place the rule lives," but `cli.main` computes a
  second predicate inline — the `optional = not required and
  args.snapshot_only and ...` expression — for the "consult a config only
  if one happens to exist" case. Adding an argument form means editing both.
  A sibling predicate in `options.py` would fix it and would be
  behavior-preserving if done exactly, but this is the newest and most
  safety-relevant control flow in the codebase (it decides whether a run
  consults a drive layout at all), so it deserves a deliberate change with
  tests rather than a drive-by.

### Environment observations

- The audit probe `ruff --select ARG` reports 89 unused-argument hits, all
  in `tests/`, and nearly all are pytest fixture parameters or
  monkeypatch-lambda signatures that *must* accept arguments they ignore.
  `ARG` is correctly absent from the project's `select` list; do not add it.

- `cli.py`'s coverage number is still the subprocess artifact described in
  the 2026-08-01 entry. Unchanged and still not worth "fixing."

- **`PROGRESS.md` was gitignored and had never been committed — now
  resolved by tracking it.** The audit found `git add` refusing it:
  `.gitignore` listed it beside `CLAUDE.md` under "Local dev-process docs —
  intentionally not published", and `git log -- PROGRESS.md` was empty.
  That was a documented decision, so the pass reported it rather than
  overriding it; the project owner then chose to track the file. It sat in
  direct tension with the project's own durability rules ("Git is the
  source of truth, not the conversation"; "Refresh the next-action line and
  commit PROGRESS.md after each task"), which cannot be followed for a file
  git will not accept, and it meant the file every session is told to read
  first survived only on the working disk — a re-cloned workspace lost the
  entire notebook of decisions, gotchas, and open questions. `CLAUDE.md`
  stays ignored; only PROGRESS.md was unlisted.

  **One consequence to carry forward: PROGRESS.md is now public.** Before
  tracking it, the file was scanned for credential-shaped strings, personal
  paths, and email addresses; no secret value was ever in it. One Gotchas
  entry did describe a credential-hygiene follow-up in more operational
  detail than belongs in a published file, so at the owner's request it was
  redacted down to the part that is genuinely a repo gotcha (an injected
  `GH_TOKEN` can arrive without `workflow` scope, which blocks any push
  touching `.github/workflows/`, and how to re-grant it). The full original
  text moved to `SECURITY-TODO.local.md`, which `.gitignore` excludes, so
  the reminder survives without being published. The redaction was done by
  amending the commit that first added PROGRESS.md rather than by a
  follow-up commit — nothing had been pushed, so the text never entered
  reachable history at all. **Generalizable: the moment a local-only file
  becomes tracked, re-read it as a stranger would.** Its whole prior life
  was spent under the assumption that nobody outside the machine would see
  it, and that assumption is written into the prose.

## 2026-10-18 — whole-repo audit (record, clean up, and fix all approved)

Baseline before any edit: `pixi run pre-commit run --all-files` green,
`322 passed, 1 skipped`. No file under `src/` or `tests/` had changed since
the 2026-09-04 pass, so this pass looked for what the three earlier passes
missed; nothing they settled is re-litigated here. Scope was whole-repo and
the audit itself ran audit-only; the project owner then approved all three
follow-ups — record the audit, apply its clean-ups and doc fixes (one commit
each, gate after each), and fix the four bugs red/green. The work is on
branch `hygiene-2026-10-18`; plan:
`docs/superpowers/plans/2026-10-18-hygiene-pass.md`.

Probes worth reusing: an AST scan for functions with no docstring (ruff's
`D` rules skip `_`-prefixed names, so the gate cannot see the "every
function has a docstring" rule being broken); read-only ruff runs with rule
families the project does not select (`RUF100`, `PLR2004`, `PLC0415`,
`SIM`, `PT`, …); a script checking that every backticked symbol in the docs
still exists in code; and a direct runtime repro of each suspected bug
before calling it one.

### Bugs (behavior changes — approved; each is fixed red/green by its own plan task)

- **A corrupt snapshot killed a whole `ALL` run.** `read_snapshot` and
  `_validate_row` raised `SystemExit` (as did `snapshot_tree` for a missing
  root), which is not an `Exception`, so it escaped `run_all_backups`'
  handlers and `cli.main`'s refusal boundary. Reproduced with two
  endpoints, the first holding a malformed snapshot line: process exit 1,
  the second endpoint never backed up, no summary line, and the message on
  bare stderr outside logging. Owner's decision: refuse that drive only. A
  new `SnapshotCorrupt` (`ValueError` subclass) becomes a logged refusal
  (exit 2) in `_run_backup_for_endpoints`; an `ALL` batch counts it as one
  error and continues; `snapshot_tree`'s root errors become
  `FileNotFoundError` / `NotADirectoryError`.
- **The `Diff:` log line omitted `modified`.** It dates from `b52c107`;
  `Changes.modified` arrived in `dba3768` and the line was never updated.
  Reproduced: an edit-only run logged
  `Diff: 0 dir moves, 0 file moves, 0 created, 0 deleted.` and then
  re-transferred the file.
- **The default exclude `.Trash-1000` only matched uid 1000.** Owner's
  decision: `.Trash-*`. The same default's `unfinished_downloads` (a
  personal folder name) is **deferred** at the owner's request — see
  `PROGRESS.md`, Deferred items.
- **SOURCE's `--help` was wrong under `--snapshot-only`:** it said every
  bare path without a destination needs a config.

### Clean-ups (strictly behavior-preserving, all low risk)

- *magic* — `_file_type_char` hand-codes `(mode & 0o170000) == 0o100000`,
  which is `stat.S_ISREG(mode)`.
- *magic / redundant* — `cli.main` compares against a literal `"ALL"`
  while `options.ALL_ARG` exists and `needs_drive_config` uses it.
- *redundant* — the row type tags `f`/`d`/`l`/`o` are spelled out six
  times across `snapshot.py` and `diff.py`. `compute_changes`' own
  comments record that 7th-NEW-H1 and 8th-M2 were exactly `diff.py`'s copy
  falling behind. One `ROW_TYPES`.
- *redundant + documentation* — the reserved-root-name check exists in
  both `snapshot_tree` and `preflight._non_reserved_names`, whose
  docstring claims the list is "spelled out in exactly one place". AD-5
  requires the copies to change together. One `is_reserved_root_name`.
- *redundant* — AD-7 ("print the preview under `--yes`, page it
  otherwise") is coded three times in `backup.py`. One `_emit_preview`.
- *documentation* — `_run_backup_for_endpoints`, `_run_rsync_only` and
  `_build_parser` have no docstrings; `main` has no Args/Returns; and
  `run_backup`'s Raises omits the `ValueError` / `FileNotFoundError` /
  `NotADirectoryError` that `run_all_backups` relies on to recognise a
  skip.
- *documentation* — comments pointing at context a reader cannot find:
  "decision 1" (`needs_drive_config`; it exists nowhere in this repo),
  "Task 3" and "Rule 3" (the mount-safety plan and spec, unnamed), and
  "this branch" (`run_all_backups`, merged long ago).
- *documentation* — `statx.py` says btime is missing on "older ext4
  without `-O extent`"; ext4 reports `i_crtime` only when the field fits in
  the inode, i.e. on 256-byte inodes. Extents are unrelated.

### Doc drift (documentation only, no runtime risk)

- `PROGRESS.md`: all 23 AD bullets sat on one line and rendered as a single
  bullet; AD-24–AD-30 were missing; AD-6's line still named `--force` as
  the override for the >50% guard (it has been `--allow-massive-delete`
  since AD-26); and Current work pointed at
  `docs/superpowers/plans/2026-07-18-handoff-migration.md`, which has never
  existed in git history. The dangling pointer is removed by the commit
  that adds this entry; the AD index has its own task.
- `docs/design/irsync-architecture.md`: the package-layout tables predate
  mount-safety and drivecfg — no `preflight.py` row, `options.py`
  described as a "drive-letter map", a four-flag `cli.py` row, and a tests
  table missing `test_preflight.py`, `test_main.py` and four `conftest`
  helpers. The doc never mentioned drivecfg at all; its rationale lived
  only in a `PROGRESS.md` gotcha. Fix the tables and record the decision as
  AD-31.
- This file's 2026-07-31 entry says the `(and N more)` truncation branch is
  untested; `b342f60` added that test 21 minutes after the entry was
  written. The entry's `backup.py:665` / `:640` / `:557` references have
  also drifted. Correct in place.
- Test comments cite `diff.py:154-161`, `backup.py:401-406` and
  `backup.py:696`, and "the 16 configured entries" of the pre-drivecfg
  layout. Anchor them to symbols.
- `RELEASING.md` still presents the trusted-publisher setup as a step to do
  before the first release (done 2026-09-06) and describes the autotick
  bot of a conda-forge feedstock that does not exist (conda-forge was
  declined).

**Fourth pass running, fourth drift finding — and now the pattern has a
shape.** Each feature that changed what a module *is* (mount-safety on
2026-07-31, drivecfg on 2026-08-16) left the architecture doc's
package-layout tables and `PROGRESS.md`'s AD index behind, because neither
sits near the code it describes. When a commit adds a module, a flag, or an
AD, update those two places in the same commit.

### Kept, with reasons

- `# noqa: S404` on `import subprocess` (`backup.py`, `rsync_runner.py`).
  `RUF100` reports them unused, but S404 is a preview-only rule today, CI
  installs ruff unpinned, and if S404 becomes stable `select = ["S"]` will
  enforce it and CI would fail without them. Forward-defensive; kept.
- `preflight._non_reserved_names`' function-local import (and the
  `TYPE_CHECKING` import of `Row`): the mount-safety plan records the local
  import as deliberate insurance against a future import cycle. Kept.
- `cli.main`'s lazy imports: consistent with lazy config loading. Kept.
- `build_rsync_command`'s own remote test (`":" in s and not
  s.startswith("/")`) beside `paths.is_rsync_remote`. They agree on every
  input that can reach it — local endpoints arrive as resolved absolute
  paths — but differ on others such as `a/b:c`, and the function is
  public, so unifying them is an API decision, not a clean-up. Flagged and
  kept.
- The truncation limits `10` (the `foreign` list in
  `_run_backup_for_endpoints`) and `5` (`snapshot_tree`'s skipped-mount
  warning), each repeated inside one expression; the two adjacent copies of
  the error-budget check in `run_all_backups`; and the cosmetic probe hits
  (`SIM105` in `_source_lock`, `C416`/`C420` in `plan_directory_moves`,
  `RUF059` unused `dir_moves` unpacks in `test_diff.py`, `PT011` broad
  `pytest.raises` that all assert on the message afterwards, `ERA001` false
  positives on prose comments). Local and visible; kept.
- The `.pre-commit-config.yaml` `exclude` for reference scripts absent
  from this checkout: harmless, and only matters if one is ever staged.

### Environment observations

- CI (`.github/workflows/test.yml`) never runs mypy; only the local
  pre-commit gate does.
- pixi pins `python <3.14`, while CI lints and tests on 3.14 and the
  classifiers claim it.
- `drivecfg`'s allowed range is `<0.2` in `pixi.toml` but `<1` in
  `pyproject.toml`: once drivecfg 0.2 ships, CI and the local environment
  resolve different versions.

### Applied

Task numbers are those of `docs/superpowers/plans/2026-10-18-hygiene-pass.md`.
Every commit below was made on branch `hygiene-2026-10-18`, which branched
from `main` at `eed5237` and was fast-forwarded into `main` on 2026-10-19;
one task is one commit unless the line lists more.

- Task 0, record the audit: `e189eaa` "docs: record the 2026-10-18 hygiene
  audit and its plan"; review fix round `5f06690` "docs: mark plan Task 0
  complete and tighten the audit record".
- Task 1, `stat.S_ISREG`: `9228f9f` "refactor: test for regular files with
  stat.S_ISREG".
- Task 2, `ALL_ARG`: `44dc539` "refactor: compare against ALL_ARG instead of
  a second "ALL" literal".
- Task 3, `ROW_TYPES`: `7a47a3a` "refactor: define the snapshot row type
  tags once".
- Task 4, `is_reserved_root_name`: `c2cc234` "refactor: share one
  reserved-root-name predicate between the walk and preflight".
- Task 5, `_emit_preview`: `34b4b8c` "refactor: route every preview through
  one AD-7 helper".
- Task 6, `Diff:` line counts `modified`: `54d5093` "fix: count modified
  files in the Diff summary log line".
- Task 7, SOURCE's help: `9a5037e` "fix: state the --snapshot-only
  exception in SOURCE's help", plus `b0a4875` "fix: keep SOURCE's help
  ASCII-only" (the plan's own wording had put an em dash in the help).
- Task 8, `.Trash-*`: `34fdcf7` "fix: exclude every user's desktop trash,
  not only uid 1000's".
- Task 9, a corrupt snapshot refuses only its own drive: `0806da1` "fix:
  refuse a corrupt snapshot per drive instead of exiting the process"; review
  fix round `93f8942` "fix: treat a non-object snapshot line as corrupt" and
  `1c63082` "fix: point a corrupt-snapshot refusal at a recovery that works".
- Task 10, docstrings: `020871d` "docs: document the orchestrator and CLI
  entry points".
- Task 11, plan-relative comments: `2311317` "docs: replace plan-relative
  references with self-contained wording".
- Task 12, `statx` and ext4: `f230692` "docs: correct the ext4 condition for
  missing btime".
- Task 13, architecture-doc tables: `e0509f0` "docs: bring the architecture
  doc's package tables up to date".
- Task 13b, added during the pass (not in the plan's original list): the
  corrupt-snapshot docs, `deca056` "docs: describe the corrupt-snapshot
  refusal".
- Task 14, AD-31: `c498dad` "docs: record the drivecfg layout decision as
  AD-31".
- Accuracy fix round for Tasks 10, 13, 13b and 14: `f8ccb99` "docs: make the
  new architecture and CLI docs match the code exactly".
- Task 15, AD index in `PROGRESS.md`: `079a433` "docs: list one AD per line
  in PROGRESS.md, through AD-31".
- Task 16, this file's 2026-07-31 entry: `2f60b6c` "docs: correct the
  2026-07-31 hygiene notes against the current code".
- Task 17, test comments: `5639a2e` "test: anchor drifted comment
  references to symbols".
- Task 18, `RELEASING.md`: `c019e84` "docs: update RELEASING.md for the live
  publisher and the conda-forge decision".
- Final whole-branch review fix wave: `4d62488` "docs: correct the README's
  corrupt-snapshot bullet", `2d4533c` "docs: make the corrupt-snapshot docs
  match the reader exactly", `dca4722` "docs: document _identity_subst and
  point the rsync excludes at their predicate", and `08527b9` "test: keep
  --help ASCII-only".
- Task 19, this closing record: the commit "docs: close out the 2026-10-18
  hygiene pass" that adds this list. It also corrects the architecture
  doc's test total and updates `PROGRESS.md`.
- Plan tracker sync, in six commits that only tick the plan's checkboxes and
  its `.tasks.json`: `b047333`, `714b1d0`, `ceabcf7`, `11a2174`, `67d036e`
  and `e21dd93` (each "docs: mark plan Task(s) ... complete"), plus a final
  sync after the closing commit.

Not applied, by decision: `unfinished_downloads` stays in the default
excludes (deferred, `PROGRESS.md` Deferred items), and the `# noqa: S404`
comments stay (see Kept, above).

### Counts

Before: 323 collected — `322 passed, 1 skipped` (323 passed when the btime
probe runs). Confirmed by collecting at `eed5237` in a scratch worktree.

After: 337 collected, and the final `pixi run test-cov` printed `337 passed`
(the btime probe ran; where btime collapses to mtime, e.g. tmpfs, it skips
and the same code reads `336 passed, 1 skipped`).

The 14 new tests, counted from the diff against `eed5237`:

- `tests/test_backup.py`, 5: `TestDiffSummaryLog` (1, an edit-only run logs
  the modified count); `TestDefaultExcludes` (2, one test parametrized over
  `.Trash-1000` and `.Trash-1001`);
  `TestCorruptSnapshotRefusesOnlyThatDrive` (2, a single run and an `ALL`
  run).
- `tests/test_cli.py`, 2: SOURCE's help names the `--snapshot-only`
  exception; `--help` is ASCII-only.
- `tests/test_snapshot.py`, 7: a root that is a file raises
  `NotADirectoryError` (1); a malformed first line is `SnapshotCorrupt` (1);
  a non-object row is `SnapshotCorrupt` (1 test, 4 parametrized cases:
  `42`, `null`, `[1]` and `"x"`); a non-object `_meta` header is
  `SnapshotCorrupt` (1).

Nine of the 14 (2 + 7) belong to the corrupt-snapshot work. No test was
removed or renamed. Four existing assertions in `test_snapshot.py` changed
their expected exception from `SystemExit` to `FileNotFoundError` or
`SnapshotCorrupt`; every other edit under `tests/` is comment rewording.

### Found while fixing

Each was confirmed with a probe script at the branch tip; none is fixed on
this branch. The first three are reproduced hazards and the fourth is an
observation. For the `TypeError` and `IsADirectoryError` shapes in the third
item the probe stops at `run_backup`; that the exception then escapes an `ALL`
batch follows from the `except` clauses of `run_all_backups` and `cli.main`.
They were filed as the first four Open questions in `PROGRESS.md`; all three
reproduced hazards have since been fixed (below). A fifth finding, a path
escape, surfaced on 2026-10-19 while fixing the third and is listed last.

- **Trash replay.** Files reach a desktop trash by being *moved* there, and
  irsync replayed that move onto the backup. Because `.Trash-*` is excluded
  from the rsync, `--delete-before` never removed the replayed copy, so the
  deleted file stayed on the backup even after the trash was emptied. The
  probe moved a file into `.Trash-1001/files/`, backed up, emptied the
  trash, and backed up again: the file was still on the destination. With
  the old default and the trash dir named `.Trash-1000`, the same file also
  stayed; with the old default and `.Trash-1001` (not excluded) it was
  removed. So the hazard predates this branch for uid 1000, and the
  `.Trash-*` default (the owner's decision) extends it to every uid. Root
  fix for a later pass: drop planned moves whose target falls under an
  excluded name, or apply the exclude list in the snapshot walk.
  **Fixed on 2026-10-19** in `cd277ea` (AD-32), by a variant of the second
  option: the snapshot walk still records everything, and `run_backup`
  drops excluded rows from both snapshots before diffing. A trashed file is
  now a deletion from the backup, and an emptied trash no longer counts
  toward the >50% guard. Re-running the probe left no `.Trash-1001` on the
  destination. Copies replayed by older versions stay where they are.
- **Empty baseline.** `read_snapshot` returned `(None, [])` for a
  zero-length or whitespace-only snapshot file and `(header, [])` for a
  header-only one: a baseline with no rows. The destination gate (AD-25),
  the empty-source guard (AD-28) and the >50% guard (AD-6) were then all
  skipped. The probe backed up an unrelated non-empty source over a
  destination holding five files under `--yes`, with each of the three
  shapes, and rsync deleted all five. A legitimate snapshot always has a
  header and a root row, so these shapes are always damage; the natural
  fix is to raise `SnapshotCorrupt`. **Fixed on 2026-10-19** in `d1bf4c6`:
  `read_snapshot` now raises `SnapshotCorrupt` for all three shapes, so the
  run refuses that drive (exit 2) and leaves the file in place. Re-running
  the probe refused each shape and left all five destination files.
- **Other ways one bad drive can kill an `ALL` run.** A row whose values
  had the wrong types (`"path": 5`, `"dev": [1]`) raised `TypeError`, and a
  `.irsync_snapshot.jsonl` that is a directory raised `IsADirectoryError`.
  Neither was caught by `run_backup`, `run_all_backups` or `cli.main`, so
  the exception escaped the batch. (`"ino": "a"` and `"size": null` were
  silently accepted.) A per-drive `except Exception` in `run_all_backups`
  that logs and counts an error would close the whole class. **Fixed on
  2026-10-19** in two layers. `139db1a` makes `read_snapshot` refuse a
  snapshot path that is not a regular file (a FIFO used to block `open()`
  forever) and `_validate_row` refuse a non-`int` or `bool` under an integer
  key and a non-`str` path, all as `SnapshotCorrupt`, so the drive gets a
  clean refusal (exit 2). By then `"path": 5` raised `AttributeError` from
  AD-32's exclude filter rather than `TypeError`. `32e5faf` adds the
  catch-all (AD-33), logged with its traceback; it caught a remaining
  shape, a directory where the *destination's* snapshot copy belongs, which
  raises `IsADirectoryError` while persisting after rsync. `RsyncUnavailable`
  is re-raised ahead of it, since a missing rsync is the run's problem and
  `cli.main` reports it once.
- **Non-ASCII in user-facing strings (pre-existing).** The first-run banner
  in `_format_first_run_preview` contains an em dash. Under `--yes` it is
  printed (interactively it goes to `less` via `_page_output`), so it raises
  `UnicodeEncodeError` on an ASCII-only stdout (`PYTHONIOENCODING=ascii`).
  The `_require_config` `ValueError` message and the permission-denied log
  line in `run_backup` also contain one, but they go through logging to
  stderr, whose `backslashreplace` error handler escapes the character
  instead of failing. A C locale does not trigger any of this (Python still
  writes UTF-8 there), so it is mostly theoretical. `--help` itself is now
  ASCII and guarded by a test.
- **Path escape (found 2026-10-19).** `apply_moves` joins each old snapshot
  path onto the destination unchecked. A probe wrote a snapshot whose row
  for a real source file carried the path `../outside/victim.txt`, or that
  file's absolute path, and backed up under `--yes`: irsync moved
  `victim.txt`, which sat outside both trees, into the backup and exited 0.
  Anyone able to write a source drive's root could so move any file the
  irsync user can write. Present in 0.1.0. **Fixed on 2026-10-19** in
  `0d32b46`: `_validate_row` refuses a path other than `"."` that is
  absolute, has an empty, `.` or `..` component, or contains NUL (AD-4).
  Re-running the probe refused the run (exit 2) and left `victim.txt` and
  the destination alone. A follow-up probe showed the row check alone was
  not enough: `os.rename` follows a symlinked directory in the backup, so a
  normalized row `link/victim.txt`, where the backup's `link` pointed at
  `../outside`, still moved `victim.txt` in. Without any tampering,
  replacing a source symlink by a real directory and moving a file into it
  made replay write that file where the backup's stale symlink pointed.
  Fixed in `ec7ab83` (AD-9): `apply_moves` skips a move whose old or new
  path runs through a symlinked directory and lets rsync transfer the data.
  When that symlink points to another filesystem, the EXDEV pre-flight
  still refuses every run; that predates the branch and is an Open question
  in `PROGRESS.md`.

### Process lessons

- Two defects in this pass came from prose the orchestrating session wrote
  into the plan, not from the agents that implemented each task: an em dash
  in SOURCE's help (it broke `--help` on an ASCII stdout; fixed in
  `b0a4875`) and an AD-21 sentence that claimed more than the reader does
  (corrected in `f8ccb99`, then `2d4533c`). Plan-written text needs the same
  accuracy review as code; both were caught only because every task was
  reviewed against the code.
- Per-task reviews found gaps the plan did not anticipate, such as the
  non-object snapshot lines in Task 9's review, and they noted facets of the
  other two. But the trash replay and the empty baseline were reproduced only
  by the final whole-branch review's runtime probes. Budget a whole-branch
  review with runtime probes, not only per-task diff reviews.

### Status

Complete. The owner chose a local merge: `main` was fast-forwarded to the
branch tip (`f0a0478`) on 2026-10-19, the gate passed on the merged result
(pre-commit clean, 337 passed), and the branch was deleted. Not yet pushed.
