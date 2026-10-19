# 2026-10-18 Hygiene Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the 2026-10-18 whole-repo hygiene audit, apply its approved behavior-preserving clean-ups and doc-drift fixes, and fix the four bugs it found with red/green tests.

**Architecture:** Twenty independent commits in four phases: (A) record the audit; (B) code refactors that change no observable behavior; (C) four bug fixes, each test-first; (D) documentation and docstring fixes, done last so they describe the final code; then a wrap-up that closes the audit record. Each task is exactly one commit.

**Tech Stack:** Python 3.12+, pytest, ruff, mypy (strict), pixi, rsync (real binary in tests), drivecfg.

**Spec:** `docs/hygiene-notes.md`, section "2026-10-18 — whole-repo audit (record, clean up, and fix all approved)" (written by Task 0). Every finding below is defined there.

## Global Constraints

- **One task = one commit.** Conventional Commits, imperative mood (`refactor:`, `fix:`, `docs:`, `test:`). Every commit message ends with the trailer `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- **Gate before every commit**, and the commit is only made if both are green:
  `pixi run pre-commit run --all-files` (ruff, ruff-format, mypy, file hooks) and `pixi run test-cov` (pytest with coverage). Pre-commit's ruff hooks mutate files: re-stage anything they touch and re-run until clean.
- **Stage files by name** (`git add <path> ...`), never `git add -A` / `git add .` — the working directory can hold user scratch that must never be committed. Re-check `git status --short` before each commit.
- **Phases B and D are strictly behavior-preserving:** no change to return values, exit codes, stdout/stderr text, log message text, or files written. The pytest pass count must be unchanged by those tasks.
- **Phase C changes behavior on purpose**, and only in the ways each task states. Follow red/green: write the test, run it, see it FAIL for the stated reason, then implement, then see it PASS.
- **Prefer symbol references over line numbers** in any comment or doc text you write (line numbers drift; this repo has had to fix that four times).
- **Do not push.** Commits stay local.
- **Baseline:** 322 passed, 1 skipped (or 323 passed — the btime probe skips on tmpfs-like filesystems; both are the same code).
- **Running irsync by hand:** `PYTHONPATH=/workspace/src pixi run python -m irsync ...`.

**User decisions (already made):**
- Scope: whole repo; do all three of (a) record the audit, (b) apply clean-ups and doc fixes one commit each with the gate after each, (c) fix the four bugs with red/green tests.
- Corrupt snapshot: "Refuse that drive only" — `read_snapshot`/`_validate_row` raise a new `SnapshotCorrupt` (a `ValueError` subclass) instead of `SystemExit`; the orchestrator logs a refusal and returns exit 2; an `ALL` batch counts it as one error and continues; `snapshot_tree`'s root errors become `FileNotFoundError`/`NotADirectoryError`.
- Trash exclude: replace `.Trash-1000` with the pattern `.Trash-*`.
- `unfinished_downloads` in the default excludes: **defer** — leave it in place and write a note in `PROGRESS.md` to look at it later.
- `# noqa: S404` (audit item "dead noqa"): **leave** — S404 is a preview-only rule today, CI installs ruff unpinned, and if S404 becomes stable `select = ["S"]` would enforce it; the comments are forward-defensive. Recorded, not removed.

---

## Phase A — record the audit

### Task 0: Record the audit and this plan

**Goal:** The audit findings, the user's decisions, and this plan are committed, and `PROGRESS.md` points at them, so a dead session loses nothing.

**Files:**
- Modify: `docs/hygiene-notes.md` (append a new `## 2026-10-18 — ...` entry)
- Modify: `PROGRESS.md` (Current work block; Deferred items; Gotchas)
- Create: `docs/superpowers/plans/2026-10-18-hygiene-pass.md` (this file) and `docs/superpowers/plans/2026-10-18-hygiene-pass.md.tasks.json`

**Acceptance Criteria:**
- [x] `docs/hygiene-notes.md` has a `## 2026-10-18` entry listing the baseline, every finding with its category and decision, the user's decisions, and a pointer to this plan.
- [x] `PROGRESS.md` Current work names this plan as active work and its next action is "first unchecked task in the plan".
- [x] The dangling pointer to `docs/superpowers/plans/2026-07-18-handoff-migration.md` is gone from `PROGRESS.md`.
- [x] `PROGRESS.md` Deferred items has an `unfinished_downloads` bullet.
- [x] Gate green; one commit.

**Verify:** `rg -n '2026-10-18' docs/hygiene-notes.md PROGRESS.md && ! rg -q 'handoff-migration' PROGRESS.md && echo OK` → prints matches then `OK`

**Steps:**

- [x] **Step 1:** Append the 2026-10-18 entry to `docs/hygiene-notes.md`: scope and baseline (`pre-commit` green, `322 passed, 1 skipped`), findings grouped as Bugs / Code clean-ups / Doc drift / Kept / Environment observations, each with category, behavior-preserving yes/no, risk, decision, and the user's decisions from this plan's header. End with "Status: in progress — see `docs/superpowers/plans/2026-10-18-hygiene-pass.md`."
- [x] **Step 2:** Rewrite `PROGRESS.md`'s Current work block: Status = active 2026-10-18 hygiene pass; Pointer = this plan + the hygiene-notes entry; Next action = first unchecked task in the plan. Keep the release facts (0.1.0 published 2026-09-06, trusted publisher live, conda-forge declined because `drivecfg` has no conda-forge package) and move the "PROGRESS.md is public" warning into Gotchas. Drop the summaries of earlier hygiene passes (they live in `docs/hygiene-notes.md`) and the dangling handoff-migration pointer.
- [x] **Step 3:** Add to Deferred items: `unfinished_downloads` is a personal folder name hardcoded in `Options.exclude_dirs`'s default, contradicting the `Options` docstring; there is no CLI/config hook to add excludes, so removing it would silently start backing that folder up; options when revisited: excludes in the drivecfg config, an `--exclude` flag, or keep and document it as a personal default.
- [x] **Step 4:** Write the `.tasks.json` beside this plan.
- [x] **Step 5:** Gate, then commit:

```bash
git add docs/hygiene-notes.md PROGRESS.md docs/superpowers/plans/2026-10-18-hygiene-pass.md docs/superpowers/plans/2026-10-18-hygiene-pass.md.tasks.json
git commit -m "docs: record the 2026-10-18 hygiene audit and its plan"
```

---

## Phase B — behavior-preserving code clean-ups

### Task 1: Use stat.S_ISREG for the regular-file test

**Goal:** `_file_type_char` tests for a regular file with `stat.S_ISREG(mode)` instead of the hand-written `(mode & 0o170000) == 0o100000`.

**Files:**
- Modify: `src/irsync/snapshot.py` (`_file_type_char`)

