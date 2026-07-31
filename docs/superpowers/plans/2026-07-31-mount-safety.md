# Mount Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop irsync from treating a path's existence as proof of identity, so an unmounted drive can no longer wipe a backup or fill the root filesystem, and remove six adjacent silent surprises.

**Architecture:** A new `src/irsync/preflight.py` holds all identity and availability checks as small pure-ish functions with two typed exceptions. `backup.py` calls them at fixed points — critically, the mount gate runs *before* `_source_lock`, because locking creates files on the very disk the gate protects. `run_all_backups` catches the mount exception and records a skip, preserving its documented exit-code contract; `cli.main` gains an exception boundary that turns every operational failure into a logged refusal with exit code 2 instead of a traceback.

**Tech Stack:** Python 3.12+, stdlib only (`os.path.ismount`, `shutil.which`), pytest, ruff, mypy strict, pixi for all tooling.

**Global Constraints:**
- Every refusal returns the existing `EXIT_REFUSED` (2) from `irsync.backup`. No new exit codes.
- One override flag per guard. A flag must never disarm a guard it does not name.
- The mount gate MUST run before `_source_lock` in every code path that locks.
- `run_all_backups` MUST keep exit 0 when drives are merely absent (contract documented in commit `044c036`).
- All dev tooling runs through pixi: `pixi run python -m pytest`, `pixi run python -m mypy .`, `pixi run python -m ruff check .`.
- Follow the repo's TDD loop: failing test first, confirm the failure, minimal implementation, confirm pass, commit.
- Conventional Commits, one logical change per commit.

**User decisions (already made):**
- "Refuse; explicit flag to override" — fail closed is the default posture.
- "Mountpoint check for shorthand endpoints" — chosen over filesystem-UUID pinning and free-space heuristics.
- "Refuse if destination is non-empty" — chosen over a dry-run-and-count-deletions approach.
- "Separate purpose-named flags" — overrides are not folded into `--force`.
- "Clean split for `--force`" — no compatibility alias; `--force` loses its undocumented second meaning outright.
- Scope covers all seven hazards A–G from the spec.

**Spec:** `docs/superpowers/specs/2026-07-31-mount-safety-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `src/irsync/preflight.py` | **New.** Endpoint identity and tool-availability checks. Two typed exceptions plus `RsyncUnavailable`. No I/O beyond `stat`/`iterdir`/`which`. |
| `src/irsync/backup.py` | Calls the checks at fixed points; owns the refusal messages and exit codes. |
| `src/irsync/cli.py` | New flags; the exception boundary that keeps tracebacks off the terminal. |
| `src/irsync/snapshot.py` | Gains the nested-mount warning inside the existing walk. |
| `tests/test_preflight.py` | **New.** Unit tests for the check functions. |
| `tests/test_backup.py` | Wiring, refusal, and regression tests. |
| `tests/test_cli.py` | Flag parsing and flag-conflict tests (subprocess-based, matching the existing style). |
| `tests/test_snapshot.py` | Nested-mount boundary helper test. |

---

### Task 1: Preflight module — mount gate primitives

**Goal:** A new `preflight.py` exposing the typed exceptions and the mount-gate functions, fully unit-tested, wired to nothing yet.

**Files:**
- Create: `src/irsync/preflight.py`
- Test: `tests/test_preflight.py`

**Acceptance Criteria:**
- [ ] `mount_gate_root` returns the first path component below `base_dir`, or `None` for paths outside `base_dir` and for `base_dir` itself
- [ ] `check_mounted` returns silently for `str` endpoints (remotes) without touching the filesystem
- [ ] `check_mounted` raises `EndpointNotMounted` when the gate component is not a mountpoint
- [ ] `check_mounted` with `gate_outside_base=True` checks the endpoint itself when it is outside `base_dir`
- [ ] `mypy --strict` passes on the new module

**Verify:** `pixi run python -m pytest tests/test_preflight.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/test_preflight.py`:

```python
"""Unit tests for the pre-flight endpoint identity checks."""

import pytest

from irsync.preflight import (
    EndpointNotMounted,
    check_mounted,
    mount_gate_root,
)


class TestMountGateRoot:
    def test_drive_letter_endpoint_gates_on_itself(self, tmp_path):
        base = tmp_path / "media" / "u"
        assert mount_gate_root(base / "G", base) == base / "G"

    def test_subdirectory_gates_on_the_drive_above_it(self, tmp_path):
        # mypython resolves to a path INSIDE drive G, and the ~ backup lands
        # inside drive M. Gating on the endpoint itself would reject both.
        base = tmp_path / "media" / "u"
        deep = base / "G" / "Documents" / "Programming" / "python"
        assert mount_gate_root(deep, base) == base / "G"

    def test_path_outside_base_dir_is_exempt(self, tmp_path):
        base = tmp_path / "media" / "u"
        assert mount_gate_root(tmp_path / "home" / "u", base) is None

    def test_base_dir_itself_is_exempt(self, tmp_path):
        # /media/u is not a drive; there is no component below it to check.
        base = tmp_path / "media" / "u"
        assert mount_gate_root(base, base) is None


