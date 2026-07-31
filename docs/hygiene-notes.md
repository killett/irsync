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
- **The destination gate fires before the `--dry-run` branch.** In
  `_run_backup_for_endpoints`, the baseline-less destination check runs before
  the `args.dry_run` branch is reached, so a read-only dry run against a
  non-empty first-run destination also refuses rather than merely previewing.
  Contestable — a dry run arguably shouldn't be blocked by a guard whose whole
  point is preventing writes — but recorded here rather than changed, since
  changing it was out of this pass's scope.
- **The `PermissionError` log-and-return block is duplicated verbatim**
  between `run_backup` and `_run_snapshot_only` in `src/irsync/backup.py`
  (each catches a read-only source root around its own `_source_lock` call
  and logs/returns identically). Could be factored into a helper alongside
  the existing `_log_lock_conflict`. Not done in this pass — behavior is
  correct, just duplicated.