**Acceptance Criteria:**
- [x] `rg -n '0o170000|0o100000' src` returns nothing.
- [x] `pixi run test` count unchanged from baseline.

**Verify:** `pixi run python -m pytest tests/test_snapshot.py tests/test_diff.py -q` → all pass

**Steps:**

- [x] **Step 1:** In `_file_type_char`, replace

```python
    if (mode & 0o170000) == 0o100000:
        return "f"
```

with

```python
    if stat.S_ISREG(mode):
        return "f"
```

(`stat` is already imported; `S_ISREG(m)` is defined as `S_IFMT(m) == S_IFREG`, i.e. exactly `(m & 0o170000) == 0o100000`.)

- [x] **Step 2:** Gate, then commit `refactor: test for regular files with stat.S_ISREG` (stage `src/irsync/snapshot.py`).

### Task 2: Use ALL_ARG in cli.main

**Goal:** `cli.main` compares the source argument against `options.ALL_ARG` instead of a second `"ALL"` literal.

**Files:**
- Modify: `src/irsync/cli.py` (`main`)

**Acceptance Criteria:**
- [x] `rg -n '== "ALL"' src` returns nothing.
- [x] User-facing strings mentioning `'ALL'` (help text, the `parser.error` message) are unchanged.

**Verify:** `pixi run python -m pytest tests/test_cli.py tests/test_options.py -q` → all pass

**Steps:**

- [x] **Step 1:** Change the import inside `main` to `from irsync.options import ALL_ARG, Options, needs_drive_config`, and change `if args.source_arg.strip().upper() == "ALL":` to `if args.source_arg.strip().upper() == ALL_ARG:`.
- [x] **Step 2:** Gate, then commit `refactor: compare against ALL_ARG instead of a second "ALL" literal`.

### Task 3: One definition of the row type tags

**Goal:** The four snapshot row type tags (`f`, `d`, `l`, `o`) are defined once in `snapshot.py` (`RowType` / `ROW_TYPES`) and every other spelling uses them. The 7th-NEW-H1 and 8th-M2 bugs were `diff.py`'s copy of this list falling behind.

**Files:**
- Modify: `src/irsync/snapshot.py` (new `RowType`, `ROW_TYPES`; `Row.type`; `_file_type_char` return type; `_validate_row`)
- Modify: `src/irsync/diff.py` (`compute_changes` type filters and their comment)

**Acceptance Criteria:**
- [x] `rg -n "\"f\", \"d\", \"l\", \"o\"|\"d\", \"f\", \"l\", \"o\"|'f', 'd', 'l', 'o'" src` matches only the `RowType` definition.
- [x] `_validate_row`'s error message text is byte-identical: `(expected one of 'f', 'd', 'l', 'o')`.
- [x] mypy strict clean.

**Verify:** `pixi run python -m pytest tests/test_snapshot.py tests/test_diff.py -q` → all pass, then `pixi run typecheck` → `Success`

**Steps:**

- [x] **Step 1:** In `snapshot.py`, extend the typing import to `from typing import Literal, TypedDict, get_args` and add after `SNAPSHOT_TEMPFILE_PREFIX`:

```python
RowType = Literal["f", "d", "l", "o"]
"""A snapshot row's type tag: regular file, directory, symlink, or other."""

ROW_TYPES: tuple[RowType, ...] = get_args(RowType)
"""Every type tag a snapshot row may carry. The one list — validation and the diff both use it."""
```

If mypy rejects `get_args(RowType)` on the implicit alias, use a PEP 695 alias instead (`type RowType = Literal[...]` and `get_args(RowType.__value__)`); do NOT annotate with `TypeAlias` (ruff UP040 rejects it).

- [x] **Step 2:** `Row.type: RowType`; `_file_type_char(...) -> RowType:`.
- [x] **Step 3:** In `_validate_row` replace the membership test and message with:

```python
    if obj["type"] not in ROW_TYPES:
        # 7th-M2: corrupted rows with unknown types were silently kept
        # and later filtered out by compute_changes' type whitelist,
        # hiding inode-level corruption. Refuse the whole snapshot.
        expected = ", ".join(repr(t) for t in ROW_TYPES)
        raise SystemExit(
            f"Invalid type {obj['type']!r} in {file}:{lineno} "
            f"(expected one of {expected})"
        )
```

- [x] **Step 4:** In `diff.py`: `from irsync.snapshot import ROW_TYPES, Row`; both `e["type"] in ("d", "f", "l", "o")` become `e["type"] in ROW_TYPES`; in the comment above them, change "Include every snapshotted inode type" to "Include every snapshotted inode type (`ROW_TYPES`, the same tuple `_validate_row` enforces, so this filter can no longer fall behind it)". Keep the 7th-NEW-H1 / 8th-M2 history sentences.
- [x] **Step 5:** Gate, then commit `refactor: define the snapshot row type tags once` (stage both files).

### Task 4: One predicate for irsync's reserved root names

**Goal:** "Is this root-level name irsync's own file?" is answered by one function, `snapshot.is_reserved_root_name`, used by both `snapshot_tree` and `preflight._non_reserved_names` (AD-5 requires every copy to change together).

**Files:**
- Modify: `src/irsync/snapshot.py` (new `is_reserved_root_name`; `snapshot_tree` skip)
- Modify: `src/irsync/preflight.py` (`_non_reserved_names` body and docstring)

**Acceptance Criteria:**
- [x] `rg -n 'SNAPSHOT_FILENAME, LOCKFILE_NAME' src` matches only `is_reserved_root_name`.
- [x] `_non_reserved_names`' docstring no longer claims a list is "spelled out in exactly one place" that it wasn't; it names `is_reserved_root_name`.
- [x] Pass count unchanged.

**Verify:** `pixi run python -m pytest tests/test_snapshot.py tests/test_preflight.py tests/test_backup.py -q` → all pass

**Steps:**

- [x] **Step 1:** In `snapshot.py`, after the constants (and `ROW_TYPES`), add:

```python
def is_reserved_root_name(name: str) -> bool:
    """Return True if ``name`` is one of irsync's own files at a tree root.

    The reserved namespace (AD-5) is the snapshot, the lockfile, and any
    snapshot tempfile a killed write left behind. Only entries directly at
    the root are reserved, so callers pass a root-level entry's name.
    rsync's anchored ``--exclude`` patterns in
    :func:`irsync.rsync_runner.build_rsync_command` are the same namespace
    in glob form and must change with this.

    Args:
        name: The name of an entry directly under the root (no ``/``).

    Returns:
        True if irsync owns that name at the root.
    """
    return name in (SNAPSHOT_FILENAME, LOCKFILE_NAME) or name.startswith(
        SNAPSHOT_TEMPFILE_PREFIX
    )
```

- [x] **Step 2:** In `snapshot_tree`, replace