class TestCheckMounted:
    def test_remote_endpoint_is_exempt(self, tmp_path):
        # A remote endpoint is a str, not a Path, and has no local mount.
        check_mounted("host:/srv/data", tmp_path, gate_outside_base=True)

    def test_unmounted_drive_raises(self, tmp_path, monkeypatch):
        base = tmp_path / "media" / "u"
        drive = base / "G"
        drive.mkdir(parents=True)
        monkeypatch.setattr("os.path.ismount", lambda p: False)

        with pytest.raises(EndpointNotMounted) as excinfo:
            check_mounted(drive, base)

        assert str(drive) in str(excinfo.value)

    def test_mounted_drive_passes(self, tmp_path, monkeypatch):
        base = tmp_path / "media" / "u"
        drive = base / "G"
        drive.mkdir(parents=True)
        monkeypatch.setattr("os.path.ismount", lambda p: str(p) == str(drive))

        check_mounted(drive, base)

    def test_outside_base_is_exempt_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr("os.path.ismount", lambda p: False)
        check_mounted(tmp_path / "mnt" / "data", tmp_path / "media" / "u")

    def test_outside_base_checked_when_opted_in(self, tmp_path, monkeypatch):
        monkeypatch.setattr("os.path.ismount", lambda p: False)
        with pytest.raises(EndpointNotMounted):
            check_mounted(
                tmp_path / "mnt" / "data",
                tmp_path / "media" / "u",
                gate_outside_base=True,
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run python -m pytest tests/test_preflight.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'irsync.preflight'`

- [ ] **Step 3: Write the implementation**

Create `src/irsync/preflight.py`:

```python
"""Pre-flight checks that an endpoint is the thing the user meant.

irsync's guards all assume a path's existence proves its identity. That
assumption fails for removable drives: an unmounted drive under
``/media/<user>`` leaves an empty directory behind, which every downstream
check reads as a legitimate empty tree. These functions establish identity
before any file is created.
"""

from __future__ import annotations

import os
from pathlib import Path


class EndpointNotMounted(Exception):
    """An endpoint that should live on its own filesystem is not mounted.

    Callers that iterate several drives (``run_all_backups``) treat this the
    same as a missing drive: a skip, not an error.
    """


class UnsafeDestination(Exception):
    """A run with no baseline would write into a destination holding data."""


class RsyncUnavailable(Exception):
    """The rsync binary is not on PATH."""


def mount_gate_root(path: Path, base_dir: Path) -> Path | None:
    """Return the component below ``base_dir`` that must be a mountpoint.

    The shorthand destinations are subdirectories of a mounted drive rather
    than mountpoints themselves (``mypython`` resolves inside drive ``G``;
    the ``~`` backup lands inside drive ``M``), so the gate is applied to the
    first component below ``base_dir``, not to the endpoint.

    Args:
        path: The resolved endpoint.
        base_dir: The drive-letter base directory, e.g. ``/media/<user>``.

    Returns:
        The path that must be a mountpoint, or ``None`` when ``path`` is
        outside ``base_dir`` or is ``base_dir`` itself.
    """
    try:
        rel = path.relative_to(base_dir)
    except ValueError:
        return None
    if not rel.parts:
        return None
    return base_dir / rel.parts[0]


def check_mounted(
    endpoint: Path | str,
    base_dir: Path,
    *,
    gate_outside_base: bool = False,
) -> None:
    """Raise :class:`EndpointNotMounted` unless ``endpoint``'s drive is mounted.

    Args:
        endpoint: A resolved local path, or a string for an rsync remote.
            Remotes are always exempt — there is no local mount to check.
        base_dir: The drive-letter base directory.
        gate_outside_base: When True, endpoints outside ``base_dir`` must
            themselves be mountpoints (the ``--require-mount`` opt-in).

    Raises:
        EndpointNotMounted: If the gate path is not a mountpoint.
    """
    if not isinstance(endpoint, Path):
        return
    gate = mount_gate_root(endpoint, base_dir)
    if gate is None:
        if not gate_outside_base:
            return
        gate = endpoint
    if not os.path.ismount(gate):
        raise EndpointNotMounted(
            f"{gate} is not a mountpoint, so {endpoint} is not the drive it "
            "appears to be. The drive is probably not mounted."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run python -m pytest tests/test_preflight.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Type-check and commit**

```bash
pixi run python -m mypy .
pixi run python -m ruff check .
git add src/irsync/preflight.py tests/test_preflight.py
git commit -m "feat: add preflight mount-gate primitives"
```

---

### Task 2: Wire the mount gate into the backup flow

**Goal:** The mount gate runs before any file is created, `ALL` treats an unmounted drive as a skip, and `--allow-unmounted` / `--require-mount` control it.

**Files:**
- Modify: `src/irsync/backup.py` (`run_backup`, `_run_snapshot_only`, `run_all_backups`)
- Modify: `src/irsync/cli.py` (new flags)
- Modify: `tests/test_backup.py` (`_args` defaults, new tests)
- Modify: `tests/test_integration.py` (`_args` defaults only)

**Acceptance Criteria:**
- [ ] The gate runs after `resolve_endpoints` and BEFORE `_source_lock`, so an unmounted source never gets a `.irsync.lock` written to it
- [ ] `--allow-unmounted` bypasses the gate; `--require-mount` extends it outside `base_dir`
- [ ] `run_all_backups` records an unmounted drive as missing/skipped and still returns 0
- [ ] `_run_snapshot_only` performs the same check before its own lock

**Verify:** `pixi run python -m pytest tests/test_backup.py -v -k "MountGate or RunAllBackups"` → all pass

**Steps:**

- [ ] **Step 1: Extend both `_args` helpers**

In `tests/test_backup.py` and `tests/test_integration.py`, add four keys to the `defaults` dict in `_args` (both files, same four keys — the two helpers are separate copies):

```python
        allow_unmounted=False,
        require_mount=False,
        allow_nonempty_dest=False,
        allow_massive_delete=False,
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_backup.py`:

```python
class TestMountGate:
    def test_unmounted_source_refuses_and_writes_no_lockfile(
        self, src_dest, basic_options, monkeypatch
    ):
        # The gate must run before _source_lock: that function does
        # mkdir(exist_ok=True) and opens .irsync.lock for writing, which
        # would create files on the very disk the gate exists to protect.
        # run_backup RAISES here rather than returning a code: run_all_backups
        # needs the exception to tell "unmounted" apart from a real failure.
        # Task 5 adds the cli.main boundary that turns it into exit code 2 for
        # single-drive runs, and tests that separately.
        from irsync.preflight import EndpointNotMounted

        src, dest = src_dest
        monkeypatch.setattr("os.path.ismount", lambda p: False)
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: pytest.fail("rsync ran")
        )
        basic_options.base_dir = src.parent

        with pytest.raises(EndpointNotMounted):
            run_backup(
                source_arg=str(src),
                destination_arg=str(dest),
                options=basic_options,
                args=_args(),
            )

        assert not (src / LOCKFILE_NAME).exists(), (
            "the gate must run before the lock is taken"
        )

    def test_allow_unmounted_bypasses_the_gate(
        self, src_dest, basic_options, monkeypatch
    ):
        src, dest = src_dest
        monkeypatch.setattr("os.path.ismount", lambda p: False)
        basic_options.base_dir = src.parent

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(allow_unmounted=True),
        )
        assert rc == 0


class TestRunAllBackupsMountSkips:
    def test_unmounted_drive_counts_as_missing_not_error(
        self, basic_options, monkeypatch
    ):
        # Contract from commit 044c036: a drive that isn't there is a skip.
        # An unmounted drive is the same situation, so it must not flip the
        # exit code of an ALL run.
        from irsync.preflight import EndpointNotMounted

        def fake_backup(*, source_arg, destination_arg, options, args):
            if source_arg == "H":
                raise EndpointNotMounted("H is not a mountpoint")
            return 0

        monkeypatch.setattr("irsync.backup.run_backup", fake_backup)
        basic_options.all_backups = ["G", "H", "~"]

        rc = run_all_backups(options=basic_options, args=_args())
        assert rc == 0
```

- [ ] **Step 2a: Add the required imports to `tests/test_backup.py`**

`LOCKFILE_NAME` is already imported at the top of the file. No new imports are needed beyond the local `from irsync.preflight import EndpointNotMounted` shown inside the test above.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pixi run python -m pytest tests/test_backup.py -v -k "MountGate or RunAllBackupsMountSkips"`
Expected: FAIL — `test_unmounted_source_refuses_and_writes_no_lockfile` returns 0 instead of 2; the ALL test errors with an uncaught `EndpointNotMounted`.

- [ ] **Step 4: Add the flags in `src/irsync/cli.py`**

Insert after the existing `--no-exclude` argument:

```python
    parser.add_argument(
        "--allow-unmounted",
        action="store_true",
        help=(
            "Proceed even when a drive under the media base directory is not "
            "mounted. Without this, irsync refuses, because an unmounted drive "
            "looks like an empty tree and would wipe its own backup."
        ),
    )
    parser.add_argument(
        "--require-mount",
        action="store_true",
        help=(
            "Apply the mountpoint check to endpoints outside the media base "
            "directory too."
        ),
    )
```

- [ ] **Step 5: Wire the gate in `src/irsync/backup.py`**

Add to the imports:

```python
from irsync.preflight import EndpointNotMounted, check_mounted
```

In `run_backup`, immediately after `endpoints = resolve_endpoints(...)` and before the `src_root` assignment:

```python
    # The gate runs BEFORE _source_lock: that function mkdirs the source root
    # and opens .irsync.lock for writing, so locking an unmounted source
    # creates files on the disk the gate exists to protect.
    try:
        check_mounted(
            endpoints.source, options.base_dir, gate_outside_base=args.require_mount
        )
        check_mounted(
            endpoints.dest, options.base_dir, gate_outside_base=args.require_mount
        )
    except EndpointNotMounted:
        if not args.allow_unmounted:
            raise
        logging.warning("Proceeding past the mount check (--allow-unmounted).")
```

The exception propagates deliberately: `run_all_backups` catches it as a skip, and (from Task 5) `cli.main` turns it into a clean refusal for single-drive runs.

In `_run_snapshot_only`, after `src = ensure_local_dir(...)` resolution and before `with _source_lock(src)`:

```python
    try:
        check_mounted(src, options.base_dir)
    except EndpointNotMounted:
        if not args.allow_unmounted:
            raise
        logging.warning("Proceeding past the mount check (--allow-unmounted).")
```

`_run_snapshot_only` currently takes `source_arg` and `options` only — add an `args: argparse.Namespace` keyword parameter and pass `args` from its one call site in `run_backup`.

In `run_all_backups`, widen the existing except clause:

```python
        except (FileNotFoundError, NotADirectoryError, EndpointNotMounted) as e:
            logging.error("Skipping %r (missing/unmounted drive): %s", backup, e)
            missing.append(backup)
            continue
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pixi run python -m pytest tests/test_backup.py -v -k "MountGate or RunAllBackupsMountSkips"`
Expected: PASS

Then the full suite, since `_args` changed for every test:

Run: `pixi run python -m pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
pixi run python -m mypy .
git add src/irsync/backup.py src/irsync/cli.py tests/
git commit -m "feat: gate backups on the source and destination being mounted"
```

---

### Task 3: Destination gate — refuse a baseline-less run into a non-empty destination

**Goal:** A first-backup-shaped run refuses when the destination already holds data, closing hazard A for explicit-path runs that never touch `base_dir`.

**Files:**
- Modify: `src/irsync/preflight.py` (add `foreign_dest_entries`)
- Modify: `src/irsync/backup.py` (`_run_backup_for_endpoints`)
- Modify: `src/irsync/cli.py` (`--allow-nonempty-dest`)
- Modify: `tests/test_preflight.py`, `tests/test_backup.py`

**Acceptance Criteria:**
- [ ] `foreign_dest_entries` excludes irsync's reserved namespace, so an interrupted first backup can be retried without an override
- [ ] `foreign_dest_entries` raises `UnsafeDestination` when the destination cannot be read (fail closed — an unreadable destination is not a proven-empty one)
- [ ] A baseline-less run into a non-empty destination returns `EXIT_REFUSED` and leaves the destination byte-for-byte intact
- [ ] `--allow-nonempty-dest` permits the run

**Verify:** `pixi run python -m pytest tests/test_preflight.py tests/test_backup.py -v -k "Foreign or DestinationGate"` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_preflight.py`:

```python
from irsync.preflight import UnsafeDestination, foreign_dest_entries
from irsync.snapshot import LOCKFILE_NAME, SNAPSHOT_FILENAME


class TestForeignDestEntries:
    def test_empty_destination_returns_nothing(self, tmp_path):
        assert foreign_dest_entries(tmp_path) == []

    def test_irsync_reserved_files_do_not_count(self, tmp_path):
        # An interrupted first backup leaves a snapshot behind. Retrying must
        # not require an override.
        (tmp_path / SNAPSHOT_FILENAME).write_text("{}\n")
        (tmp_path / LOCKFILE_NAME).write_text("")
        (tmp_path / ".irsync-snap-abc123").write_text("")
        assert foreign_dest_entries(tmp_path) == []

    def test_user_data_counts(self, tmp_path):
        (tmp_path / "photos").mkdir()
        (tmp_path / "notes.txt").write_text("hi")
        assert foreign_dest_entries(tmp_path) == ["notes.txt", "photos"]

    def test_missing_destination_is_empty(self, tmp_path):
        assert foreign_dest_entries(tmp_path / "not_created_yet") == []

    def test_unreadable_destination_fails_closed(self, tmp_path):
        dest = tmp_path / "locked"
        dest.mkdir()
        dest.chmod(0o000)
        try:
            with pytest.raises(UnsafeDestination):
                foreign_dest_entries(dest)
        finally:
            dest.chmod(0o755)
```

Add to `tests/test_backup.py`:

```python
class TestDestinationGate:
    def test_first_backup_into_nonempty_dest_refuses_and_preserves_data(
        self, tmp_path, make_tree, basic_options, monkeypatch
    ):
        # Hazard A, the reproduction that motivated this work: an unmounted
        # source presents as an empty tree with no snapshot, and rsync's
        # --delete-before then empties the backup. Verified against the old
        # code: the file below was deleted and the run exited 0.
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        precious = dest / "old_backup.txt"
        precious.write_text("irreplaceable")
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: pytest.fail("rsync ran")
        )

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )

        assert rc == EXIT_REFUSED
        assert precious.read_text() == "irreplaceable"

    def test_allow_nonempty_dest_permits_adoption(
        self, tmp_path, make_tree, basic_options
    ):
        src = tmp_path / "src"
        make_tree(src, num_files=3, depth=1)
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "pre_existing.txt").write_text("adopt me")

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(allow_nonempty_dest=True),
        )
        assert rc == 0

    def test_empty_dest_first_backup_still_works(
        self, src_dest, basic_options
    ):
        # The gate must not break the ordinary first backup.
        src, dest = src_dest
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run python -m pytest tests/test_preflight.py tests/test_backup.py -v -k "Foreign or DestinationGate"`
Expected: FAIL — `ImportError` for `foreign_dest_entries`, and `test_first_backup_into_nonempty_dest_refuses_and_preserves_data` fails at `pytest.fail("rsync ran")`.

- [ ] **Step 3: Implement `foreign_dest_entries`**

Append to `src/irsync/preflight.py`:

```python
def foreign_dest_entries(dest_root: Path) -> list[str]:
    """Return destination-root entries that are not irsync's own files.

    irsync's reserved namespace is excluded so that a destination holding
    only a snapshot from an interrupted first backup still counts as empty
    and can be retried without an override.

    Args:
        dest_root: The destination root to inspect. A destination that does
            not exist yet counts as empty.

    Returns:
        Sorted names of entries that are not part of irsync's namespace.

    Raises:
        UnsafeDestination: If the destination exists but cannot be read. An
            unreadable destination is not a proven-empty one, so this fails
            closed rather than reporting no entries.
    """
    from irsync.snapshot import (
        LOCKFILE_NAME,
        SNAPSHOT_FILENAME,
        SNAPSHOT_TEMPFILE_PREFIX,
    )

    try:
        names = sorted(p.name for p in dest_root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    except PermissionError as e:
        raise UnsafeDestination(
            f"Cannot read destination {dest_root} to check whether it is "
            f"empty: {e}"
        ) from e
    return [
        name
        for name in names
        if name not in (SNAPSHOT_FILENAME, LOCKFILE_NAME)
        and not name.startswith(SNAPSHOT_TEMPFILE_PREFIX)
    ]
```

The import is function-local to avoid a circular import: `snapshot.py` does not import `preflight`, but keeping the dependency inside the function makes that ordering irrelevant.

- [ ] **Step 4: Add the flag in `src/irsync/cli.py`**

```python
    parser.add_argument(
        "--allow-nonempty-dest",
        action="store_true",
        help=(
            "Allow a first backup (no prior snapshot) into a destination that "
            "already contains files. Without this, irsync refuses, because "
            "rsync --delete-before would remove them."
        ),
    )
```

- [ ] **Step 5: Wire the gate in `_run_backup_for_endpoints`**

Add `foreign_dest_entries` and `UnsafeDestination` to the `irsync.preflight` import in `backup.py`. Then, in `_run_backup_for_endpoints`, insert immediately after the `if have_before:` block closes and before the preview section:

```python
    # No usable baseline: this run will transfer the whole source and delete
    # everything at the destination that is not on it. If the destination
    # already holds data, that is the unmounted-source scenario — refuse.
    foreign: list[str] = []
    if not have_before and dest_root is not None:
        foreign = foreign_dest_entries(dest_root)
        if foreign and not args.allow_nonempty_dest:
            shown = ", ".join(foreign[:10])
            more = f" (and {len(foreign) - 10} more)" if len(foreign) > 10 else ""
            logging.error(
                "Refusing: no prior snapshot, but the destination %s already "
                "contains %d entries: %s%s. rsync would DELETE them. This "
                "usually means the source drive is not mounted. Pass "
                "--allow-nonempty-dest to adopt this destination anyway.",
                dest_root,
                len(foreign),
                shown,
                more,
            )
            return EXIT_REFUSED
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pixi run python -m pytest tests/test_preflight.py tests/test_backup.py -v -k "Foreign or DestinationGate"`
Expected: PASS

Run: `pixi run python -m pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
pixi run python -m mypy .
git add src/irsync/preflight.py src/irsync/backup.py src/irsync/cli.py tests/
git commit -m "feat: refuse a baseline-less backup into a non-empty destination"
```

---

### Task 4: First-run preview

**Goal:** A run with no baseline shows what it is about to do before asking for confirmation, instead of prompting with nothing above it.

**Files:**
- Modify: `src/irsync/backup.py` (`_format_first_run_preview`, `_run_backup_for_endpoints`)
- Modify: `tests/test_backup.py`

**Acceptance Criteria:**
- [ ] The summary names the source, its entry count and total size, the destination, and its existing-entry count
- [ ] The summary states that non-matching destination content will be deleted
- [ ] Under `--yes` it reaches stdout (AD-7 cron audit trail); interactively it goes through the pager
- [ ] Counts come from `fresh_rows` and the Task 3 `iterdir` — no extra tree scan

**Verify:** `pixi run python -m pytest tests/test_backup.py -v -k FirstRunPreview` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
class TestFirstRunPreview:
    def test_first_backup_prints_a_summary_before_confirming(
        self, src_dest, basic_options, capsys
    ):
        # D: with no baseline, changes is None, so _show_preview was skipped
        # and the user confirmed a full transfer plus deletions blind.
        src, dest = src_dest

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "FIRST BACKUP" in out
        assert str(src) in out
        assert str(dest) in out
        assert "DELETE" in out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pixi run python -m pytest tests/test_backup.py -v -k FirstRunPreview`
Expected: FAIL — `assert "FIRST BACKUP" in out`, because nothing is printed.

- [ ] **Step 3: Implement the formatter**

Add to `src/irsync/backup.py`, next to `_format_preview`:

```python
def _format_first_run_preview(
    rows: list[Row],
    src_root: Path,
    dest_root: Path | None,
    foreign_count: int,
) -> str:
    """Summarize a backup that has no baseline to diff against.

    With no prior snapshot there is no move/delete plan to show, but the run
    is the most consequential one: it transfers the whole source and removes
    anything at the destination that is not on it. Report the scale of both
    so the confirmation prompt is not answered blind.
    """
    total_bytes = sum(r["size"] for r in rows if r["type"] == "f")
    gib = total_bytes / (1024**3)
    dest_desc = "n/a (remote)" if dest_root is None else str(dest_root)
    lines = [
        "=== FIRST BACKUP — no prior snapshot ===",
        f"Source:      {src_root}   {len(rows):,} entries, {gib:.1f} GiB",
        f"Destination: {dest_desc}   {foreign_count} existing entries",
        "rsync will transfer the source in full and DELETE anything at the",
        "destination that is not on the source.",
    ]
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Render it**

In `_run_backup_for_endpoints`, replace the existing preview block:

```python
    if changes is not None:
        _show_preview(changes, interactive=not args.yes)
```

with:

```python
    if changes is not None:
        _show_preview(changes, interactive=not args.yes)
    else:
        text = _format_first_run_preview(
            fresh_rows, src_root, dest_root, len(foreign)
        )
        if args.yes:
            print(text)
        else:
            _page_output(text)
```

`foreign` is in scope from Task 3 and is `[]` whenever the destination gate did not run.

- [ ] **Step 5: Run the test to verify it passes**

Run: `pixi run python -m pytest tests/test_backup.py -v -k FirstRunPreview`
Expected: PASS

Run: `pixi run python -m pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
pixi run python -m mypy .
git add src/irsync/backup.py tests/test_backup.py
git commit -m "feat: summarize the first backup before asking for confirmation"
```

---

### Task 5: Clean refusals instead of tracebacks

**Goal:** Missing rsync, a read-only source root, and a missing source path all produce a logged refusal and exit 2 rather than a stack trace.

**Files:**
- Modify: `src/irsync/preflight.py` (`check_rsync_available`)
- Modify: `src/irsync/backup.py` (`run_backup`, `_source_lock` call sites)
- Modify: `src/irsync/cli.py` (`main` exception boundary)
- Modify: `tests/test_preflight.py`, `tests/test_backup.py`

**Acceptance Criteria:**
- [ ] `shutil.which("rsync")` returning `None` produces `EXIT_REFUSED` with an actionable message
- [ ] A read-only source root produces `EXIT_REFUSED`, not `PermissionError`
- [ ] A missing source path in a single-drive run produces `EXIT_REFUSED`, not a traceback (verified as broken today: `FileNotFoundError` escapes `cli.main` and exits 1)
- [ ] `run_all_backups` still catches `FileNotFoundError` first, so `ALL` keeps skipping absent drives

**Verify:** `pixi run python -m pytest tests/test_backup.py tests/test_preflight.py -v -k "CleanRefusal or RsyncAvailable"` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_preflight.py`:

```python
from irsync.preflight import RsyncUnavailable, check_rsync_available


class TestRsyncAvailable:
    def test_missing_rsync_raises(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        with pytest.raises(RsyncUnavailable) as excinfo:
            check_rsync_available()
        assert "rsync" in str(excinfo.value)

    def test_present_rsync_passes(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/rsync")
        check_rsync_available()
```

Add to `tests/test_backup.py`:

```python
class TestCleanRefusals:
    def test_missing_rsync_binary_refuses(
        self, src_dest, basic_options, monkeypatch
    ):
        src, dest = src_dest
        monkeypatch.setattr("shutil.which", lambda name: None)

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == EXIT_REFUSED

    def test_read_only_source_root_refuses_without_traceback(
        self, src_dest, basic_options
    ):
        # Reproduced against the old code: PermissionError from _source_lock
        # opening .irsync.lock escaped as a stack trace.
        src, dest = src_dest
        src.chmod(0o555)
        try:
            rc = run_backup(
                source_arg=str(src),
                destination_arg=str(dest),
                options=basic_options,
                args=_args(),
            )
        finally:
            src.chmod(0o755)
        assert rc == EXIT_REFUSED
```

Add to `tests/test_cli.py`, matching the file's subprocess style:

```python
def test_missing_source_path_exits_cleanly(tmp_path):
    # Reproduced against the old code: FileNotFoundError escaped cli.main as
    # a traceback and the exit code was flattened to 1.
    dest = tmp_path / "dest"
    dest.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "irsync",
            str(tmp_path / "definitely_missing"),
            str(dest),
            "--yes",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=ENV,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run python -m pytest tests/test_preflight.py tests/test_backup.py tests/test_cli.py -v -k "CleanRefusal or RsyncAvailable or missing_source_path"`
Expected: FAIL — `ImportError` for `check_rsync_available`; `PermissionError` raised; the CLI test shows returncode 1 with a traceback.

- [ ] **Step 3: Implement `check_rsync_available`**

Append to `src/irsync/preflight.py` (and add `import shutil` at the top):

```python
def check_rsync_available() -> None:
    """Raise :class:`RsyncUnavailable` if the rsync binary is not on PATH.

    Raises:
        RsyncUnavailable: If ``shutil.which`` cannot find rsync.
    """
    if shutil.which("rsync") is None:
        raise RsyncUnavailable(
            "rsync was not found on PATH. Install it (on Debian/Ubuntu: "
            "sudo apt install rsync) and try again."
        )
```

- [ ] **Step 4: Call it and convert `PermissionError` in `backup.py`**

Add `check_rsync_available` and `RsyncUnavailable` to the `irsync.preflight` import.

In `run_backup`, make `check_rsync_available()` the first statement of the function body (before `resolve_endpoints`).

Wrap the two `_source_lock` uses. In `run_backup`:

```python
    try:
        with _source_lock(src_root):
            return _run_backup_for_endpoints(
                endpoints=endpoints, options=options, args=args
            )
    except BlockingIOError:
        return _log_lock_conflict(src_root)
    except PermissionError as e:
        logging.error(
            "Cannot write to the source root %s (%s). irsync needs to create "
            "its lockfile and snapshot there.",
            src_root,
            e,
        )
        return EXIT_REFUSED
```

Apply the same `except PermissionError` handler to the `with _source_lock(src)` block in `_run_snapshot_only`.

- [ ] **Step 5: Add the exception boundary in `src/irsync/cli.py`**

Replace the two `return run_backup(...)` / `return run_all_backups(...)` statements at the end of `main` with:

```python
    from irsync.preflight import (
        EndpointNotMounted,
        RsyncUnavailable,
        UnsafeDestination,
    )
    from irsync.backup import EXIT_REFUSED

    try:
        if args.source_arg.strip().upper() == "ALL":
            if args.destination_arg:
                parser.error("Destination is not allowed when SOURCE is 'ALL'.")
            return run_all_backups(options=options, args=args)
        return run_backup(
            source_arg=args.source_arg,
            destination_arg=args.destination_arg,
            options=options,
            args=args,
        )
    except (
        EndpointNotMounted,
        UnsafeDestination,
        RsyncUnavailable,
        FileNotFoundError,
        NotADirectoryError,
        ValueError,
    ) as e:
        logging.error("%s", e)
        return EXIT_REFUSED
```

Keep the existing `ALL` handling inside the `try` exactly as shown so the destination check still runs. The `EndpointNotMounted` catch is what turns Task 2's deliberate propagation into a clean single-drive refusal.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pixi run python -m pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
pixi run python -m mypy .
git add src/irsync/preflight.py src/irsync/backup.py src/irsync/cli.py tests/
git commit -m "fix: turn operational failures into refusals instead of tracebacks"
```

---

### Task 6: Warn when the walk skips a nested mount

**Goal:** A filesystem mounted inside the source is reported rather than silently excluded from the backup.

**Files:**
- Modify: `src/irsync/snapshot.py` (`snapshot_tree`, new `_crosses_boundary` helper)
- Modify: `tests/test_snapshot.py`

**Acceptance Criteria:**
- [ ] `_crosses_boundary` returns True only when `xdev` is on and the device differs
- [ ] `snapshot_tree` emits exactly one aggregated warning naming the count and the first few paths
- [ ] No warning when nothing is skipped
- [ ] `snapshot_tree`'s signature and return type are unchanged, so no caller or test churn

**Verify:** `pixi run python -m pytest tests/test_snapshot.py -v -k Boundary` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
class TestNestedMountBoundary:
    def test_helper_flags_only_cross_device_entries_when_xdev(self):
        from irsync.snapshot import _crosses_boundary

        assert _crosses_boundary(entry_dev=42, root_dev=7, xdev=True) is True
        assert _crosses_boundary(entry_dev=7, root_dev=7, xdev=True) is False
        # With xdev off the walk descends everywhere, so nothing is skipped.
        assert _crosses_boundary(entry_dev=42, root_dev=7, xdev=False) is False

    def test_walk_warns_once_when_a_boundary_is_skipped(
        self, tmp_path, monkeypatch, caplog
    ):
        # A real nested mount needs root, so drive the boundary decision
        # directly: report a foreign st_dev for one subdirectory.
        (tmp_path / "normal").mkdir()
        (tmp_path / "nested").mkdir()
        real_lstat = Path.lstat

        def fake_lstat(self):
            st = real_lstat(self)
            if self.name == "nested":
                return os.stat_result(
                    (st.st_mode, st.st_ino, st.st_dev + 1, st.st_nlink,
                     st.st_uid, st.st_gid, st.st_size,
                     int(st.st_atime), int(st.st_mtime), int(st.st_ctime))
                )
            return st

        monkeypatch.setattr(Path, "lstat", fake_lstat)

        with caplog.at_level(logging.WARNING):
            snapshot_tree(tmp_path)

        warnings = [r for r in caplog.records if "separate filesystem" in r.getMessage()]
        assert len(warnings) == 1
        assert "nested" in warnings[0].getMessage()
```

Add `import logging` and `import os` to `tests/test_snapshot.py` if not already present, and ensure `from pathlib import Path` is imported.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pixi run python -m pytest tests/test_snapshot.py -v -k Boundary`
Expected: FAIL — `ImportError: cannot import name '_crosses_boundary'`

- [ ] **Step 3: Implement**

Add `import logging` to `src/irsync/snapshot.py`, then:

```python
def _crosses_boundary(*, entry_dev: int, root_dev: int, xdev: bool) -> bool:
    """Return True if this entry sits on another filesystem and will be skipped.

    Extracted so the boundary decision is directly testable: creating a real
    nested mount requires privileges the test suite does not have.
    """
    return xdev and entry_dev != root_dev
```

In `snapshot_tree`, initialise `skipped_mounts: list[str] = []` beside `rows`, and replace the two `not xdev or st.st_dev == root_dev` conditions with the helper. Where a directory is skipped, record it:

```python
            crosses = _crosses_boundary(
                entry_dev=int(st.st_dev), root_dev=int(root_dev), xdev=xdev
            )
            if crosses:
                if is_dir and not is_symlink:
                    skipped_mounts.append(rel)
                continue
```

placed so it replaces the existing device guards on both the row append and the descend. After the `while stack:` loop ends, before `return rows`:

```python
    if skipped_mounts:
        shown = ", ".join(sorted(skipped_mounts)[:5])
        more = (
            f" (and {len(skipped_mounts) - 5} more)"
            if len(skipped_mounts) > 5
            else ""
        )
        logging.warning(
            "Skipped %d path(s) on a separate filesystem; they are NOT backed "
            "up (rsync runs with --one-file-system too): %s%s",
            len(skipped_mounts),
            shown,
            more,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run python -m pytest tests/test_snapshot.py -v`
Expected: PASS (including the existing walk tests, which must be unaffected)

- [ ] **Step 5: Commit**

```bash
pixi run python -m mypy .
git add src/irsync/snapshot.py tests/test_snapshot.py
git commit -m "feat: warn when the snapshot walk skips a nested filesystem"
```

---

### Task 7: Split `--force`

**Goal:** `--force` means only "run even though nothing changed"; bypassing the catastrophic-delete guard requires `--allow-massive-delete`.

**Files:**
- Modify: `src/irsync/backup.py:398` (the guard condition)
- Modify: `src/irsync/cli.py` (new flag, conflict check)
- Modify: `tests/test_backup.py` (`TestCatastrophicDiffSanityCheck`)
- Modify: `tests/test_cli.py` (conflict test)

**Acceptance Criteria:**
- [ ] `--force` alone no longer bypasses the >50% deletion refusal
- [ ] `--allow-massive-delete` bypasses it
- [ ] `--force` still defeats the no-change short-circuit
- [ ] `--allow-massive-delete` with `--no-snapshot` is rejected at parse time, matching the existing `--force` rule

**Verify:** `pixi run python -m pytest tests/test_backup.py tests/test_cli.py -v -k "Catastrophic or Force or massive"` → all pass

**Steps:**

- [ ] **Step 1: Update the existing tests to the new behavior**

In `tests/test_backup.py`, rename `test_majority_deletion_proceeds_with_force` to `test_majority_deletion_proceeds_with_allow_massive_delete` and change its final `run_backup` call from `args=_args(force=True)` to `args=_args(allow_massive_delete=True)`.

Then add, in the same class:

```python
    def test_force_alone_no_longer_bypasses_the_guard(
        self, src_dest, basic_options, monkeypatch
    ):
        # G: --force used to disarm this guard as an undocumented side
        # effect, so a cron line carrying --force for the no-change
        # short-circuit silently lost the protection.
        src, dest = src_dest
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        for child in src.iterdir():
            if child.name != SNAPSHOT_FILENAME:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: calls.append(cmd) or 0
        )

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(force=True),
        )
        assert rc == EXIT_REFUSED
        assert calls == [], "rsync must not run when the guard fires"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run python -m pytest tests/test_backup.py -v -k Catastrophic`
Expected: FAIL — `test_force_alone_no_longer_bypasses_the_guard` gets 0 because `--force` still disarms the guard; the renamed test fails because `allow_massive_delete` is not consulted.

- [ ] **Step 3: Change the guard condition**

In `src/irsync/backup.py`, change:

```python
        if before_rows and not args.force:
```

to:

```python
        if before_rows and not args.allow_massive_delete:
```

and update the guard's message so it names the new flag:

```python
                    "args were reversed. Pass --allow-massive-delete to override.",
```

Leave the no-change short-circuit at `backup.py:392` on `args.force` — that is `--force`'s legitimate meaning.

- [ ] **Step 4: Add the flag and the conflict check in `src/irsync/cli.py`**

```python
    parser.add_argument(
        "--allow-massive-delete",
        action="store_true",
        help=(
            "Bypass the refusal when the diff would delete more than half of "
            "the previously-recorded entries."
        ),
    )
```

Extend the existing `--no-snapshot` conflict check:

```python
    if args.allow_massive_delete and args.no_snapshot:
        parser.error(
            "--allow-massive-delete has no effect with --no-snapshot (the "
            "snapshot diff is what it overrides). Drop one of them."
        )
```

- [ ] **Step 5: Add the CLI conflict test**

In `tests/test_cli.py`, copy `test_5th_m2_force_with_no_snapshot_rejected_at_parse_time` into a new `test_allow_massive_delete_with_no_snapshot_rejected`, substituting `--allow-massive-delete` for `--force` in both the argv list and the two assertions.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pixi run python -m pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
pixi run python -m mypy .
git add src/irsync/backup.py src/irsync/cli.py tests/
git commit -m "fix: split --force so it no longer disarms the deletion guard"
```

---

### Task 8: Documentation

**Goal:** README, architecture doc, and hygiene notes describe the new guards, so the next reader does not rediscover them from source.

**Files:**
- Modify: `README.md`
- Modify: `docs/design/irsync-architecture.md`
- Modify: `docs/hygiene-notes.md`

**Acceptance Criteria:**
- [ ] README documents all five flags and the fail-closed posture
- [ ] README's first-use guidance warns that an unmounted drive was the motivating hazard
- [ ] The architecture doc gains AD entries for the mount gate, the destination gate, and the `--force` split
- [ ] `docs/hygiene-notes.md` records that hazards A–G are now closed, and that F has no end-to-end test

**Verify:** `pixi run pre-commit run --all-files` → passes; `rg -c 'allow-unmounted|allow-nonempty-dest|allow-massive-delete' README.md` → 3 or more

**Steps:**

- [ ] **Step 1: Add a Safety section to `README.md`**

Insert after the "Drive-letter shorthand" paragraph:

```markdown
## Safety

irsync fails closed. It refuses, with exit code 2, when it cannot establish
that an endpoint is the drive you meant:

- **The drive is not mounted.** Any endpoint under the media base directory
  must sit on a mounted filesystem. An unmounted drive leaves an empty
  directory behind, which would otherwise read as a legitimate empty tree —
  and an empty *source* would delete the whole backup.
- **A first backup would destroy data.** With no prior snapshot, a
  destination that already contains files is refused, because
  `rsync --delete-before` would remove them.

| Flag | Effect |
|---|---|
| `--allow-unmounted` | proceed even if the drive is not mounted |
| `--allow-nonempty-dest` | adopt a destination that already holds files |
| `--allow-massive-delete` | proceed when the diff would delete >50% of recorded entries |
| `--require-mount` | apply the mount check outside the media base directory too |
| `--force` | run rsync even when nothing changed (nothing else) |

Each flag disarms exactly one guard, so a cron line cannot silently lose a
protection it did not name.
```

- [ ] **Step 2: Add AD entries to `docs/design/irsync-architecture.md`**

Append to the decisions list, following the existing `**AD-N** — ...` one-line style:

```
- **AD-24** — Endpoint identity is gated on mount state: any endpoint under `base_dir` must have its first component below `base_dir` be a mountpoint; the gate runs BEFORE `_source_lock`, which would otherwise create files on the unmounted disk.
- **AD-25** — A run with no usable baseline refuses when the destination holds entries outside irsync's reserved namespace; `run_all_backups` treats `EndpointNotMounted` as a skip, never an error.
- **AD-26** — `--force` means only "run despite no changes"; bypassing the >50% deletion guard requires `--allow-massive-delete`. One override per guard.
```

- [ ] **Step 3: Update `docs/hygiene-notes.md`**

Append a dated section recording that hazards A–G are closed, naming the commits, and restating that hazard F has no end-to-end test because creating a nested mount requires privileges the suite lacks.

- [ ] **Step 4: Verify and commit**

```bash
pixi run pre-commit run --all-files
git add README.md docs/
git commit -m "docs: document the mount and destination guards"
```

---

## Verification (whole plan)

After Task 8:

```bash
pixi run python -m ruff check .
pixi run python -m ruff format --check .
pixi run python -m mypy .
pixi run python -m pytest -q
```

Then re-run the original hazard reproduction by hand and confirm it now refuses:

```bash
mkdir -p /tmp/verify/src /tmp/verify/dest
echo precious > /tmp/verify/dest/old_backup.txt
irsync /tmp/verify/src /tmp/verify/dest --yes
```

Expected: exit code 2, a refusal naming `--allow-nonempty-dest`, and
`/tmp/verify/dest/old_backup.txt` still present. On the old code this printed
`*deleting old_backup.txt` and exited 0.
