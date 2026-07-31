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

### Known, not fixed here (behavior changes — need a decision)

These are real defects, not style. A hygiene pass must not fix them silently.

- **`src/irsync/__main__.py` discards `main()`'s return value**, so
  `python -m irsync` always exits 0 — even on rsync failure, lock conflict, or
  user abort. The `irsync` console script is unaffected (the generated wrapper
  wraps it in `sys.exit`). Fix would be `raise SystemExit(main())`.

- **`run_dry_run` runs rsync with `check=True` and neither call site in
  `backup.py` catches `CalledProcessError`.** rsync exits 23/24 on
  partial-transfer / vanished-file, which is routine on a live tree, so the
  preview can end in a traceback rather than a clean exit code.

- **`run_all_backups` returns 0 when drives were missing** (the
  `total_errors == 0 and missing` case warns but reports success). Cron reads
  that as a clean run. This is the same partial-failure question already
  listed under "Open questions" in `PROGRESS.md`.

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