```python
            if rel in (SNAPSHOT_FILENAME, LOCKFILE_NAME):
                continue
            if "/" not in rel and rel.startswith(SNAPSHOT_TEMPFILE_PREFIX):
                continue
```

with

```python
            if "/" not in rel and is_reserved_root_name(rel):
                continue
```

(Equivalent: a `rel` containing `/` can never equal either reserved filename.) Keep the comment above it, adjusting "Naturally limited to root since..." to say the `"/" not in rel` test is what limits it to the root.

- [x] **Step 3:** In `preflight._non_reserved_names`, keep the function-local import (documented intent in the mount-safety plan: insurance against a future import cycle) but import only `is_reserved_root_name`, and return `[name for name in names if not is_reserved_root_name(name)]`. Rewrite the docstring's first paragraph to: "Shared by :func:`foreign_dest_entries` (destination side) and :func:`source_root_is_empty` (source side). The reserved-name rule itself is :func:`~irsync.snapshot.is_reserved_root_name`, the same predicate the snapshot walk uses, so adding a reserved file is a one-place change."
- [x] **Step 4:** Gate, then commit `refactor: share one reserved-root-name predicate between the walk and preflight`.

### Task 5: One helper for the AD-7 preview rule

**Goal:** "Page the preview interactively, print it under `--yes`" (AD-7) lives in one helper, `_emit_preview`, used by the change preview, the first-run summary, and the `--no-snapshot` dry-run output.

**Files:**
- Modify: `src/irsync/backup.py` (`_show_preview`, the first-run branch of `_run_backup_for_endpoints`, `_run_rsync_only`)

**Acceptance Criteria:**
- [x] `rg -n 'print\((text|out)\)' src/irsync/backup.py` matches only inside `_emit_preview` (and `_page_output`'s no-`less` fallback, which prints `text`).
- [x] Confirmation and abort behavior unchanged (`_confirm_or_abort` still returns True under `--yes`).
- [x] Pass count unchanged.

**Verify:** `pixi run python -m pytest tests/test_backup.py tests/test_integration.py -q` → all pass

**Steps:**

- [x] **Step 1:** Replace `_show_preview` with:

```python
def _emit_preview(text: str, *, interactive: bool) -> None:
    """Show pre-confirmation text: paged when interactive, printed under ``--yes``.

    AD-7: a ``--yes`` (cron) run prints the preview to stdout so the run is
    auditable in its log, and never pages. Every preview goes through here,
    so the change preview, the first-run summary, and the ``--no-snapshot``
    dry-run output cannot drift apart on that rule.
    """
    if interactive:
        _page_output(text)
    else:
        print(text)


def _show_preview(changes: Changes, *, interactive: bool) -> None:
    """Render the change preview, paging through ``less`` if interactive."""
    _emit_preview(_format_preview(changes), interactive=interactive)
```

- [x] **Step 2:** In `_run_backup_for_endpoints`, replace the `else:` branch of `if changes is not None:` with:

```python
    else:
        _emit_preview(
            _format_first_run_preview(fresh_rows, src_root, dest_root, len(foreign)),
            interactive=not args.yes,
        )
```

- [x] **Step 3:** In `_run_rsync_only`, replace

```python
    if args.yes:
        print(out)
    else:
        _page_output(out)
        if not _confirm_or_abort(args):
            return EXIT_ABORTED
```

with

```python
    _emit_preview(out, interactive=not args.yes)
    if not _confirm_or_abort(args):
        return EXIT_ABORTED
```

(Equivalent: `_confirm_or_abort` returns True immediately when `args.yes`.)

- [x] **Step 4:** Gate, then commit `refactor: route every preview through one AD-7 helper`.

---

## Phase C — bug fixes (red/green)

Test-design notes for every test in this phase: each states the behavior, the concrete bug it catches, and where its expected value comes from (construction, not the code under test).

### Task 6: The Diff summary counts modified files

**Goal:** The `Diff:` log line reports `modified` alongside moves, creations, and deletions.

**Files:**
- Modify: `src/irsync/backup.py` (the `logging.info("Diff: ...")` call in `_run_backup_for_endpoints`)
- Test: `tests/test_backup.py` (new class `TestDiffSummaryLog`)

**Acceptance Criteria:**
- [ ] A modification-only run logs exactly `Diff: 0 dir moves, 0 file moves, 1 modified, 0 created, 0 deleted.`
- [ ] The new test fails before the fix (old line lacks `modified`) and passes after.

**Verify:** `pixi run python -m pytest tests/test_backup.py -q -k DiffSummaryLog` → 1 passed

**Steps:**

- [ ] **Step 1: Write the failing test** (append to `tests/test_backup.py`):

```python
class TestDiffSummaryLog:
    """The one-line ``Diff:`` summary is what a cron log keeps; it must count every change kind."""

    def test_modification_only_run_reports_the_modified_count(
        self, src_dest, basic_options, caplog
    ):
        # Behavior: the Diff summary counts in-place modifications.
        # Bug caught: the line predated Changes.modified and reported only
        # moves/created/deleted, so a run that re-transfers an edited file
        # logged all zeros, indistinguishable from a no-op in a cron log.
        # Expected value: by construction exactly one file grows in place;
        # nothing is created, deleted, or moved.
        src, dest = src_dest
        first = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert first == 0
        victim = sorted(src.rglob("*.bin"))[0]
        victim.write_bytes(victim.read_bytes() + b"grown")

        with caplog.at_level(logging.INFO):
            rc = run_backup(
                source_arg=str(src),
                destination_arg=str(dest),
                options=basic_options,
                args=_args(),
            )

        assert rc == 0
        diff_lines = [
            r.getMessage() for r in caplog.records if r.getMessage().startswith("Diff:")
        ]
        assert diff_lines == [
            "Diff: 0 dir moves, 0 file moves, 1 modified, 0 created, 0 deleted."
        ]
```

- [ ] **Step 2:** Run `pixi run python -m pytest tests/test_backup.py -q -k DiffSummaryLog` → FAIL: the list holds `'Diff: 0 dir moves, 0 file moves, 0 created, 0 deleted.'`.
- [ ] **Step 3:** Implement:

```python
        logging.info(
            "Diff: %d dir moves, %d file moves, %d modified, %d created, %d deleted.",
            len(changes.dir_moves),
            len(changes.file_moves),
            len(changes.modified),
            len(changes.created),
            len(changes.deleted),
        )
```

- [ ] **Step 4:** Re-run → PASS. Then `rg -n 'Diff: %d|"Diff:' tests` to confirm no other test pinned the old text.
- [ ] **Step 5:** Gate, then commit `fix: count modified files in the Diff summary log line`.

### Task 7: SOURCE --help names the --snapshot-only exception

**Goal:** The SOURCE help no longer claims every destination-less bare path needs a config; it states the `--snapshot-only` exception (`needs_drive_config`'s last rule).

**Files:**
- Modify: `src/irsync/cli.py` (`_build_parser`, the `source_arg` help)
- Test: `tests/test_cli.py` (new test)

**Acceptance Criteria:**
- [ ] The `source_arg` help mentions `--snapshot-only`.
- [ ] The test fails before the change and passes after.

**Verify:** `pixi run python -m pytest tests/test_cli.py -q -k snapshot_only_exception` → 1 passed

**Steps:**

- [ ] **Step 1: Write the failing test** (append to `tests/test_cli.py`):

```python
def test_source_help_names_the_snapshot_only_exception():
    # Behavior: SOURCE's help says when a config is needed, including the
    # one destination-less form that does not need one (--snapshot-only,
    # needs_drive_config's last rule).
    # Bug caught: the help said every bare path without a DESTINATION
    # needs a config, sending a --snapshot-only user off to write a
    # drives.toml they don't need.
    from irsync.cli import _build_parser

    source = next(a for a in _build_parser()._actions if a.dest == "source_arg")
    assert "--snapshot-only" in " ".join(str(source.help).split())
```

- [ ] **Step 2:** Run it → FAIL (`--snapshot-only` not in the SOURCE help).
- [ ] **Step 3:** Replace the `source_arg` help's last sentence so the string reads:

```python
        help=(
            "Source: a full path, a configured drive id (usually a drive "
            "letter), a configured endpoint name such as '~' for the home "
            "directory or 'mypython' for the Python directory, 'ALL' to back "
            "up every entry in the configured backup order, or an rsync "
            "remote like host:/path. Every shorthand needs a drive config, "
            "and so does a bare path given without a DESTINATION (it must "
            "be checked against the config before it can be treated as a "
            "plain path) — except under --snapshot-only, which uses a config "
            "if one exists and otherwise treats the path literally. A path "
            "given together with a DESTINATION never needs one."
        ),
```

- [ ] **Step 4:** Re-run → PASS; also `pixi run python -m pytest tests/test_cli.py -q` (the no-private-paths help test must still pass).
- [ ] **Step 5:** Gate, then commit `fix: state the --snapshot-only exception in SOURCE's help`.

### Task 8: Exclude every user's desktop trash

**Goal:** The default exclude list skips `.Trash-*` (any uid) instead of only `.Trash-1000`.

**Files:**
- Modify: `src/irsync/options.py` (`Options.exclude_dirs` default and its attribute doc)
- Test: `tests/test_backup.py` (new class `TestDefaultExcludes`)

**Acceptance Criteria:**
- [ ] A default run does not transfer `.Trash-1000/` or `.Trash-1001/`, and does transfer ordinary files.
- [ ] The `.Trash-1001` case fails before the fix; both pass after.
- [ ] `unfinished_downloads` stays in the default list (user decision: deferred).

**Verify:** `pixi run python -m pytest tests/test_backup.py -q -k DefaultExcludes` → 2 passed

**Steps:**

- [ ] **Step 1: Write the failing test** (append to `tests/test_backup.py`):

```python
class TestDefaultExcludes:
    """The default exclude list must skip desktop trash for any user, not only uid 1000."""

    @pytest.mark.parametrize("trash_dir", [".Trash-1000", ".Trash-1001"])
    def test_default_run_does_not_back_up_trash(
        self, tmp_path, basic_options, trash_dir
    ):
        # Behavior: a default run never transfers a freedesktop trash
        # directory (.Trash-$uid), whatever the uid.
        # Bug caught: the default hardcoded ".Trash-1000", so trash owned by
        # any other uid (a second user, a removable drive used elsewhere)
        # was backed up. The 1000 case guards the uid it always covered.
        # Expected values: hand-built tree; keep.txt is the only real data.
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        (src / trash_dir / "files").mkdir(parents=True)
        (src / trash_dir / "files" / "junk.txt").write_text("deleted stuff")
        (src / "keep.txt").write_text("real data")
        dest.mkdir()

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )

        assert rc == 0
        assert (dest / "keep.txt").read_text() == "real data"
        assert not (dest / trash_dir).exists()
```

- [ ] **Step 2:** Run → `.Trash-1001` FAILS (`dest/.Trash-1001` exists), `.Trash-1000` passes.
- [ ] **Step 3:** In `Options`, change the default to `[".Trash-*", ".cache", "unfinished_downloads"]` and the attribute doc to: `exclude_dirs: Names or rsync patterns skipped by default, at any depth. ".Trash-*" covers every user's desktop trash (freedesktop's ".Trash-$uid").`
- [ ] **Step 4:** Re-run → both PASS.
- [ ] **Step 5:** Gate, then commit `fix: exclude every user's desktop trash, not only uid 1000's`.

### Task 9: A corrupt snapshot refuses only its own drive

**Goal:** A malformed snapshot raises `SnapshotCorrupt` (not `SystemExit`); `_run_backup_for_endpoints` turns it into a logged refusal (exit 2) that leaves the destination and the corrupt file untouched; an `ALL` batch counts it as one error and continues. `snapshot_tree`'s missing/not-a-directory root raises `FileNotFoundError`/`NotADirectoryError`.

**Files:**
- Modify: `src/irsync/snapshot.py` (new `SnapshotCorrupt`; `read_snapshot`, `_validate_row`, `snapshot_tree` raises and docstrings)
- Modify: `src/irsync/backup.py` (import `SnapshotCorrupt`; new `except` after `SnapshotMismatch` in `_run_backup_for_endpoints`)
- Test: `tests/test_snapshot.py` (four `pytest.raises(SystemExit)` sites, plus two new tests)
- Test: `tests/test_backup.py` (new class `TestCorruptSnapshotRefusesOnlyThatDrive`)

**Acceptance Criteria:**
- [ ] `rg -n 'SystemExit' src` matches only `rsync_runner.py`'s cleanup comment and `__main__.py`.
- [ ] Single run with a corrupt snapshot: returns 2, logs an error naming the snapshot path and line, no rsync, dest unchanged, the corrupt file byte-identical.
- [ ] `ALL` with a corrupt snapshot on B and a healthy C: returns 1, C is backed up, summary says `backed up=1 (C)` and `errors=1`.
- [ ] Every new/changed test fails before the fix and passes after.

**Verify:** `pixi run python -m pytest tests/test_snapshot.py tests/test_backup.py -q -k "Corrupt or rejects or missing_root or not_a_directory or malformed or corrupted"` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests.**

In `tests/test_snapshot.py`, import `SnapshotCorrupt` from `irsync.snapshot` and change:
  - `test_missing_root_raises` → `with pytest.raises(FileNotFoundError, match="does not exist"):` (bug caught: a library raising `SystemExit` for a missing root).
  - `test_read_rejects_missing_keys` → `with pytest.raises(SnapshotCorrupt, match="Missing key 'type'"):` (the row has `dev` and `ino`, so `type` is the first missing key).
  - `test_7th_m2_read_snapshot_rejects_invalid_type_value` → `with pytest.raises(SnapshotCorrupt, match="Invalid type 'q'"):`.
  - `test_10th_f4_corrupted_row_reports_actual_file_line_number` → `with pytest.raises(SnapshotCorrupt, match=":3:"):`.

Add to `TestSnapshotTree`:

```python
    def test_root_that_is_a_file_raises_not_a_directory(self, tmp_path):
        # Bug caught: the not-a-directory branch left raising SystemExit
        # (a separate raise site from the missing-root one).
        a_file = tmp_path / "plain.txt"
        a_file.write_text("x")
        with pytest.raises(NotADirectoryError, match="not a directory"):
            snapshot_tree(a_file)
```

Add to `TestWriteReadRoundtrip`:

```python
    def test_malformed_first_line_is_corrupt(self, tmp_path):
        # Bug caught: the first-line parse (header or legacy row) is its own
        # raise site; leaving it as SystemExit would still kill an ALL run.
        out = tmp_path / "bad_first.jsonl"
        out.write_text("{not json\n", encoding="utf-8")
        with pytest.raises(SnapshotCorrupt, match=":1:"):
            read_snapshot(out, expected_source_root=tmp_path)
```

Append to `tests/test_backup.py` (add `import json` and `SNAPSHOT_FILENAME` / `EXIT_REFUSED` / `run_all_backups` imports if absent):

```python
class TestCorruptSnapshotRefusesOnlyThatDrive:
    """A corrupt baseline refuses its own run; it must not take down an ALL batch."""

    @staticmethod
    def _corrupt_snapshot(src: Path) -> Path:
        """Write a snapshot whose header is valid and whose line 2 is not JSON."""
        snap = src / SNAPSHOT_FILENAME
        header = json.dumps(
            {
                "_meta": {
                    "source_root": str(src.resolve()),
                    "irsync_version": "x",
                    "created_at_utc": "x",
                }
            }
        )
        snap.write_text(header + "\nTHIS IS NOT JSON\n", encoding="utf-8")
        return snap

    def test_single_run_refuses_without_touching_destination(
        self, src_dest, basic_options, monkeypatch, caplog
    ):
        # Behavior: a corrupt baseline is a logged refusal (exit 2); nothing
        # is transferred and the corrupt file is kept for the user.
        # Bug caught: read_snapshot raised SystemExit, which escaped as a
        # bare stderr message with exit 1 — bypassing logging (a log-to-file
        # cron run recorded nothing) and the refusal exit code.
        src, dest = src_dest
        snap = self._corrupt_snapshot(src)
        corrupt_bytes = snap.read_bytes()
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: pytest.fail("rsync ran")
        )

        with caplog.at_level(logging.ERROR):
            rc = run_backup(
                source_arg=str(src),
                destination_arg=str(dest),
                options=basic_options,
                args=_args(),
            )

        assert rc == EXIT_REFUSED
        assert f"{snap.resolve()}:2:" in caplog.text
        assert list(dest.iterdir()) == []
        assert snap.read_bytes() == corrupt_bytes

    def test_all_run_counts_it_as_an_error_and_continues(
        self, make_options, monkeypatch, caplog
    ):
        # Behavior: in an ALL run, a drive with a corrupt baseline is one
        # counted error; later drives are still backed up; exit 1.
        # Bug caught: SystemExit is not an Exception, so it escaped
        # run_all_backups' handlers and aborted the batch — C was never
        # backed up and no summary was logged.
        # Expected values: B is the only broken drive; C holds one file.
        options = make_options(backup_order=["B", "C"])
        base = options.base_dir
        b_src, c_src = base / "B", base / "C"
        for d in (b_src, base / "B_backup", c_src, base / "C_backup"):
            d.mkdir()
        (b_src / "b.txt").write_text("b")
        (c_src / "c.txt").write_text("c")
        self._corrupt_snapshot(b_src)
        monkeypatch.setattr("os.path.ismount", lambda p: True)

        with caplog.at_level(logging.INFO):
            rc = run_all_backups(options=options, args=_args())

        assert rc == 1
        assert (base / "C_backup" / "c.txt").read_text() == "c"
        assert list((base / "B_backup").iterdir()) == []
        summary = next(
            r.getMessage()
            for r in caplog.records
            if "missing/skipped" in r.getMessage()
        )
        assert "backed up=1 (C)" in summary
        assert "errors=1" in summary
```

- [ ] **Step 2:** Run the Verify command → every changed/new test FAILS (they see `SystemExit`, or for the ALL test the batch aborts).
- [ ] **Step 3: Implement.** In `snapshot.py` add after `SnapshotMismatch`:

```python
class SnapshotCorrupt(ValueError):
    """Raised when a snapshot file cannot be read as a valid snapshot.

    A malformed line, a row missing a required key, or an unknown type tag.
    The snapshot is refused whole (7th-M2) rather than partially trusted. A
    :class:`ValueError` subclass, so a caller that treats malformed input as
    a refusal (``cli.main``'s boundary) still handles it if nothing closer
    does.
    """
```

Replace the three `raise SystemExit(...)` in `read_snapshot`/`_validate_row` with `raise SnapshotCorrupt(...)` (same message text; keep `from e` where present). In `snapshot_tree`, raise `FileNotFoundError(f"Root path does not exist: {root}") from e` and `NotADirectoryError(f"Root path is not a directory: {root}")`. Update the three docstrings' Raises sections accordingly.

In `backup.py`, import `SnapshotCorrupt` and add after the `except SnapshotMismatch` block:

```python
        except SnapshotCorrupt as e:
            # 7th-M2 refuses a corrupt snapshot whole rather than diffing
            # against part of it. Refuse THIS run with a logged message and
            # a normal return code, so run_all_backups counts it as one
            # drive's error and moves on instead of the whole batch dying.
            logging.error(
                "Refusing: %s. The snapshot cannot be used as a diff baseline; "
                "move %s aside to treat the next run as a first backup.",
                e,
                before_path,
            )
            return EXIT_REFUSED
```

- [ ] **Step 4:** Re-run the Verify command → all PASS. Run the full suite.
- [ ] **Step 5:** Repro by hand (the audit's scenario) and confirm exit 1 with C backed up and a summary line.
- [ ] **Step 6:** Gate, then commit `fix: refuse a corrupt snapshot per drive instead of exiting the process`.

---

## Phase D — documentation (behavior-preserving)

### Task 10: Document the orchestrator and CLI entry points

**Goal:** `_run_backup_for_endpoints`, `_run_rsync_only`, and `_build_parser` have Google-style docstrings; `main` gains Args/Returns/Raises; `run_backup`'s Raises lists the `ValueError`/`FileNotFoundError`/`NotADirectoryError` that `run_all_backups` relies on.

**Files:**
- Modify: `src/irsync/backup.py` (`run_backup`, `_run_backup_for_endpoints`, `_run_rsync_only`)
- Modify: `src/irsync/cli.py` (`_build_parser`, `main`)

**Acceptance Criteria:**
- [ ] The AST probe from the audit lists no module-level function without a docstring in `backup.py`/`cli.py`.
- [ ] Docstrings only; no code change.

**Verify:** `pixi run python -I -c "import ast,pathlib;[print(f,n.name) for f in ('src/irsync/backup.py','src/irsync/cli.py') for n in ast.parse(pathlib.Path(f).read_text()).body if isinstance(n,ast.FunctionDef) and not ast.get_docstring(n)]"` → no output

**Steps:**

- [ ] **Step 1:** `_run_backup_for_endpoints`:

```python
    """Run the snapshot, diff, replay, rsync, persist pipeline for one pair.

    Called with the source lock already held (local source) or with no lock
    (remote source). The order of the steps is load-bearing — AD-2's
    no-change short-circuit, the refusal guards (AD-6, AD-25, AD-28),
    AD-22's deferred destination cleanup, and AD-15's persist order — so
    add new work inside the step it belongs to rather than reordering them.

    Args:
        endpoints: The resolved source/destination pair.
        options: Path knobs, including the default exclude list.
        args: Parsed CLI namespace.

    Returns:
        0 on success or when there was nothing to do,
        :data:`EXIT_REFUSED` when a guard refuses (including a corrupt
        snapshot), :data:`EXIT_ABORTED` when the user declines, otherwise
        rsync's own non-zero exit code.

    Raises:
        UnsafeDestination: If there is no usable baseline and the local
            destination cannot be listed.
        PermissionError: If a read or write is denied; :func:`run_backup`
            turns this into a refusal.
    """
```

- [ ] **Step 2:** `_run_rsync_only`:

```python
    """Run rsync directly, with no snapshot layer.

    Used for ``--no-snapshot`` and for a remote source. Always runs the
    dry-run first so there is a preview to show (NEW-M1), stops after it
    under ``--dry-run`` (hazard H, AD-30), and otherwise runs the real
    transfer.

    Args:
        src_str: Source endpoint, with its trailing slash.
        dest_str: Destination endpoint, with its trailing slash.
        options: Path knobs, including the default exclude list.
        args: Parsed CLI namespace.

    Returns:
        0 on success (including a completed ``--dry-run``),
        :data:`EXIT_ABORTED` when the user declines, otherwise the failing
        rsync run's exit code (:data:`EXIT_REFUSED` for a signal-killed
        preview).
    """
```

- [ ] **Step 3:** `run_backup`'s Raises: add, after `RsyncUnavailable`:

```
        ValueError: If the arguments cannot be resolved — an empty source,
            an unconfigured drive id or endpoint, a shorthand with no config
            loaded, remote-to-remote, or identical or nested paths (from
            :func:`~irsync.options.resolve_endpoints` /
            :func:`~irsync.options.resolve_source`).
        FileNotFoundError: If a local source, or a destination's parent,
            does not exist. :func:`run_all_backups` treats this as a skip.
        NotADirectoryError: If a local endpoint is not a directory. Also a
            skip in :func:`run_all_backups`.
```

- [ ] **Step 4:** `cli._build_parser`: `"""Build the irsync argument parser.\n\n    Returns:\n        The parser :func:`main` uses; built separately so tests can inspect\n        its help text without running a backup.\n    """`. `cli.main`:

```python
    """CLI entry point. Returns the process exit code.

    Args:
        argv: Arguments to parse; defaults to ``sys.argv[1:]``.

    Returns:
        0 on success or when only help was shown;
        :data:`~irsync.backup.EXIT_REFUSED` (2) for a refused or invalid
        invocation; :data:`~irsync.backup.EXIT_LOCK_HELD` (75) when another
        run holds the lock; :data:`~irsync.backup.EXIT_ABORTED` (130) when
        the user declines; 1 when an ``ALL`` run had a failing drive;
        otherwise rsync's own exit code.

    Raises:
        SystemExit: From argparse, for ``--help``, ``--version``, and
            invalid flags.
    """
```

- [ ] **Step 5:** Gate, then commit `docs: document the orchestrator and CLI entry points`.

### Task 11: Replace plan-relative references in code comments

**Goal:** Four comments that point at plan/branch context a reader cannot find from the code say what they mean instead.

**Files:**
- Modify: `src/irsync/options.py` (`needs_drive_config` docstring, "decision 1")
- Modify: `src/irsync/backup.py` (`_format_first_run_preview` comment "Task 3"; destination-gate comment "Rule 3"; `run_all_backups` docstring "this branch")

**Acceptance Criteria:**
- [ ] `rg -n 'decision 1|Task [0-9]|Rule [0-9]|this branch' src` returns nothing.

**Verify:** `rg -n 'decision 1|Task [0-9]|Rule [0-9]|this branch' src; echo "exit=$?"` → `exit=1`

**Steps:**

- [ ] **Step 1:** `options.py`: "That is the form decision 1 protects (``irsync SRC DEST``), and it also covers" → "That is the plain ``irsync SRC DEST`` form, which must keep working on a machine with no config file at all, and it also covers".
- [ ] **Step 2:** `backup.py` `_format_first_run_preview`: "(Task 3 only counts a local dest's existing entries)" → "(the destination gate only lists a local destination's entries)".
- [ ] **Step 3:** destination-gate comment: "the first-run preview (Rule 3) reports it" → "the first-run preview (``_format_first_run_preview``) reports it".
- [ ] **Step 4:** `run_all_backups` docstring: "which is exactly the silent-success hazard this branch exists to close" → "which is exactly the silent success the destination mount gate exists to prevent (AD-29)".
- [ ] **Step 5:** Gate, then commit `docs: replace plan-relative references with self-contained wording`.

### Task 12: Correct the ext4 btime condition in statx's docstring

**Goal:** `statx.py` says ext4 lacks btime when its inodes are 128 bytes (no room for `i_crtime`; ext4 only reports it when the field fits in the inode), not "without `-O extent`".

**Files:**
- Modify: `src/irsync/statx.py` (module docstring, "Fallback behaviour")

**Acceptance Criteria:**
- [ ] `rg -n 'extent' src/irsync/statx.py` returns nothing; the text names 128-byte inodes.

**Verify:** `rg -n '128-byte' src/irsync/statx.py` → one match

**Steps:**

- [ ] **Step 1:** "filesystem doesn't report btime (older ext4 without ``-O extent``, FAT, NFSv3, etc.)" → "filesystem doesn't report btime (ext4 with 128-byte inodes — e.g. upgraded from ext3 — which have no room for the creation time; FAT; NFSv3; etc.)".
- [ ] **Step 2:** Gate, then commit `docs: correct the ext4 condition for missing btime`.

### Task 13: Refresh the architecture doc's package-layout tables

**Goal:** The `src/irsync/` and `tests/` tables in `docs/design/irsync-architecture.md` match the code after mount-safety, drivecfg, and this pass.

**Files:**
- Modify: `docs/design/irsync-architecture.md` (section "Package layout")

**Acceptance Criteria:**
- [ ] A `preflight.py` row exists; the `options.py` row mentions `drivecfg`, `needs_drive_config`, `resolve_source` and no "drive-letter map"; the `cli.py` row describes lazy config loading and both parse-time rejections.
- [ ] `snapshot.py` row mentions `ROW_TYPES`, `is_reserved_root_name`, `SnapshotCorrupt`; `backup.py` row mentions the refusal guards and `_emit_preview`.
- [ ] The tests table lists `test_preflight.py` and `test_main.py`, and `conftest.py`'s row lists `make_tree`, `make_options`, `basic_options`, `write_drive_config`, `cli_args`, `subprocess_pythonpath`, `tree_signature`, `inode_for`.

**Verify:** `rg -c 'preflight.py|test_main.py|drivecfg' docs/design/irsync-architecture.md` → ≥ 3

**Steps:**

- [ ] **Step 1:** Rewrite the `cli.py`, `options.py`, `snapshot.py`, `backup.py` rows and add a `preflight.py` row, using the wording in the audit's doc-drift finding and the current code (read each module's top-level names before writing).
- [ ] **Step 2:** Update the tests table (add the two files, fix the `conftest.py` and `test_options.py` rows; `test_cli.py` row: end-to-end `python -m irsync` runs plus in-process `main` calls covering flag validation, lazy config loading, and help text). Leave the dated "Total: 168 tests (as of the pass-10 migration ...)" sentence — it is self-dated.
- [ ] **Step 3:** Gate, then commit `docs: bring the architecture doc's package tables up to date`.

### Task 14: Record the drivecfg decision as AD-31

**Goal:** The architecture doc, which claims to hold every decision, records where the drive layout comes from and when it is loaded.

**Files:**
- Modify: `docs/design/irsync-architecture.md` (new `### 31.` after `### 30.`)

**Acceptance Criteria:**
- [ ] `### 31.` exists, covering: layout from `drivecfg.DriveConfig`; `needs_drive_config`'s rules decided from argument text alone; shorthands fail closed with exit 2; `base_dir` default is only the mount-gate root; explicit config (presence, not truthiness) must exist; broken config always refuses; the known duplicated predicate in `cli.main`.

**Verify:** `rg -n '^### 31\.' docs/design/irsync-architecture.md` → one match

**Steps:**

- [ ] **Step 1:** Add:

```markdown
### 31. The drive layout comes from drivecfg, loaded only when the arguments need it

irsync ships no machine's storage layout. Drive ids, named endpoints (`~`,
`mypython`, …) and the `ALL` backup order come from a `drivecfg.DriveConfig`
(`Options.from_drive_config`; `Options.all_backups` is the config's
`backup_order`). The config is loaded lazily, and `needs_drive_config`
(`options.py`) decides from the argument text alone — never from what exists
in the working directory, so the same command always means the same thing —
whether an invocation needs one: `ALL` and a single-letter drive id always
do; any source paired with a destination never does, which keeps
`irsync SRC DEST` (and `irsync '~' /backup`, expanded by `Path.expanduser`)
working on a machine with no `drives.toml`; a source with no destination
does, because it can only be a configured name; and `--snapshot-only` relaxes
that last rule, consulting a config that exists but otherwise treating the
argument as a literal path (`resolve_source`). Every shorthand fails closed
with exit 2 — an unconfigured drive id is refused, never guessed under
`/media/$USER`. `base_dir` keeps its `/media/$USER` default only because it
is the mount-gate root (AD-24): it can refuse a run but never redirect one. A
config named explicitly — `--config`, or `$DRIVECFG_CONFIG`, decided on
presence rather than truthiness so a set-but-empty variable is refused as
drivecfg intends — must exist; only *discovery* coming up empty is tolerated,
and only under `--snapshot-only`. A config that exists but is broken always
refuses. Known wart: `cli.main` computes a second, inline predicate for the
`--snapshot-only` "consult if present" case (see `PROGRESS.md`, Open
questions).
```

- [ ] **Step 2:** Gate, then commit `docs: record the drivecfg layout decision as AD-31`.

### Task 15: One line per AD in PROGRESS.md, through AD-31

**Goal:** `PROGRESS.md`'s Cross-cutting decisions renders one bullet per AD and covers AD-24 to AD-31.

**Files:**
- Modify: `PROGRESS.md` (Cross-cutting decisions; "all 30 decisions" → "all 31")

**Acceptance Criteria:**
- [ ] 31 lines start with `- **AD-`; no line holds two.
- [ ] Current work says "all 31 decisions".

**Verify:** `rg -c '^- \*\*AD-' PROGRESS.md` → `31`

**Steps:**

- [ ] **Step 1:** Split the single AD line into 23 lines (text unchanged) and append:

```markdown
- **AD-24** — Mount gate checks the first component below `base_dir` (never `base_dir` itself) and runs before the lock, so nothing is written to an unmounted source.
- **AD-25** — Destination gate fires only with no usable baseline and only for a local dest; a remote dest is reported "not checked"; `UnsafeDestination` is an ALL error, not a skip.
- **AD-26** — One override per guard: `--force` (no-change short-circuit) vs `--allow-massive-delete` (>50% guard); both rejected with `--no-snapshot`.
- **AD-27** — Nested-mount skips produce one aggregated warning per walk (first five paths, then "(and N more)").
- **AD-28** — An empty source with no baseline is refused whatever the dest holds (`--allow-empty-source`); not `--dry-run`-exempt.
- **AD-29** — `check_mounted(role=...)`: an unmounted source is an ALL skip, an unmounted destination an ALL error.
- **AD-30** — `--no-snapshot --dry-run` honors `--dry-run` (hazard H).
- **AD-31** — Drive layout comes from drivecfg, loaded only when `needs_drive_config` says the arguments need it; every shorthand fails closed.
```

- [ ] **Step 2:** Gate, then commit `docs: list one AD per line in PROGRESS.md, through AD-31`.

### Task 16: Correct the 2026-07-31 hygiene-notes entry in place

**Goal:** The entry's "truncation branch is untested" claim (false since `b342f60`) and its drifted `backup.py:NNN` references are corrected.

**Files:**
- Modify: `docs/hygiene-notes.md` (2026-07-31 entry only)

**Acceptance Criteria:**
- [ ] The truncation bullet is marked corrected, names `test_walk_reports_truncated_count_past_five_skipped_dirs` and `b342f60`, and keeps the still-true `include_other=True` + boundary gap.
- [ ] `backup.py:665`, `backup.py:640`, `backup.py:557` no longer appear (the `backup.py:696` mention in the 2026-08-01 entry describes history and stays).

**Verify:** `rg -n 'backup\.py:(665|640|557)' docs/hygiene-notes.md; echo "exit=$?"` → `exit=1`

**Steps:**

- [ ] **Step 1:** Replace the truncation bullet with:

```markdown
- ~~**The `(and N more)` truncation branch of the nested-mount warning** (more
  than 5 skipped paths) is untested.~~ **Corrected 2026-10-18:** covered since
  `b342f60` (21 minutes after this entry was written) by
  `test_walk_reports_truncated_count_past_five_skipped_dirs`. Still true:
  `include_other=True` combined with a crossed filesystem boundary has no
  regression test.
```

- [ ] **Step 2:** In the dry-run carve-out bullet drop "(`backup.py:665`)" and "(`backup.py:640`)"; in M2 change "at `backup.py:557`" to "in `_run_backup_for_endpoints`' `--no-snapshot` branch"; in M3 change "at `backup.py:665`" to "in `_run_backup_for_endpoints`".
- [ ] **Step 3:** Gate, then commit `docs: correct the 2026-07-31 hygiene notes against the current code`.

### Task 17: Fix drifted references in test comments

**Goal:** Test comments stop citing line numbers that moved and a "16 configured entries" layout that no longer exists.

**Files:**
- Modify: `tests/test_diff.py` (comment in `test_8th_type_change_at_same_path_does_not_silently_lose_path`)
- Modify: `tests/test_backup.py` (comments in `test_5th_snapshot_not_persisted_when_rsync_fails`, `TestNoSnapshotDryRunHonored` docstring, `test_missing_drive_is_skipped_without_failing_the_run`)

**Acceptance Criteria:**
- [ ] `rg -n '\.py:\d' tests` matches only the integration test's reference `test_backup.py:test_5th_m1_...` (a test name, not a line).
- [ ] `rg -n '16 configured' tests` returns nothing.

**Verify:** `rg -n '\b(backup|diff)\.py:\d+' tests; echo "exit=$?"` → `exit=1`

**Steps:**

- [ ] **Step 1:** `test_diff.py`: "the code at diff.py:154-161 defensively logs" → "compute_moves' type-change branch defensively logs".
- [ ] **Step 2:** `test_backup.py`: "Verifies AD-equivalent: backup.py:401-406 guarantees that _persist_snapshots runs only after rsync returns 0." → "Verifies that _run_backup_for_endpoints calls _persist_snapshots only after run_real_sync returns 0."; "(``backup.py:696``)" → "(the ``if args.dry_run:`` block in ``_run_backup_for_endpoints``)"; "Most of the 16 configured entries are external drives" → "Most entries in a configured backup order are external drives".
- [ ] **Step 3:** Gate, then commit `test: anchor drifted comment references to symbols`.

### Task 18: Bring RELEASING.md up to date

**Goal:** RELEASING.md stops presenting the done trusted-publisher setup as pending and stops describing a conda-forge feedstock that does not exist.

**Files:**
- Modify: `RELEASING.md` (sections "One-time setup: PyPI Trusted Publisher" and "conda-forge")

**Acceptance Criteria:**
- [ ] The trusted-publisher section says it was done on 2026-09-06 and later releases need no PyPI-side setup.
- [ ] The conda-forge section says irsync is deliberately not on conda-forge because `drivecfg` isn't, and when to revisit.

**Verify:** `rg -n '2026-09-06|not on conda-forge' RELEASING.md` → ≥ 2 matches

**Steps:**

- [ ] **Step 1:** Heading → `## One-time setup: PyPI Trusted Publisher (done)`; first sentence → "Done on 2026-09-06, before v0.1.0: the trusted publisher is live, so later releases need no PyPI-side setup. Kept for reference (a fork, or a renamed project, would repeat it) — the registration used:"; keep the numbered list.
- [ ] **Step 2:** Replace the conda-forge section body with: "irsync is deliberately not on conda-forge (decided 2026-09-06). It depends on `drivecfg`, which has no conda-forge package, so submitting irsync would also mean writing and co-maintaining a `drivecfg` recipe. Revisit once `drivecfg` is on conda-forge: the recipe then goes to [conda-forge/staged-recipes](https://github.com/conda-forge/staged-recipes), and once the feedstock exists its autotick bot opens a PR for each new PyPI version."
- [ ] **Step 3:** Gate, then commit `docs: update RELEASING.md for the live publisher and the conda-forge decision`.

---

## Wrap-up

### Task 19: Close the audit record

**Goal:** The 2026-10-18 hygiene-notes entry lists what was applied (with commit hashes), what was kept and why, and the final counts; `PROGRESS.md` returns to maintenance with a correct next action; this plan's checkboxes and `.tasks.json` are complete.

**Files:**
- Modify: `docs/hygiene-notes.md` (2026-10-18 entry)
- Modify: `PROGRESS.md` (Current work)
- Modify: `docs/superpowers/plans/2026-10-18-hygiene-pass.md` and its `.tasks.json`

**Acceptance Criteria:**
- [ ] The entry has an "Applied" list naming every commit from Tasks 1–18 by hash, a "Bugs fixed" list, the kept items, and the before/after test counts as reported by pytest.
- [ ] Every path, symbol, and hash in the entry is re-checked against the repo after writing (`git show --stat <hash>` for each hash; `rg` for each symbol).
- [ ] `pixi run pre-commit run --all-files` and `pixi run test-cov` green; `git status --short` clean after the commit.

**Verify:** `pixi run test-cov 2>&1 | tail -3` → `N passed` with N = baseline + new tests, then `git status --short` → empty

**Steps:**

- [ ] **Step 1:** Fill in the entry's Applied / Bugs fixed / Kept sections and final counts from `git log --oneline` and the last pytest run.
- [ ] **Step 2:** `PROGRESS.md` Current work: Status back to maintenance; Next action "none pending" plus the deferred `unfinished_downloads` item and the open questions.
- [ ] **Step 3:** Tick every checkbox in this plan; set every task's status to `completed` in `.tasks.json`.
- [ ] **Step 4:** Re-read the entry against the repo (hashes, symbols, paths).
- [ ] **Step 5:** Gate, then commit `docs: close out the 2026-10-18 hygiene pass`.
