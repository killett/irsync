# Ask the Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** irsync lists a move only when the backup can make it. Under a default-excluded name the local backup is asked what it holds before the diff, so a file is copied only when the backup lacks it; a remote destination, which can be neither examined nor renamed on, claims no moves.

**Architecture:** A `Destination` interface (`look`, `replay`) with a local implementation, and a pure `baseline` module that treats baseline rows under the default-excluded names as hints and replaces them by what the destination holds. `backup._plan_changes` puts that in front of the unchanged `compute_changes` for the three cases: a run with the excludes, a `--no-exclude` run, and a remote destination. The snapshot format and what is written to it do not change.

**Tech Stack:** Python 3.12+, pytest (the real rsync binary in end-to-end tests), ruff, mypy (strict), pixi, drivecfg.

**Spec:** `docs/superpowers/specs/2026-11-25-excluded-names-backup-truth-design.md`. Read it first: the "cases" and "sections" named below are its.

## Global Constraints

- **Branch.** Work on branch `ask-the-backup`, created in place from `main` with `git switch -c ask-the-backup`. No worktree: the pixi environment lives per checkout, and the owner chose a branch in `/workspace` for this repo. Do not push and do not merge; integrating the branch is the owner's call.
- **One task = one commit.** Conventional Commits, imperative mood. Every commit message ends with the trailer `Co-Authored-By: Claude <noreply@anthropic.com>`.
- **Gate before every commit**, and commit only if both are green: `pixi run pre-commit run --all-files` (ruff, ruff-format, mypy, file hooks) and `pixi run test-cov`. The ruff hooks rewrite files: re-stage what they touch and run the gate again until it is clean.
- **Stage files by name** (`git add <path> ...`), never `git add -A` or `git add .`; check `git status --short` before each commit.
- **Red, then green.** In each task write the tests first, run them, and see them fail for the reason the task states; then implement and see them pass. A test marked **guard** passes before the change by design (it pins behaviour that must not regress, or blocks a wrong fix): say so in your report instead of forcing it red.
- **The code blocks are complete and were run.** Every code block and diff in this plan was applied in a scratch checkout on 2026-11-25 (Linux, rsync 3.4.1, Python 3.13), task by task: after each task ruff, ruff-format and mypy were clean and the suite gave the pass count the task states. Copy them exactly. If a count differs, or a test from this plan fails after its implementation is in, stop and find out why; do not weaken an assertion or reword a string to get to green.
- **Diffs are against the file as the previous task left it.** `git apply` accepts each one; making the same edits by hand is equally fine. Line numbers in a hunk header are a guide only: find the place by the quoted lines.
- **Every user-visible string is fixed.** The notes, the refusals, the warning and the help sentence come from the spec and are asserted verbatim by tests. Do not reword one.
- **Tests state their purpose.** Each test opens with `# Behavior:` / `# Bug caught:` / `# Expected values:` comments, the repo's convention. Keep them when copying.
- **Baseline.** `pixi run test -q` on `main` ends with `431 passed, 1 skipped`. The one skip is the kernel inode-reuse test in `tests/test_diff.py`, which skips when the kernel does not hand the freed inode back within its budget (or the filesystem has no usable btime). When it runs instead, the count reads one higher with nothing skipped: `443 passed, 1 skipped` and `444 passed` are the same result, and so on for every count in this plan.
- **What a local run cannot show.** CI also runs on macOS, whose rsync is openrsync. Two things there are not verified by this plan and must be reported, not patched around, if CI disagrees: the `remote_host` fixture (it skips when rsync cannot be driven through the stand-in ssh), and `test_what_irsync_accepts_as_a_move_rsync_leaves_alone`, which encodes rsync's whole-second quick check.
- **Out of scope.** No version bump, no release, no replay over ssh (the next design), no change to the snapshot format or to what is persisted.
- **Running irsync by hand:** `PYTHONPATH=/workspace/src pixi run python -m irsync ...`.

**User decisions (already made):**
- Approach: "I think I want 1" — ask the backup (an `lstat`, rsync's own size-and-whole-second-mtime test), not a record in the snapshot.
- Rejected: diffing with the default excludes on both sides. "I don't want to copy files we don't have to! that's the whole point of irsync!"
- Order and scope: "A" — design the first option now with one destination interface, include the honesty fix for remote destinations, make replay over ssh the next design.
- Design sections 1 to 6: each approved ("y" / "yes"), including that a large rename to a remote destination is refused by the >50% guard until `--allow-massive-delete`, and that a lookup error refuses the run.
- The written spec: "no changes".

---

## File Structure

| File | Task | Responsibility |
|---|---|---|
| `src/irsync/diff.py` (modify) | 1 | `same_file`: the one identity check. `compute_changes(detect_moves=False)`: a path change reported as deleted and created. |
| `src/irsync/destination.py` (create) | 2 | What irsync may ask of a backup: `Destination` (protocol), `LocalDestination`, `Seen`, `DestinationUnreadable`. The only new I/O. |
| `src/irsync/baseline.py` (create) | 3 | What a baseline may say about the backup under the default-excluded names: `split_hints`, `holds`, `checked_rows`, `moved_out_candidates`. No I/O. |
| `src/irsync/backup.py` (modify) | 4, 5, 6 | `_plan_changes` builds the diff's inputs for the three cases; the preview notes; the refusal; the replay through the destination. |
| `src/irsync/cli.py` (modify) | 6 | The description says the replay needs a local destination. |
| `tests/test_diff.py` (modify) | 1 | `same_file`; the diff without move detection. |
| `tests/test_destination.py` (create) | 2 | `LocalDestination` on real files. |
| `tests/test_baseline.py` (create) | 3 | The spec's section 1 table and section 2 rules, on rows. |
| `tests/test_excluded_names.py` (create, extend) | 4, 5 | End to end with the real rsync, local destination. |
| `tests/test_remote_destination.py` (create) | 6 | End to end to a remote destination through a stand-in ssh. |
| `tests/conftest.py` (modify) | 6 | The `remote_host` fixture. |
| `tests/test_backup.py`, `tests/test_cli.py` (modify) | 4, 6 | Preview notes; three existing tests that follow the code; the help text. |
| `docs/design/irsync-architecture.md`, `README.md`, `PROGRESS.md`, the spec (modify) | 7 | AD-36, AD-37, AD-38 and what they amend. |

Tasks 1 and 2 are independent of each other. Task 3 needs both. Tasks 4, 5, 6 and 7 follow in order.

---

### Task 1: One identity check; a diff without move detection

**Goal:** `diff.same_file` is the one definition of "same file", and `compute_changes(detect_moves=False)` reports a path change as deleted and created.

**Files:**
- Modify: `src/irsync/diff.py` (new `same_file`; `compute_moves`; `compute_changes`)
- Test: `tests/test_diff.py` (the import list; two classes appended at the end)

**Acceptance Criteria:**
- [ ] `same_file(b, a)` is True only when size and `mtime_ns` match, neither `mtime_ns` is -1, and `btime_ns` matches when both are >= 0.
- [ ] `compute_moves` calls `same_file` and holds no second copy of that condition.
- [ ] `compute_changes(before, after, detect_moves=False)` returns no moves: a renamed file is in `deleted` and `created`, a renamed folder contributes every entry in it, and an in-place edit is still in `modified`.
- [ ] `compute_changes` called without `detect_moves` behaves as before: every test that existed before this task passes unmodified.
- [ ] `pixi run test -q` ends with `443 passed, 1 skipped`.

**Verify:** `pixi run test tests/test_diff.py -q` → no failures; then `pixi run test -q` → `443 passed, 1 skipped`

**What each test is for:**

| Test | Bug it would catch |
|---|---|
| `TestSameFile::test_identity_needs_size_mtime_and_btime_when_known` (8 cases) | Dropping any clause of the identity check would call a reused inode number a rename, and the replay would move the wrong file's backup copy. |
| `TestComputeChangesWithoutMoveDetection::test_a_renamed_file_is_deleted_and_created` | A switch that only hid the move lists would leave a renamed file out of every list, and the run could stop at "no source changes". |
| `...::test_a_renamed_folder_lists_every_entry_in_it` | Listing only the folder would under-count the deletions the >50% guard has to see. |
| `...::test_an_edit_in_place_is_still_modified` | Skipping the whole shared-inode pass along with move detection would lose in-place edits. |
| `...::test_move_detection_stays_on_by_default` | A default of "off" would turn every backup's renames into delete and re-send. |

**Steps:**

- [ ] **Step 1: Write the failing tests**

In `tests/test_diff.py`, add `same_file` to the import list:

```diff
--- a/tests/test_diff.py
+++ b/tests/test_diff.py
@@ -11,6 +11,7 @@
     make_parent_substituter,
     plan_directory_moves,
     prune_redundant_dir_moves,
+    same_file,
 )


@@ -709,3 +710,4 @@
             f"got modified={changes.modified!r}, deleted={changes.deleted!r}, "
             f"created={changes.created!r}"
         )
+
```

Then append this to the end of `tests/test_diff.py` (two blank lines after the last existing test):

```python
class TestSameFile:
    @staticmethod
    def _info(**overrides):
        row = _row(**{"ino": 1, "path": "p", "size": 10, **overrides})
        return index_by_inode([row])[(1, 1)]

    @pytest.mark.parametrize(
        ("before", "after", "expected"),
        [
            ({}, {}, True),
            ({}, {"size": 11}, False),
            ({}, {"mtime_ns": 1_000_000_001}, False),
            ({}, {"btime_ns": 2_000_000_000}, False),
            ({"btime_ns": -1}, {"btime_ns": 2_000_000_000}, True),
            ({}, {"btime_ns": -1}, True),
            ({"mtime_ns": -1}, {"mtime_ns": -1}, False),
            ({}, {"mtime_ns": -1}, False),
        ],
        ids=[
            "identical",
            "size-differs",
            "mtime-one-ns-apart",
            "btime-differs",
            "baseline-has-no-btime",
            "source-has-no-btime",
            "neither-has-an-mtime",
            "source-has-no-mtime",
        ],
    )
    def test_identity_needs_size_mtime_and_btime_when_known(
        self, before, after, expected
    ):
        # Behavior: two records of one inode number are the same file when
        # size and mtime_ns match, and btime_ns too when both have one.
        # Bug caught: dropping any clause would call a reused inode number
        # a rename, and the replay would then move the wrong file's backup
        # copy to the new file's path.
        # Expected values: AD-3 and AD-13, read by hand: -1 means "not
        # recorded"; an unrecorded mtime is never trusted, an unrecorded
        # btime only switches the tiebreaker off.
        assert same_file(self._info(**before), self._info(**after)) is expected


class TestComputeChangesWithoutMoveDetection:
    def test_a_renamed_file_is_deleted_and_created(self):
        # Behavior: with move detection off, a path change is a deletion
        # at the old path and a creation at the new one.
        # Bug caught: a switch that only hid the move lists would leave the
        # renamed file out of every list, and the run could stop at "no
        # source changes" without backing it up.
        before = [_row(ino=10, path="old.txt")]
        after = [_row(ino=10, path="new.txt")]

        changes = compute_changes(before, after, detect_moves=False)

        assert changes == Changes(deleted=["old.txt"], created=["new.txt"])

    def test_a_renamed_folder_lists_every_entry_in_it(self):
        # Behavior: a renamed folder appears entry by entry, the folder
        # itself included, since each is deleted and sent again where the
        # move cannot be replayed.
        # Bug caught: listing only the folder would under-count the
        # deletions the >50% guard is there to see.
        # Expected values: hand count, 3 entries each way.
        before = [
            _row(ino=1, path="big", type="d"),
            _row(ino=2, path="big/f1"),
            _row(ino=3, path="big/sub/f2"),
        ]
        after = [
            _row(ino=1, path="big2", type="d"),
            _row(ino=2, path="big2/f1"),
            _row(ino=3, path="big2/sub/f2"),
        ]

        changes = compute_changes(before, after, detect_moves=False)

        assert changes == Changes(
            deleted=["big", "big/f1", "big/sub/f2"],
            created=["big2", "big2/f1", "big2/sub/f2"],
        )

    def test_an_edit_in_place_is_still_modified(self):
        # Bug caught: skipping the whole shared-inode pass along with move
        # detection would lose in-place edits.
        before = [_row(ino=10, path="a.txt", size=1)]
        after = [_row(ino=10, path="a.txt", size=2)]

        changes = compute_changes(before, after, detect_moves=False)

        assert changes == Changes(modified=["a.txt"])

    def test_move_detection_stays_on_by_default(self):
        # Bug caught: a default of "off" would silently turn every backup's
        # renames into delete and re-send.
        before = [_row(ino=10, path="old.txt")]
        after = [_row(ino=10, path="new.txt")]

        assert compute_changes(before, after) == Changes(
            file_moves=[("old.txt", "new.txt")]
        )
```

- [ ] **Step 2: Run them and see them fail**

Run: `pixi run test tests/test_diff.py -q`
Expected: a collection error, `ImportError: cannot import name 'same_file' from 'irsync.diff'`.

- [ ] **Step 3: Implement**

Change `src/irsync/diff.py`: a new `same_file` above `compute_moves`, which now calls it; and a `detect_moves` switch on `compute_changes`.

```diff
--- a/src/irsync/diff.py
+++ b/src/irsync/diff.py
@@ -117,6 +117,47 @@
     return kept


+def same_file(b: NodeInfo, a: NodeInfo) -> bool:
+    """Return True if two records of one inode number describe the same file.
+
+    The one definition of "same file" for move detection: a shared
+    ``(dev, ino)`` alone is not enough, because a kernel that hands a freed
+    inode number to a brand-new file would otherwise look identical to a
+    rename.
+
+    Args:
+        b: The inode's record in the baseline.
+        a: The inode's record in the fresh snapshot.
+
+    Returns:
+        True if size and ``mtime_ns`` match, and ``btime_ns`` too when both
+        records have one.
+    """
+    # The size+mtime_ns gate (H2 from pass 2) catches inode reuse on
+    # filesystems with nanosecond-precision mtime.
+    #
+    # The btime_ns tiebreaker (replacing the broken 5th-pass ctime attempt)
+    # closes the residual NFS hole: on second-granular FSes it's possible for
+    # an unrelated new file to land at the same inode with the same
+    # wall-clock-second mtime AND the same size. btime is set when the inode
+    # is allocated and never changes afterward, so a real rename preserves it
+    # while inode reuse always changes it. btime must match only WHEN both
+    # snapshots have it (>= 0); when either side reports -1 (statx
+    # unavailable, FS without btime support, or legacy snapshot), the
+    # size+mtime gate decides alone.
+    #
+    # mtime_ns == -1 (legacy snapshot pre-H2) still forces a conservative
+    # "not the same file": a safe fallback, rsync re-transfers.
+    both_have_btime = b["btime_ns"] >= 0 and a["btime_ns"] >= 0
+    return not (
+        b["mtime_ns"] == -1
+        or a["mtime_ns"] == -1
+        or b["size"] != a["size"]
+        or b["mtime_ns"] != a["mtime_ns"]
+        or (both_have_btime and b["btime_ns"] != a["btime_ns"])
+    )
+
+
 def compute_moves(
     before: dict[tuple[int, int], NodeInfo],
     after: dict[tuple[int, int], NodeInfo],
@@ -178,33 +219,8 @@
         if len(bpaths) == 1 and len(apaths) == 1:
             (old_path,) = tuple(bpaths)
             (new_path,) = tuple(apaths)
-            # Defend against inode reuse: a kernel that hands out a freed
-            # inode number to a brand-new file would otherwise look identical
-            # to a rename. The size+mtime_ns gate (H2 from pass 2) catches
-            # this on filesystems with nanosecond-precision mtime.
-            #
-            # The btime_ns tiebreaker (replacing the broken 5th-pass ctime
-            # attempt) closes the residual NFS hole: on second-granular FSes
-            # it's possible for an unrelated new file to land at the same
-            # inode with the same wall-clock-second mtime AND the same size.
-            # btime is set when the inode is allocated and never changes
-            # afterward, so a real rename preserves it while inode reuse
-            # always changes it. We only require btime to match WHEN both
-            # snapshots have it (>= 0); when either side reports -1 (statx
-            # unavailable, FS without btime support, or legacy snapshot),
-            # we fall back to the original size+mtime gate alone.
-            #
-            # mtime_ns == -1 sentinel (legacy snapshot pre-H2) still forces
-            # a conservative refuse-the-move — safe fallback, rsync re-
-            # transfers.
-            both_have_btime = b["btime_ns"] >= 0 and a["btime_ns"] >= 0
-            if (
-                b["mtime_ns"] == -1
-                or a["mtime_ns"] == -1
-                or b["size"] != a["size"]
-                or b["mtime_ns"] != a["mtime_ns"]
-                or (both_have_btime and b["btime_ns"] != a["btime_ns"])
-            ):
+            # Defend against inode reuse (see same_file).
+            if not same_file(b, a):
                 logging.debug(
                     "Inode %s path changed but size/mtime differ "
                     "(possible inode reuse); not treating as a move: %s -> %s",
@@ -336,6 +352,7 @@
     *,
     skip_hardlinks: bool = True,
     include_symlinks: bool = False,
+    detect_moves: bool = True,
 ) -> Changes:
     """High-level diff: produce moves, prune redundant dir moves, and list created/deleted.

@@ -344,6 +361,9 @@
         after_rows: Snapshot rows from the current run.
         skip_hardlinks: Forwarded to :func:`compute_moves`.
         include_symlinks: Forwarded to :func:`compute_moves`.
+        detect_moves: If False, report no moves: an entry whose path changed
+            is listed as deleted at its old path and created at its new one,
+            which is what rsync does where a move cannot be replayed.

     Returns:
         A :class:`Changes` aggregate ready for the orchestrator to act on.
@@ -351,11 +371,18 @@
     before = index_by_inode(before_rows)
     after = index_by_inode(after_rows)

-    dir_moves, file_moves, consumed_keys = compute_moves(
-        before, after, skip_hardlinks=skip_hardlinks, include_symlinks=include_symlinks
-    )
-    dir_moves = prune_redundant_dir_moves(dir_moves)
-    file_moves = _suppress_file_moves_covered_by_dirs(dir_moves, file_moves)
+    dir_moves: list[tuple[str, str]] = []
+    file_moves: list[tuple[str, str]] = []
+    consumed_keys: set[tuple[int, int]] = set()
+    if detect_moves:
+        dir_moves, file_moves, consumed_keys = compute_moves(
+            before,
+            after,
+            skip_hardlinks=skip_hardlinks,
+            include_symlinks=include_symlinks,
+        )
+        dir_moves = prune_redundant_dir_moves(dir_moves)
+        file_moves = _suppress_file_moves_covered_by_dirs(dir_moves, file_moves)

     before_keys = set(before.keys())
     after_keys = set(after.keys())
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `pixi run test tests/test_diff.py -q` → no failures (12 more tests than before).
Run: `pixi run test -q` → `443 passed, 1 skipped`.

- [ ] **Step 5: Gate and commit**

```bash
pixi run pre-commit run --all-files
pixi run test-cov
git add src/irsync/diff.py tests/test_diff.py
git commit -m "feat: add same_file and a diff without move detection

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: The destination interface and its local implementation

**Goal:** `irsync.destination` offers `Destination`, `LocalDestination`, `Seen` and `DestinationUnreadable`: a local backup can be asked what it holds at a list of paths, and renamed on.

**Files:**
- Create: `src/irsync/destination.py`
- Create: `tests/test_destination.py`

**Acceptance Criteria:**
- [ ] `LocalDestination(root).look(paths)` returns one entry per distinct path: `Seen(type, size, mtime_ns)` taken by `lstat`, or `None`.
- [ ] A path is absent (`None`) when nothing is there, when a parent is missing or is not a directory, when the root does not exist, or when a parent below the root is a symlink (never followed). A symlink on the way to the root is followed.
- [ ] An `lstat` failure other than absence raises `DestinationUnreadable` carrying the path and the reason.
- [ ] `look([])` touches no filesystem; a folder the destination lacks costs one `lstat` whatever lies below it.
- [ ] `replay` renames under the root through `apply_moves` and returns its `ReplayResult`.
- [ ] `pixi run test -q` ends with `452 passed, 1 skipped`; `pixi run test-cov` shows `src/irsync/destination.py` at 100%.

**Verify:** `pixi run test tests/test_destination.py -q` → `9 passed`; then `pixi run test -q` → `452 passed, 1 skipped`

**What each test is for:**

| Test | Bug it would catch |
|---|---|
| `TestLook::test_reports_type_size_and_mtime_of_each_kind_of_entry` | `stat` instead of `lstat` would report a symlink as the file it points to; a swapped size or mtime would make every copy look stale. |
| `TestLook::test_nothing_there_is_an_answer_not_an_error` | Letting `ENOENT` or `ENOTDIR` escape would refuse a run over the most ordinary case: a backup that never received the folder. |
| `TestLook::test_a_root_that_does_not_exist_yet_holds_nothing` | Resolving or listing a missing root would raise on a destination rsync is about to create. |
| `TestLook::test_a_symlinked_folder_below_the_root_is_not_followed` | Following the link would report a file outside the backup as held there, and a move would be planned from a place the replay refuses to touch. |
| `TestLook::test_a_symlink_on_the_way_to_the_root_is_followed` | Applying the no-symlink rule to the root's own path would make every file on a symlinked mount point look absent. |
| `TestLook::test_a_folder_that_cannot_be_read_raises` | Treating a permission error as absence would list the backup's files as created on the strength of an error. |
| `TestLook::test_no_paths_means_no_filesystem_access` | Resolving the root first would wake the backup drive on a run that has nothing to ask (AD-2). |
| `TestLook::test_a_missing_folder_costs_one_lookup_whatever_lies_below` | One `lstat` per row would stat every file of a folder the backup does not have. |
| `TestReplay::test_renames_under_the_root` | A replay wired to another root, or with the move lists swapped, would rename nothing here. |

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/test_destination.py`:

```python
"""LocalDestination: what a local backup holds at a path, and renaming on it."""

import os
from pathlib import Path

import pytest

from irsync.destination import DestinationUnreadable, LocalDestination, Seen
from irsync.replay import ReplayResult

_T = 1_700_000_000_123_456_789


def _file(path: Path, content: bytes = b"data", mtime_ns: int = _T) -> Path:
    """Create ``path`` holding ``content`` with a known mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


class TestLook:
    def test_reports_type_size_and_mtime_of_each_kind_of_entry(self, tmp_path):
        # Behavior: look answers with the entry's own type, size and mtime.
        # Bug caught: stat instead of lstat would report the symlink as the
        # file it points to; a swapped size/mtime would make every copy
        # look stale.
        # Expected values: the 5-byte content and the mtime set by hand. A
        # directory's size and a symlink's mtime are the filesystem's own
        # business, so only their types are asserted; a FIFO stands for
        # every entry that is none of the three.
        _file(tmp_path / "f.bin", b"12345")
        (tmp_path / "d").mkdir()
        os.symlink("f.bin", tmp_path / "link")
        os.mkfifo(tmp_path / "pipe")

        seen = LocalDestination(tmp_path).look(["f.bin", "d", "link", "pipe"])

        assert seen["f.bin"] == Seen(type="f", size=5, mtime_ns=_T)
        assert {path: there.type for path, there in seen.items() if there} == {
            "f.bin": "f",
            "d": "d",
            "link": "l",
            "pipe": "o",
        }

    def test_nothing_there_is_an_answer_not_an_error(self, tmp_path):
        # Behavior: a missing path, a path under a missing folder, and a
        # path under a regular file are all absent.
        # Bug caught: letting ENOTDIR or ENOENT escape would refuse a run
        # over the most ordinary case there is, a backup that never
        # received the folder.
        # Expected values: None for each, by the design's section 5.
        _file(tmp_path / "plain.txt")

        seen = LocalDestination(tmp_path).look(
            ["gone.txt", "gone/deep/file.txt", "plain.txt/child"]
        )

        assert seen == {
            "gone.txt": None,
            "gone/deep/file.txt": None,
            "plain.txt/child": None,
        }

    def test_a_root_that_does_not_exist_yet_holds_nothing(self, tmp_path):
        # Bug caught: resolving or listing a missing root would raise on a
        # destination rsync is about to create.
        seen = LocalDestination(tmp_path / "not-yet").look(["a", "b/c"])

        assert seen == {"a": None, "b/c": None}

    def test_a_symlinked_folder_below_the_root_is_not_followed(self, tmp_path):
        # Behavior: a path that runs through a symlinked folder is absent,
        # while the symlink itself is reported as a symlink.
        # Bug caught: following the link would report a file outside the
        # backup as held there, and a move would be planned from a place
        # the replay refuses to touch (AD-9).
        # Expected values: victim.txt really exists through the link, so
        # only "not followed" can explain None.
        root = tmp_path / "backup"
        root.mkdir()
        outside = tmp_path / "outside"
        _file(outside / "victim.txt")
        os.symlink(outside, root / "link")
        assert (root / "link" / "victim.txt").is_file()

        seen = LocalDestination(root).look(["link/victim.txt", "link"])

        assert seen["link/victim.txt"] is None
        there = seen["link"]
        assert there is not None and there.type == "l"

    def test_a_symlink_on_the_way_to_the_root_is_followed(self, tmp_path):
        # Behavior: the root may itself be reached through a symlink (a
        # symlinked mount point).
        # Bug caught: applying the no-symlink rule to the root's own path
        # would make every file on such a backup look absent.
        real = tmp_path / "real-backup"
        _file(real / "f.bin", b"12345")
        os.symlink(real, tmp_path / "mount")

        seen = LocalDestination(tmp_path / "mount").look(["f.bin"])

        assert seen == {"f.bin": Seen(type="f", size=5, mtime_ns=_T)}

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_a_folder_that_cannot_be_read_raises(self, tmp_path):
        # Behavior: a permission error is a failure, named by its path.
        # Bug caught: treating it as absent would list the backup's files
        # as created on the strength of an error.
        # Expected values: the path asked about, under the locked folder.
        locked = tmp_path / "locked"
        _file(locked / "f.bin")
        locked.chmod(0)
        try:
            with pytest.raises(DestinationUnreadable) as excinfo:
                LocalDestination(tmp_path).look(["locked/f.bin"])
        finally:
            locked.chmod(0o755)

        assert excinfo.value.path == str(tmp_path.resolve() / "locked" / "f.bin")
        assert "ermission denied" in excinfo.value.reason

    def test_no_paths_means_no_filesystem_access(self, tmp_path, monkeypatch):
        # Behavior: asking about nothing touches nothing.
        # Bug caught: resolving the root first would wake the backup drive
        # on a run that has nothing to ask (AD-2).
        def no_access(*_args, **_kwargs):
            raise AssertionError("the destination was accessed")

        monkeypatch.setattr("irsync.destination.os.lstat", no_access)
        monkeypatch.setattr(Path, "resolve", no_access)

        assert LocalDestination(tmp_path).look([]) == {}

    def test_a_missing_folder_costs_one_lookup_whatever_lies_below(
        self, tmp_path, monkeypatch
    ):
        # Behavior: once a folder is known to be missing, nothing below it
        # is examined.
        # Bug caught: one lstat per row would make a first --no-exclude run
        # over a large cache tree stat every file of a folder the backup
        # does not have.
        # Expected values: only "gone" itself needs examining. Resolving
        # the root also calls lstat, on the root and its parents, so only
        # paths below the root are recorded.
        examined: list[str] = []
        real_lstat = os.lstat
        root = tmp_path.resolve()

        def recording_lstat(path):
            if root in Path(path).parents:
                examined.append(Path(path).relative_to(root).as_posix())
            return real_lstat(path)

        monkeypatch.setattr("irsync.destination.os.lstat", recording_lstat)

        seen = LocalDestination(tmp_path).look(["gone/a", "gone/b", "gone/sub/deep/c"])

        assert seen == {"gone/a": None, "gone/b": None, "gone/sub/deep/c": None}
        assert examined == ["gone"]


class TestReplay:
    def test_renames_under_the_root(self, tmp_path):
        # Behavior: replay performs the moves on this destination's root.
        # Bug caught: a replay wired to another root, or with the move
        # lists swapped, would rename nothing here.
        _file(tmp_path / "old" / "f.bin", b"payload")

        result = LocalDestination(tmp_path).replay(
            dir_moves=[], file_moves=[("old/f.bin", "new/f.bin")]
        )

        assert result == ReplayResult(dirs_moved=0, files_moved=1, skipped=0)
        assert (tmp_path / "new" / "f.bin").read_bytes() == b"payload"
        assert not (tmp_path / "old" / "f.bin").exists()
```

- [ ] **Step 2: Run them and see them fail**

Run: `pixi run test tests/test_destination.py -q`
Expected: a collection error, `ModuleNotFoundError: No module named 'irsync.destination'`.

- [ ] **Step 3: Implement**

Create `src/irsync/destination.py`:

```python
"""What irsync may ask of a backup destination: look at paths, and replay moves."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from irsync.replay import ReplayResult, apply_moves
from irsync.snapshot import RowType


class DestinationUnreadable(Exception):
    """Raised by :meth:`Destination.look` when a path cannot be examined.

    Not raised for a path that is simply not there: that is an answer, not a
    failure. The orchestrator refuses the run, because a backup that cannot
    be read must not be described as if it held nothing.

    Attributes:
        path: The destination path that could not be examined.
        reason: The operating system's reason, as text.
    """

    def __init__(self, path: str, reason: str) -> None:
        """Record the path and the reason, and build the message from them."""
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class Seen:
    """What a destination holds at one path.

    Attributes:
        type: The entry's type tag, as in a snapshot row.
        size: Its size in bytes.
        mtime_ns: Its modification time in nanoseconds since the epoch.
    """

    type: RowType
    size: int
    mtime_ns: int


class Destination(Protocol):
    """A backup destination irsync can examine and rename entries on.

    The local filesystem implements it today (:class:`LocalDestination`). A
    remote destination has no implementation yet, so the orchestrator holds
    ``None`` for one and neither looks nor replays there.
    """

    def look(self, paths: Iterable[str]) -> dict[str, Seen | None]:
        """Report what the destination holds at each path.

        Takes every path in one call so that an implementation working over
        a network needs a single round trip.

        Args:
            paths: POSIX paths relative to the destination root.

        Returns:
            One entry per distinct path: what is there, or None if nothing
            is.

        Raises:
            DestinationUnreadable: If a path cannot be examined for a reason
                other than its absence.
        """
        ...

    def replay(
        self,
        *,
        dir_moves: list[tuple[str, str]],
        file_moves: list[tuple[str, str]],
    ) -> ReplayResult:
        """Rename entries on the destination, as :func:`~irsync.replay.apply_moves` does.

        Args:
            dir_moves: ``(old, new)`` directory pairs.
            file_moves: ``(old, new)`` file pairs.

        Returns:
            Counts of what was moved and skipped.
        """
        ...


def _type_of(mode: int) -> RowType:
    """Classify an ``lstat`` mode as a snapshot row type tag."""
    if stat.S_ISLNK(mode):
        return "l"
    if stat.S_ISDIR(mode):
        return "d"
    if stat.S_ISREG(mode):
        return "f"
    return "o"


class LocalDestination:
    """A destination on a local filesystem, rooted at ``root``."""

    def __init__(self, root: Path) -> None:
        """Remember the destination root; nothing is read until asked."""
        self._root = root

    def look(self, paths: Iterable[str]) -> dict[str, Seen | None]:
        """Report what the destination holds at each path, by ``lstat``.

        A path is absent when nothing is there, when one of its parents is
        missing or is not a directory, or when one of its parents below the
        root is a symlink: a symlinked directory is never followed, the rule
        :func:`~irsync.replay.apply_moves` has, since it can lead out of the
        backup. Each directory on the way is examined once, so a folder the
        destination lacks costs one ``lstat`` however many paths lie below
        it. No path means no filesystem access at all.

        Args:
            paths: POSIX paths relative to the destination root.

        Returns:
            One entry per distinct path: what is there, or None.

        Raises:
            DestinationUnreadable: If an ``lstat`` fails for a reason other
                than the path's absence, such as a permission or I/O error.
        """
        wanted = sorted(set(paths))
        if not wanted:
            return {}
        # Resolved once, so a symlink on the way to the root (a symlinked
        # mount point) is fine while one below it is not followed.
        root = self._root.resolve()
        stats: dict[str, os.stat_result | None] = {}
        real_dir: dict[str, bool] = {"": True}

        def lstat(rel: str) -> os.stat_result | None:
            if rel not in stats:
                path = root / PurePosixPath(rel)
                try:
                    stats[rel] = os.lstat(path)
                except (FileNotFoundError, NotADirectoryError):
                    stats[rel] = None
                except OSError as e:
                    raise DestinationUnreadable(str(path), e.strerror or str(e)) from e
            return stats[rel]

        def is_real_dir(rel_dir: str) -> bool:
            # Walk up to the nearest directory already judged, then judge
            # each one on the way back down. Iterative, for trees deeper
            # than the recursion limit (10th-F5).
            pending: list[str] = []
            d = rel_dir
            while d not in real_dir:
                pending.append(d)
                d = d.rpartition("/")[0]
            ok = real_dir[d]
            for p in reversed(pending):
                if ok:
                    st = lstat(p)
                    ok = st is not None and stat.S_ISDIR(st.st_mode)
                real_dir[p] = ok
            return ok

        seen: dict[str, Seen | None] = {}
        for rel in wanted:
            st = lstat(rel) if is_real_dir(rel.rpartition("/")[0]) else None
            seen[rel] = (
                None
                if st is None
                else Seen(
                    type=_type_of(st.st_mode), size=st.st_size, mtime_ns=st.st_mtime_ns
                )
            )
        return seen

    def replay(
        self,
        *,
        dir_moves: list[tuple[str, str]],
        file_moves: list[tuple[str, str]],
    ) -> ReplayResult:
        """Rename entries under the root with :func:`~irsync.replay.apply_moves`.

        Args:
            dir_moves: ``(old, new)`` directory pairs.
            file_moves: ``(old, new)`` file pairs.

        Returns:
            Counts of what was moved and skipped.

        Raises:
            CrossDeviceMoveError: If a planned move would cross filesystems
                on the destination; raised before any rename.
        """
        return apply_moves(
            dir_moves=dir_moves, file_moves=file_moves, dest_root=self._root
        )
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `pixi run test tests/test_destination.py -q` → `9 passed`.
Run: `pixi run test -q` → `452 passed, 1 skipped`.

- [ ] **Step 5: Gate and commit**

`pixi run test-cov` must show `src/irsync/destination.py` at 100%.

```bash
pixi run pre-commit run --all-files
pixi run test-cov
git add src/irsync/destination.py tests/test_destination.py
git commit -m "feat: add a destination interface with a local implementation

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Baseline rows checked against the destination

**Goal:** `irsync.baseline` splits a baseline into trusted rows and hints, decides whether the destination holds a row, replaces hints by what the destination holds, and picks the hints a run with the excludes may ask about, all without I/O.

**Files:**
- Create: `src/irsync/baseline.py`
- Create: `tests/test_baseline.py`

**Acceptance Criteria:**
- [ ] `split_hints(rows, unvouched)` returns `(trusted, hints)`, each in the baseline's order.
- [ ] `holds(row, there)`: a regular file is held when the entry is a file of the same size whose mtime agrees to the whole second, and the row's mtime is recorded; any other row is held when the entry has the row's type.
- [ ] `checked_rows(hints, seen)`: nothing there drops the row; a held row is kept unchanged; anything else is kept with the destination's type, size and `mtime_ns`.
- [ ] `moved_out_candidates(hints, trusted, after)` returns only regular-file hints whose inode has exactly that one path in the baseline and exactly one path in `after`, where it is still a regular file and `same_file` holds.
- [ ] Through `compute_changes`, the spec's section 1 table holds line by line, and admitting only candidates lists no path under an excluded name as modified, created or deleted.
- [ ] `pixi run test -q` ends with `482 passed, 1 skipped`; `pixi run test-cov` shows `src/irsync/baseline.py` at 100%.

**Verify:** `pixi run test tests/test_baseline.py -q` → `30 passed`; then `pixi run test -q` → `482 passed, 1 skipped`

**What each test is for:**

| Test | Bug it would catch |
|---|---|
| `TestSplitHints::test_rows_under_an_excluded_name_are_hints` | Matching only a top-level name would trust `proj/.cache/x`, which a run with the excludes never copied. |
| `TestHolds::test_a_file_is_held_by_rsyncs_own_test` (8 cases, both edges of the second) | Comparing nanoseconds would refuse a move rsync would have left alone (an unneeded copy); comparing more loosely, or ignoring size or type, would list a move rsync then re-sends. |
| `TestHolds::test_other_entries_are_held_by_type_alone` (3 cases) | Comparing a folder's size or mtime would drop a folder the backup has, and with it every directory move out of an excluded name. |
| `TestHolds::test_a_row_with_no_recorded_mtime_is_never_held` | A legacy row (`mtime_ns == -1`) would match any same-sized file dated in the second before the epoch. |
| `TestCheckedRows::test_nothing_there_drops_the_row_so_the_file_is_created` | Keeping the row would list a move the backup cannot make (case 1). |
| `TestCheckedRows::test_nothing_there_and_gone_from_the_source_reports_nothing` | A phantom deletion that counts toward the >50% guard (case 2). |
| `TestCheckedRows::test_a_matching_copy_keeps_the_row_so_a_new_path_is_a_move` | Dropping every hint would copy a file the backup already holds (the rejected option). |
| `TestCheckedRows::test_a_matching_copy_at_an_unchanged_path_reports_nothing` | A kept row altered on the way would report a change where there is none. |
| `TestCheckedRows::test_a_differing_copy_at_the_same_path_is_modified` | Trusting the baseline row would report nothing while rsync re-sends the file. |
| `TestCheckedRows::test_a_differing_copy_whose_file_moved_is_deleted_and_created` | Listing a move, then renaming a stale copy that rsync has to re-send (case 4). |
| `TestCheckedRows::test_a_wrong_type_on_the_destination_is_deleted_and_created` | Keeping the row as a file would report nothing while rsync replaces a folder with the file. |
| `TestCheckedRows::test_a_folder_the_destination_lacks_takes_its_rows_with_it` | Rows below a missing folder kept as facts, each then reported as a deletion from a backup that never held them. |
| `TestMovedOutCandidates::test_a_file_moved_into_the_synced_tree_qualifies` | No candidates at all would copy a file the backup already holds (case 3). |
| `TestMovedOutCandidates::test_anything_else_does_not` (7 cases) | A hint admitted without being certain to become a move is left over by the diff and listed as deleted under an excluded name, in a run that deletes nothing there. |
| `TestMovedOutCandidates::test_admitting_only_candidates_lists_nothing_under_an_excluded_name` | Admitting every hint whose inode is still on the source would list an edited file's old path as deleted. |

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/test_baseline.py`:

```python
"""What a baseline may say about the backup under the default-excluded names."""

import pytest

from irsync.baseline import checked_rows, holds, moved_out_candidates, split_hints
from irsync.destination import Seen
from irsync.diff import compute_changes
from irsync.rsync_runner import exclude_matcher

_S = 1_000_000_000  # nanoseconds per second
_T = 1_700_000_000 * _S + 500_000_000  # half past a whole second

_UNVOUCHED = exclude_matcher([".Trash-*", ".cache", "unfinished_downloads"])


def _row(*, ino, path, type="f", size=10, mtime_ns=_T, btime_ns=_T, dev=1, nlink=1):
    return {
        "dev": dev,
        "ino": ino,
        "type": type,
        "nlink": nlink,
        "size": size,
        "mtime_ns": mtime_ns,
        "btime_ns": btime_ns,
        "path": path,
    }


class TestSplitHints:
    def test_rows_under_an_excluded_name_are_hints(self):
        # Behavior: a row is a hint when its own name or a parent folder's
        # matches a default exclude; every other row is trusted.
        # Bug caught: matching only the top-level name would trust
        # "proj/.cache/x", which a run with the excludes never copied.
        # Expected values: by the exclude list above, read by hand.
        rows = [
            _row(ino=1, path="docs/a.txt"),
            _row(ino=2, path="unfinished_downloads", type="d"),
            _row(ino=3, path="unfinished_downloads/part.bin"),
            _row(ino=4, path="proj/.cache/x"),
            _row(ino=5, path=".Trash-1000/files/old.txt"),
            _row(ino=6, path="proj/keep.txt"),
        ]

        trusted, hints = split_hints(rows, _UNVOUCHED)

        assert [r["path"] for r in trusted] == ["docs/a.txt", "proj/keep.txt"]
        assert [r["path"] for r in hints] == [
            "unfinished_downloads",
            "unfinished_downloads/part.bin",
            "proj/.cache/x",
            ".Trash-1000/files/old.txt",
        ]


class TestHolds:
    @pytest.mark.parametrize(
        ("there", "expected"),
        [
            (Seen(type="f", size=10, mtime_ns=_T), True),
            # rsync's quick check compares whole seconds: a copy on a
            # filesystem with coarser timestamps is still up to date.
            (Seen(type="f", size=10, mtime_ns=_T - 400_000_000), True),
            (Seen(type="f", size=10, mtime_ns=_T + 499_999_999), True),
            (Seen(type="f", size=10, mtime_ns=_T + 500_000_000), False),
            (Seen(type="f", size=10, mtime_ns=_T - 500_000_001), False),
            (Seen(type="f", size=11, mtime_ns=_T), False),
            (Seen(type="d", size=10, mtime_ns=_T), False),
            (None, False),
        ],
        ids=[
            "identical",
            "same-second-earlier-ns",
            "last-ns-of-the-second",
            "first-ns-of-the-next-second",
            "last-ns-of-the-previous-second",
            "size-differs",
            "folder-where-a-file-is-recorded",
            "nothing-there",
        ],
    )
    def test_a_file_is_held_by_rsyncs_own_test(self, there, expected):
        # Behavior: a file is held when the destination's copy has the same
        # size and the same mtime to the whole second.
        # Bug caught: comparing nanoseconds would refuse a move rsync would
        # have left alone (an unneeded copy); comparing more loosely, or
        # ignoring size or type, would list a move rsync then re-sends.
        # Expected values: rsync's quick check, observed on rsync 3.4.1
        # (design doc, Decisions): _T is xxx.5 s, so the second it lies in
        # runs from _T - 500_000_000 to _T + 499_999_999.
        row = _row(ino=1, path="unfinished_downloads/ep.mkv", size=10, mtime_ns=_T)

        assert holds(row, there) is expected

    @pytest.mark.parametrize("row_type", ["d", "l", "o"])
    def test_other_entries_are_held_by_type_alone(self, row_type):
        # Behavior: a folder, symlink or special file is held when the
        # destination has an entry of the same type.
        # Bug caught: comparing a folder's size or mtime, which change
        # whenever its contents do, would drop a folder the backup has and
        # with it every directory move out of an excluded name.
        row = _row(ino=1, path=".cache/x", type=row_type, size=4096, mtime_ns=_T)

        assert holds(row, Seen(type=row_type, size=1, mtime_ns=5)) is True
        other = "f" if row_type != "f" else "d"
        assert holds(row, Seen(type=other, size=4096, mtime_ns=_T)) is False

    def test_a_row_with_no_recorded_mtime_is_never_held(self):
        # Behavior: a legacy row (mtime_ns == -1) matches nothing.
        # Bug caught: -1 ns lies in the second before the epoch, so without
        # the guard such a row would "match" any same-sized file dated in
        # that second.
        row = _row(ino=1, path=".cache/x", size=10, mtime_ns=-1)

        assert holds(row, Seen(type="f", size=10, mtime_ns=-5)) is False


class TestCheckedRows:
    """The design's section 1 table, one line per test, through the real diff."""

    HINT = "unfinished_downloads/ep.mkv"

    def _hint(self, **overrides):
        return _row(ino=7, path=self.HINT, **overrides)

    def test_nothing_there_drops_the_row_so_the_file_is_created(self):
        # Table line 1. Bug caught: keeping the row would list a move the
        # backup cannot make (the reproduced case 1).
        before = checked_rows([self._hint()], {self.HINT: None})
        after = [_row(ino=7, path="finished/ep.mkv")]

        changes = compute_changes(before, after)

        assert before == []
        assert (changes.file_moves, changes.created, changes.deleted) == (
            [],
            ["finished/ep.mkv"],
            [],
        )

    def test_nothing_there_and_gone_from_the_source_reports_nothing(self):
        # Table line 1, the other half. Bug caught: a phantom deletion that
        # counts toward the >50% guard (the reproduced case 2).
        before = checked_rows([self._hint()], {self.HINT: None})

        assert compute_changes(before, []).any_changes() is False

    def test_a_matching_copy_keeps_the_row_so_a_new_path_is_a_move(self):
        # Table line 2. Bug caught: dropping every hint would copy a file
        # the backup already holds (the rejected option A).
        hint = self._hint()
        before = checked_rows(
            [hint], {self.HINT: Seen(type="f", size=10, mtime_ns=_T - 1)}
        )
        after = [_row(ino=7, path="finished/ep.mkv")]

        changes = compute_changes(before, after)

        assert before == [hint]
        assert changes.file_moves == [(self.HINT, "finished/ep.mkv")]
        assert (changes.created, changes.deleted, changes.modified) == ([], [], [])

    def test_a_matching_copy_at_an_unchanged_path_reports_nothing(self):
        # Table line 2, unchanged path.
        before = checked_rows(
            [self._hint()], {self.HINT: Seen(type="f", size=10, mtime_ns=_T)}
        )

        assert compute_changes(before, [self._hint()]).any_changes() is False

    def test_a_differing_copy_at_the_same_path_is_modified(self):
        # Table line 3. Bug caught: trusting the baseline row would report
        # nothing while rsync re-sends the file.
        # Expected values: the destination's copy is 4 bytes, the source's
        # 10, so rsync's quick check re-sends it.
        there = Seen(type="f", size=4, mtime_ns=_T - 9 * _S)
        before = checked_rows([self._hint()], {self.HINT: there})

        changes = compute_changes(before, [self._hint()])

        assert before == [self._hint(size=4, mtime_ns=_T - 9 * _S)]
        assert changes.modified == [self.HINT]
        assert (changes.created, changes.deleted, changes.file_moves) == ([], [], [])

    def test_a_differing_copy_whose_file_moved_is_deleted_and_created(self):
        # Table line 3, changed path. Bug caught: listing a move, then
        # renaming a stale copy that rsync has to re-send (the reproduced
        # case 4).
        there = Seen(type="f", size=4, mtime_ns=_T - 9 * _S)
        before = checked_rows([self._hint()], {self.HINT: there})
        after = [_row(ino=7, path="finished/ep.mkv")]

        changes = compute_changes(before, after)

        assert changes.file_moves == []
        assert (changes.deleted, changes.created) == ([self.HINT], ["finished/ep.mkv"])

    def test_a_wrong_type_on_the_destination_is_deleted_and_created(self):
        # Design section 5. Bug caught: keeping the row as a file would
        # report nothing while rsync replaces the folder with the file.
        there = Seen(type="d", size=4096, mtime_ns=_T)
        before = checked_rows([self._hint()], {self.HINT: there})

        changes = compute_changes(before, [self._hint()])

        assert [r["type"] for r in before] == ["d"]
        assert (changes.deleted, changes.created) == ([self.HINT], [self.HINT])

    def test_a_folder_the_destination_lacks_takes_its_rows_with_it(self):
        # Design section 2. Bug caught: rows below a missing folder kept as
        # facts, each then reported as a deletion from a backup that never
        # held them.
        folder = _row(ino=20, path="unfinished_downloads", type="d")
        child = _row(ino=21, path="unfinished_downloads/part.bin")
        seen = {"unfinished_downloads": None, "unfinished_downloads/part.bin": None}

        assert checked_rows([folder, child], seen) == []


class TestMovedOutCandidates:
    HINT = _row(ino=7, path="unfinished_downloads/ep.mkv")
    MOVED = _row(ino=7, path="finished/ep.mkv")

    def test_a_file_moved_into_the_synced_tree_qualifies(self):
        # Behavior: the one thing a run with the excludes can do under an
        # excluded name is take a file out.
        # Bug caught: no candidates at all would copy a file the backup
        # already holds (the reproduced case 3).
        trusted = [_row(ino=1, path="docs/a.txt")]

        assert moved_out_candidates([self.HINT], trusted, [self.MOVED]) == [self.HINT]

    @pytest.mark.parametrize(
        ("hints", "trusted", "after"),
        [
            ([HINT], [], [_row(ino=7, path="finished/ep.mkv", size=11)]),
            ([HINT], [], [_row(ino=7, path="finished/ep.mkv", mtime_ns=_T + 1)]),
            ([HINT], [_row(ino=7, path="docs/link.mkv")], [MOVED]),
            ([HINT], [], [MOVED, _row(ino=7, path="finished/link.mkv")]),
            (
                [_row(ino=7, path="unfinished_downloads/Pack", type="d")],
                [],
                [_row(ino=7, path="finished/Pack", type="d")],
            ),
            ([HINT], [], [_row(ino=8, path="finished/other.mkv")]),
            ([HINT], [], []),
        ],
        ids=[
            "size-changed",
            "mtime-changed",
            "hard-link-in-the-baseline",
            "hard-link-on-the-source",
            "a-folder",
            "another-inode",
            "gone-from-the-synced-tree",
        ],
    )
    def test_anything_else_does_not(self, hints, trusted, after):
        # Behavior: a hint qualifies only when it is certain to become a
        # file move.
        # Bug caught: a hint admitted without that certainty is left over
        # by the diff and listed as deleted under an excluded name, in a
        # run that deletes nothing there.
        # Expected values: each case breaks exactly one condition named in
        # the design's section 2.
        assert moved_out_candidates(hints, trusted, after) == []

    def test_admitting_only_candidates_lists_nothing_under_an_excluded_name(self):
        # Behavior (design section 2): in a run with the excludes no path
        # under an excluded name is listed as modified, created or deleted.
        # Bug caught: admitting every hint whose inode is still on the
        # source would list the edited file's old path as deleted.
        # Expected values: one file moved out unchanged (a move), one moved
        # out after an edit (a creation), one deleted and one edited in
        # place under the excluded name (nothing).
        hints = [
            _row(ino=10, path="unfinished_downloads/same.mkv"),
            _row(ino=11, path="unfinished_downloads/edited.mkv"),
            _row(ino=12, path="unfinished_downloads/deleted.part"),
            _row(ino=13, path="unfinished_downloads/growing.part"),
        ]
        trusted = [_row(ino=1, path="docs", type="d")]
        after = [
            _row(ino=1, path="docs", type="d"),
            _row(ino=10, path="docs/same.mkv"),
            _row(ino=11, path="docs/edited.mkv", size=99),
        ]

        candidates = moved_out_candidates(hints, trusted, after)
        changes = compute_changes(trusted + candidates, after)

        assert changes.file_moves == [
            ("unfinished_downloads/same.mkv", "docs/same.mkv")
        ]
        assert changes.created == ["docs/edited.mkv"]
        assert (changes.deleted, changes.modified, changes.dir_moves) == ([], [], [])
```

- [ ] **Step 2: Run them and see them fail**

Run: `pixi run test tests/test_baseline.py -q`
Expected: a collection error, `ModuleNotFoundError: No module named 'irsync.baseline'`.

- [ ] **Step 3: Implement**

Create `src/irsync/baseline.py`:

```python
"""Decide what a baseline may say about the backup under the default-excluded names.

A snapshot records every row of the source, but a run with the default
excludes never copies the excluded names, so a baseline row under one of
them says what the source had, not what the backup holds. Such a row is a
hint. The functions here pick the hints worth asking the destination about
and turn its answers into rows the diff can trust. They do no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from irsync.destination import Seen
from irsync.diff import index_by_inode, same_file
from irsync.snapshot import Row

_NS_PER_SECOND = 1_000_000_000


def split_hints(
    rows: Iterable[Row], unvouched: Callable[[str], bool]
) -> tuple[list[Row], list[Row]]:
    """Split baseline rows into those the backup is known to hold and the hints.

    Args:
        rows: The baseline's rows.
        unvouched: A predicate true for a path under a default-excluded
            name (:func:`~irsync.rsync_runner.exclude_matcher` on the default
            exclude list, whatever ``--no-exclude`` says).

    Returns:
        ``(trusted, hints)``, each in the baseline's order.
    """
    trusted: list[Row] = []
    hints: list[Row] = []
    for row in rows:
        (hints if unvouched(row["path"]) else trusted).append(row)
    return trusted, hints


def holds(row: Row, there: Seen | None) -> bool:
    """Return True if the destination holds what a baseline row describes.

    For a regular file this is rsync's own quick check: the same size, and
    the same mtime to the whole second. irsync then never lists a move that
    rsync would follow with a transfer, and never refuses one rsync would
    have left alone. Any other entry only has to be of the row's type.

    Args:
        row: A baseline row.
        there: What the destination holds at the row's path, or None.

    Returns:
        True if the destination's entry matches the row.
    """
    if there is None or there.type != row["type"]:
        return False
    if row["type"] != "f":
        return True
    return (
        row["mtime_ns"] != -1
        and there.size == row["size"]
        and there.mtime_ns // _NS_PER_SECOND == row["mtime_ns"] // _NS_PER_SECOND
    )


def checked_rows(hints: Iterable[Row], seen: Mapping[str, Seen | None]) -> list[Row]:
    """Replace each hint by what the destination holds at its path.

    Used when rsync syncs the excluded names (``--no-exclude``), so that the
    diff's "before" side describes the backup:

    - nothing there: the hint is dropped, and the diff reports the path as
      created if the source still has it;
    - an entry matching the hint (:func:`holds`): the hint is kept as is,
      and can become a move;
    - anything else: the hint is kept with the destination's type, size and
      mtime, so the diff reports the path as modified, or as deleted and
      created.

    Args:
        hints: Baseline rows under a default-excluded name.
        seen: The destination's answer for each hint's path.

    Returns:
        The rows to put on the diff's "before" side, in the hints' order.
    """
    out: list[Row] = []
    for row in hints:
        there = seen.get(row["path"])
        if there is None:
            continue
        if holds(row, there):
            out.append(row)
            continue
        out.append(
            Row(
                dev=row["dev"],
                ino=row["ino"],
                type=there.type,
                nlink=row["nlink"],
                size=there.size,
                mtime_ns=there.mtime_ns,
                btime_ns=row["btime_ns"],
                path=row["path"],
            )
        )
    return out


def moved_out_candidates(
    hints: Iterable[Row], trusted: Iterable[Row], after: Iterable[Row]
) -> list[Row]:
    """Pick the hints that can become a file move in a run with the excludes.

    Such a run leaves the excluded names on the destination alone, so the
    only thing irsync can do there is take a file out. A hint qualifies when
    it is certain to become a move if the destination holds it: a regular
    file whose inode has exactly one path in the baseline (the hint's) and
    exactly one in ``after``, where it is still a regular file and the same
    file (:func:`~irsync.diff.same_file`). Anything else under an excluded
    name stays out of the diff, so no path there can be listed as modified,
    created, or deleted.

    Args:
        hints: Baseline rows under a default-excluded name.
        trusted: The other baseline rows.
        after: The fresh rows rsync will sync; none is under an excluded
            name.

    Returns:
        The qualifying hints, in the hints' order.
    """
    hints = list(hints)
    hint_keys = {(h["dev"], h["ino"]) for h in hints}
    now = index_by_inode(r for r in after if (r["dev"], r["ino"]) in hint_keys)
    if not now:
        return []
    before = index_by_inode(
        r for r in (*trusted, *hints) if (r["dev"], r["ino"]) in now
    )
    out: list[Row] = []
    for h in hints:
        key = (h["dev"], h["ino"])
        a = now.get(key)
        if a is None or h["type"] != "f" or a["type"] != "f":
            continue
        b = before[key]
        if b["paths"] == {h["path"]} and len(a["paths"]) == 1 and same_file(b, a):
            out.append(h)
    return out
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `pixi run test tests/test_baseline.py -q` → `30 passed`.
Run: `pixi run test -q` → `482 passed, 1 skipped`.

- [ ] **Step 5: Gate and commit**

`pixi run test-cov` must show `src/irsync/baseline.py` at 100%.

```bash
pixi run pre-commit run --all-files
pixi run test-cov
git add src/irsync/baseline.py tests/test_baseline.py
git commit -m "feat: check baseline rows under excluded names against the destination

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: A `--no-exclude` run asks the backup

**Goal:** With a local destination, a `--no-exclude` run diffs against what the backup holds under the default-excluded names, replays through the destination interface, and refuses when the backup cannot be read.

**Files:**
- Modify: `src/irsync/backup.py` (imports; `_format_preview`; `_show_preview`; new `_plan_changes`; `_run_backup_for_endpoints`)
- Create: `tests/test_excluded_names.py`
- Modify: `tests/test_backup.py` (`TestFormatPreview`, `TestCrossDeviceReplayHandled`, `TestProvenanceMismatchRejected`, `TestNoExcludeReachesRsync`)

**Acceptance Criteria:**
- [ ] Spec case 1: after runs with the excludes, a file moved out of `unfinished_downloads` is listed as created under `--no-exclude` (`Diff: 0 dir moves, 0 file moves, ...`).
- [ ] Spec case 2: deletions and an edit in an excluded folder the backup never held are not listed and do not trip the >50% guard; when the backup does hold them they are listed and counted (refusal at 8 of 15).
- [ ] Spec case 4: a stale copy at the old path gives 1 created and 1 deleted, not a move.
- [ ] A second `--no-exclude` run in a row is still a file move, and the backup's copy keeps its inode.
- [ ] A folder moved out is one directory move, with a missing child created and a stale child deleted and created.
- [ ] A copy whose mtime differs only within the second is a move that rsync then leaves alone; one a second apart is not a move.
- [ ] A lookup error refuses the run with exit 2 and a message starting `Refusing: cannot read the backup at `; rsync is not started; backup and baseline are unchanged.
- [ ] For a local destination the `--no-exclude` note has the spec's three new lines; `_format_preview` without `checked=True` prints the old ones.
- [ ] The replay goes through `destination.replay`; `backup.py` no longer imports `apply_moves`.
- [ ] A run with the excludes and a run to a remote destination behave as before: every other pre-existing test passes unmodified.
- [ ] `pixi run test -q` ends with `494 passed, 1 skipped`.

**Verify:** `pixi run test tests/test_excluded_names.py tests/test_backup.py -q` → no failures; then `pixi run test -q` → `494 passed, 1 skipped`

**What each test is for** (all in `tests/test_excluded_names.py::TestNoExcludeRun` unless a file is named):

| Test | Red first? | Bug it would catch |
|---|---|---|
| `test_a_file_the_backup_lacks_is_created_not_moved` | red | The baseline row for the old path was taken as proof that the backup holds the file, so the preview said "File moves (1)" for a full copy (case 1). |
| `test_changes_the_backup_never_held_are_neither_listed_nor_counted` | red | 8 deleted partial downloads counted against 15 recorded entries, and the run was refused though it deletes nothing (case 2). |
| `test_deletions_the_backup_does_hold_are_listed_and_counted` | guard | A fix that ignores every row under an excluded name would let rsync delete 8 of the backup's 15 entries with the guard silent. |
| `test_a_stale_copy_is_deleted_and_created_not_moved` | red | The preview said "move", the replay renamed the stale copy, and rsync re-sent the whole file (case 4). |
| `test_a_current_copy_is_still_moved_with_nothing_sent` | guard | Dropping every hint would send the whole file again where a rename is enough. |
| `test_a_folder_moved_out_is_a_directory_move_with_its_files_checked` | red | The files inside were taken on trust, so the preview showed one directory move while rsync sent two files. |
| `test_what_irsync_accepts_as_a_move_rsync_leaves_alone[same-second]` | guard | A nanosecond comparison would refuse a move rsync would have left alone, and copy the file. |
| `test_what_irsync_accepts_as_a_move_rsync_leaves_alone[a-second-apart]` | red | A looser comparison would list a move that rsync then re-sends. |
| `test_a_backup_that_cannot_be_read_refuses_the_run` | red | Treating the error as "nothing there" would list the backup's files as created and hand the run to rsync. |
| `test_the_note_says_the_lists_were_checked_against_the_backup` | red | The old note ("the lists show only what changed on the source") would now be false. |
| `test_an_unchanged_source_whose_backup_holds_everything_still_runs_rsync` | guard | Restoring the short-circuit on an empty diff would leave a file only the backup has under an excluded name there for good. |
| `tests/test_backup.py::TestFormatPreview::test_checked_lists_get_the_note_that_names_what_they_cannot_show` | red | The same note, at the formatting function: the earlier text would be printed for a run whose lists were checked. |

Three existing tests in `tests/test_backup.py` change because the code they watch moves:

- `TestNoExcludeReachesRsync::test_unchanged_source_copies_what_earlier_runs_skipped` asserted the log line "No source changes since last backup, but --no-exclude: running rsync anyway ...". On that tree the backup lacks the excluded names, so the diff now lists them as created (7 entries) and the line is not logged. The test asserts the `Diff:` line instead; everything else it checks stays. The log line itself is still pinned, for the case where it still applies, by `test_an_unchanged_source_whose_backup_holds_everything_still_runs_rsync`.
- `TestCrossDeviceReplayHandled::test_5th_h2_xdev_returns_clean_error_no_partial_state_no_snapshot` and `TestProvenanceMismatchRejected::test_snapshot_from_different_tree_is_rejected` replace `apply_moves` by patching the name in `irsync.backup`. The call now lives in `irsync.destination`, so they patch it there. Their assertions do not change.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/test_excluded_names.py`:

```python
"""End to end: under an excluded name, irsync lists and makes only what the backup can.

Every test here runs the real rsync on real files, because the behaviour
rests on what ``lstat`` and rsync report (2026-11-25 design). "Excluded
name" means one of the default excludes, here ``unfinished_downloads``.
"""

import logging
import os
from pathlib import Path

import pytest

from irsync.backup import EXIT_REFUSED, run_backup
from irsync.snapshot import SNAPSHOT_FILENAME

from .conftest import cli_args as _args
from .conftest import inode_for, tree_signature

DL = "unfinished_downloads"
NOTE_HEADER = (
    "=== --no-exclude: rsync also syncs .Trash-*, .cache, unfinished_downloads ==="
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _tree(tmp_path: Path, *files: str) -> tuple[Path, Path]:
    """Create src holding ``files`` (each file's content is its path) and an empty dest."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    for rel in files:
        _write(src / rel, rel)
    dest.mkdir()
    return src, dest


def _backup(src, dest, options, caplog, **flags):
    """Run one backup; return its exit code and its "Diff:" log lines."""
    caplog.clear()
    with caplog.at_level(logging.INFO):
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=options,
            args=_args(**flags),
        )
    return rc, [m for m in caplog.messages if m.startswith("Diff:")]


def _make_stale(path: Path) -> None:
    """Turn a backup copy into an older, shorter one, as a partial download is."""
    path.write_text("partial")
    os.utime(path, ns=(1_600_000_000 * 10**9, 1_600_000_000 * 10**9))


class TestNoExcludeRun:
    """A ``--no-exclude`` run asks the backup about every row under an excluded name."""

    def test_a_file_the_backup_lacks_is_created_not_moved(
        self, tmp_path, basic_options, caplog
    ):
        # Behavior (reproduced case 1): after runs with the excludes, a
        # file moved out of an excluded folder is listed as created.
        # Bug caught: the baseline row for the old path was taken as proof
        # that the backup holds the file, so the preview said "File moves
        # (1)" for what is a full copy.
        # Expected values: hand count. Created: finished/ep.mkv, and the
        # now empty unfinished_downloads folder the backup never had.
        src, dest = _tree(tmp_path, f"{DL}/ep.mkv", "finished/other.txt", "keep")
        assert _backup(src, dest, basic_options, caplog)[0] == 0
        assert not (dest / DL).exists()
        (src / DL / "ep.mkv").rename(src / "finished" / "ep.mkv")

        rc, diffs = _backup(src, dest, basic_options, caplog, no_exclude=True)

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 0 file moves, 0 modified, 2 created, 0 deleted."
        ]
        assert (dest / "finished" / "ep.mkv").read_text() == f"{DL}/ep.mkv"

    def test_changes_the_backup_never_held_are_neither_listed_nor_counted(
        self, tmp_path, basic_options, caplog
    ):
        # Behavior (reproduced case 2): deleting and editing files in an
        # excluded folder the backup never received deletes nothing from
        # the backup, so nothing is listed as deleted and the >50% guard
        # stays quiet.
        # Bug caught: the 8 deleted partial downloads counted against the
        # 15 recorded entries (53%), and the run was refused though it
        # deletes nothing; the edited one was listed as "re-transferred".
        # Expected values: hand count. Created: the folder and part9.
        parts = [f"{DL}/part{i}" for i in range(1, 10)]
        src, dest = _tree(tmp_path, *parts, "docs/real1", "docs/real2", "docs/real3")
        assert _backup(src, dest, basic_options, caplog)[0] == 0
        for rel in parts[:8]:
            (src / rel).unlink()
        _write(src / parts[8], "more content now")

        rc, diffs = _backup(src, dest, basic_options, caplog, no_exclude=True)

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 0 file moves, 0 modified, 2 created, 0 deleted."
        ]
        assert (dest / parts[8]).read_text() == "more content now"

    def test_deletions_the_backup_does_hold_are_listed_and_counted(
        self, tmp_path, basic_options, caplog
    ):
        # Behavior: when the backup holds the excluded folder, deleting
        # most of the tree there is a mass deletion like any other.
        # Bug caught: a fix that simply ignores every row under an excluded
        # name would let rsync delete 8 of the backup's 15 entries with the
        # guard silent and the deletions unlisted.
        # Expected values: 8 of 15 rows (the root, docs and its 3 files,
        # the folder and its 9 parts) is 53%.
        parts = [f"{DL}/part{i}" for i in range(1, 10)]
        src, dest = _tree(tmp_path, *parts, "docs/real1", "docs/real2", "docs/real3")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        for rel in parts[:8]:
            (src / rel).unlink()

        rc, _diffs = _backup(src, dest, basic_options, caplog, no_exclude=True)

        assert rc == EXIT_REFUSED
        assert any(
            "would delete 8 of 15 previously-recorded entries (53%)" in m
            for m in caplog.messages
        )
        assert (dest / parts[0]).is_file()

        rc, diffs = _backup(
            src, dest, basic_options, caplog, no_exclude=True, allow_massive_delete=True
        )

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 0 file moves, 0 modified, 0 created, 8 deleted."
        ]
        assert sorted(p.name for p in (dest / DL).iterdir()) == ["part9"]

    def test_a_stale_copy_is_deleted_and_created_not_moved(
        self, tmp_path, basic_options, caplog
    ):
        # Behavior (reproduced case 4): when the backup's copy at the old
        # path is not the file the source has now, the preview says what
        # rsync does: remove the old copy, send the file.
        # Bug caught: the preview said "move", the replay renamed the stale
        # copy, and rsync then re-sent the whole file.
        # Expected values: hand count. (The new copy's inode proves nothing
        # here: the filesystem may hand the deleted copy's number straight
        # to the file rsync writes.)
        src, dest = _tree(tmp_path, f"{DL}/ep.mkv", "finished/other.txt", "keep")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        _make_stale(dest / DL / "ep.mkv")
        (src / DL / "ep.mkv").rename(src / "finished" / "ep.mkv")

        rc, diffs = _backup(src, dest, basic_options, caplog, no_exclude=True)

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 0 file moves, 0 modified, 1 created, 1 deleted."
        ]
        assert (dest / "finished" / "ep.mkv").read_text() == f"{DL}/ep.mkv"
        assert not (dest / DL / "ep.mkv").exists()

    def test_a_current_copy_is_still_moved_with_nothing_sent(
        self, tmp_path, basic_options, caplog
    ):
        # Behavior: a second --no-exclude run in a row renames the copy the
        # first one made. Passes before this change too; it pins the one
        # case that was already right.
        # Bug caught: dropping every hint (the rejected option A) would
        # send the whole file again.
        # Expected values: the copy keeps its inode, which only a rename
        # does; rsync writes a new file when it sends one.
        src, dest = _tree(tmp_path, f"{DL}/ep.mkv", "finished/other.txt", "keep")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        copy_inode = inode_for(dest / DL / "ep.mkv")
        (src / DL / "ep.mkv").rename(src / "finished" / "ep.mkv")

        rc, diffs = _backup(src, dest, basic_options, caplog, no_exclude=True)

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 1 file moves, 0 modified, 0 created, 0 deleted."
        ]
        assert inode_for(dest / "finished" / "ep.mkv") == copy_inode
        assert not (dest / DL / "ep.mkv").exists()

    def test_a_folder_moved_out_is_a_directory_move_with_its_files_checked(
        self, tmp_path, basic_options, caplog
    ):
        # Behavior: the folder is renamed on the backup; inside it, a file
        # the backup holds rides along, a missing one is created, a stale
        # one is deleted and created.
        # Bug caught: the files inside were taken on trust, so the preview
        # showed one directory move and nothing else while rsync sent two
        # files.
        # Expected values: hand count; e1 keeps its inode (renamed with the
        # folder), e2 and e3 end up with the source's content.
        pack = f"{DL}/Pack"
        src, dest = _tree(
            tmp_path, f"{pack}/e1", f"{pack}/e2", f"{pack}/e3", "finished/other.txt"
        )
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        e1_inode = inode_for(dest / pack / "e1")
        _make_stale(dest / pack / "e2")
        (dest / pack / "e3").unlink()
        (src / pack).rename(src / "finished" / "Pack")

        rc, diffs = _backup(src, dest, basic_options, caplog, no_exclude=True)

        assert rc == 0
        assert diffs == [
            "Diff: 1 dir moves, 0 file moves, 0 modified, 2 created, 1 deleted."
        ]
        moved = dest / "finished" / "Pack"
        assert inode_for(moved / "e1") == e1_inode
        assert (moved / "e2").read_text() == f"{pack}/e2"
        assert (moved / "e3").read_text() == f"{pack}/e3"
        assert not (dest / pack).exists()

    @pytest.mark.parametrize(
        ("shift_ns", "moved"),
        [(-400_000_000, True), (1_000_000_000, False)],
        ids=["same-second", "a-second-apart"],
    )
    def test_what_irsync_accepts_as_a_move_rsync_leaves_alone(
        self, tmp_path, basic_options, caplog, shift_ns, moved
    ):
        # Behavior: the backup's copy counts as the same file when its
        # mtime agrees to the whole second, which is rsync's own test.
        # Bug caught: a stricter test (nanoseconds) would refuse a move
        # rsync would have left alone, and copy the file; a looser one
        # would list a move that rsync then re-sends.
        # Expected values: rsync 3.4.1 leaves a copy alone when only the
        # nanoseconds differ and re-sends it when a second apart (design
        # doc, Decisions). A copy that was renamed and then left alone
        # keeps its inode: had rsync re-sent it, the new file would have
        # been written while the renamed one still held that number.
        mtime = 1_700_000_000 * 10**9 + 500_000_000
        src, dest = _tree(tmp_path, f"{DL}/ep.mkv", "finished/other.txt", "keep")
        os.utime(src / DL / "ep.mkv", ns=(mtime, mtime))
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        copy = dest / DL / "ep.mkv"
        os.utime(copy, ns=(mtime + shift_ns, mtime + shift_ns))
        copy_inode = inode_for(copy)
        (src / DL / "ep.mkv").rename(src / "finished" / "ep.mkv")

        rc, diffs = _backup(src, dest, basic_options, caplog, no_exclude=True)

        assert rc == 0
        expected = (
            "Diff: 0 dir moves, 1 file moves, 0 modified, 0 created, 0 deleted."
            if moved
            else "Diff: 0 dir moves, 0 file moves, 0 modified, 1 created, 1 deleted."
        )
        assert diffs == [expected]
        if moved:
            assert inode_for(dest / "finished" / "ep.mkv") == copy_inode
        assert (dest / "finished" / "ep.mkv").read_text() == f"{DL}/ep.mkv"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_a_backup_that_cannot_be_read_refuses_the_run(
        self, tmp_path, basic_options, caplog, monkeypatch
    ):
        # Behavior: an error while looking at the backup refuses the run
        # before anything is written.
        # Bug caught: treating the error as "nothing there" would list the
        # backup's files as created and hand the run to rsync.
        # Expected values: exit 2, rsync never started, the backup and the
        # baseline byte for byte as they were.
        src, dest = _tree(tmp_path, f"{DL}/part", "keep")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        _write(src / "new.txt", "new")
        backup_before = tree_signature(dest)
        baseline_before = (src / SNAPSHOT_FILENAME).read_bytes()
        rsync_runs: list[list[str]] = []
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: rsync_runs.append(cmd) or 0
        )
        (dest / DL).chmod(0)
        try:
            rc, _diffs = _backup(src, dest, basic_options, caplog, no_exclude=True)
        finally:
            (dest / DL).chmod(0o755)

        assert rc == EXIT_REFUSED
        assert any(
            m.startswith("Refusing: cannot read the backup at ")
            and f"{DL}/part" in m
            and "ermission denied" in m
            for m in caplog.messages
        )
        assert rsync_runs == []
        assert tree_signature(dest) == backup_before
        assert (src / SNAPSHOT_FILENAME).read_bytes() == baseline_before

    def test_the_note_says_the_lists_were_checked_against_the_backup(
        self, tmp_path, basic_options, caplog, capsys
    ):
        # Behavior: for a local destination the note says the lists cover
        # the excluded names and names the one thing they cannot show.
        # Bug caught: the old note ("the lists show only what changed on
        # the source") would now be false.
        # Expected values: the note's text in the design's section 2.
        src, dest = _tree(tmp_path, f"{DL}/part", "keep")
        assert _backup(src, dest, basic_options, caplog)[0] == 0
        capsys.readouterr()

        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0

        lines = capsys.readouterr().out.splitlines()
        start = lines.index(NOTE_HEADER)
        assert lines[start + 1 : start + 4] == [
            "The lists below cover these names too, checked against the backup. They",
            "cannot show one thing: a file there that only the backup has and no earlier",
            "run recorded, which rsync DELETES. --dry-run shows every file.",
        ]

    def test_an_unchanged_source_whose_backup_holds_everything_still_runs_rsync(
        self, tmp_path, basic_options, caplog
    ):
        # Behavior (AD-35): an empty diff under --no-exclude still reaches
        # rsync, because only rsync finds what no baseline recorded.
        # Bug caught: restoring the short-circuit on an empty diff would
        # leave a file that only the backup has under an excluded name
        # there for good.
        # Expected values: the stray file is gone after the run.
        src, dest = _tree(tmp_path, f"{DL}/part", "keep")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        _write(dest / DL / "stray.part", "only on the backup")

        rc, diffs = _backup(src, dest, basic_options, caplog, no_exclude=True)

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 0 file moves, 0 modified, 0 created, 0 deleted."
        ]
        assert (
            "No source changes since last backup, but --no-exclude: running "
            "rsync anyway so the backup gets what the default excludes skip "
            "(.Trash-*, .cache, unfinished_downloads)."
        ) in caplog.messages
        assert not (dest / DL / "stray.part").exists()
```

Change `tests/test_backup.py`:

```diff
--- a/tests/test_backup.py
+++ b/tests/test_backup.py
@@ -127,6 +127,28 @@
         # Bug caught: a note printed on every run would teach the user to
         # skip it, and would be false for a run that keeps the excludes.
         assert _format_preview(Changes()).startswith("=== Directory moves (0) ===")
+
+    def test_checked_lists_get_the_note_that_names_what_they_cannot_show(self):
+        # Behavior: when the lists cover the excluded names, checked
+        # against the backup, the note says so and names the one thing
+        # they cannot show.
+        # Bug caught: the earlier note ("the lists show only what changed
+        # on the source") would be false for such a run.
+        # Expected values: the note's text in the 2026-11-25 design,
+        # section 2; its first line is unchanged.
+        lines = _format_preview(
+            Changes(),
+            unexcluded=[".Trash-*", ".cache", "unfinished_downloads"],
+            checked=True,
+        ).splitlines()
+        assert lines[:5] == [
+            "=== --no-exclude: rsync also syncs .Trash-*, .cache, "
+            "unfinished_downloads ===",
+            "The lists below cover these names too, checked against the backup. They",
+            "cannot show one thing: a file there that only the backup has and no earlier",
+            "run recorded, which rsync DELETES. --dry-run shows every file.",
+            "=== Directory moves (0) ===",
+        ]


 class TestPreviewNamesSurviveStrictOutput:
@@ -860,7 +882,7 @@
         def always_refuse(*, dir_moves, file_moves, dest_root):
             raise CrossDeviceMoveError(18, "simulated EXDEV from pre-flight")

-        monkeypatch.setattr("irsync.backup.apply_moves", always_refuse)
+        monkeypatch.setattr("irsync.destination.apply_moves", always_refuse)
         # Capture rsync to make sure it doesn't run after the refusal.
         rsync_calls: list[list[str]] = []
         monkeypatch.setattr(
@@ -916,7 +938,7 @@
         # (because the provenance mismatch falls back to "first backup"),
         # not that apply_moves replayed the wrong moves.
         replays: list[tuple] = []
-        from irsync import backup as backup_mod
+        from irsync import destination as destination_mod
         from irsync.replay import apply_moves as real_apply_moves

         def recording_apply(*, dir_moves, file_moves, dest_root):
@@ -925,7 +947,7 @@
                 dir_moves=dir_moves, file_moves=file_moves, dest_root=dest_root
             )

-        monkeypatch.setattr(backup_mod, "apply_moves", recording_apply)
+        monkeypatch.setattr(destination_mod, "apply_moves", recording_apply)

         rc = run_backup(
             source_arg=str(src_b),
@@ -2732,11 +2754,14 @@
         self, tmp_path, basic_options, caplog, capsys
     ):
         # Behavior: on an unchanged source, --no-exclude still runs rsync,
-        # says why, and copies every excluded file to the backup.
+        # lists what the backup lacks under the excluded names as created,
+        # and copies every excluded file to the backup.
         # Bug caught: the no-change short-circuit returned first, so the run
         # logged "backup drive untouched" and copied nothing.
         # Expected values: the hand-built source tree; every file in it must
-        # now be on the backup, beside the backup's snapshot copy.
+        # now be on the backup, beside the backup's snapshot copy. Created:
+        # the 3 skipped files and the 4 folders that hold them (.Trash-1000,
+        # .Trash-1000/files, Films/.cache, unfinished_downloads).
         src, dest = self._baselined(tmp_path, basic_options)
         capsys.readouterr()

@@ -2745,10 +2770,9 @@

         assert rc == 0
         assert (
-            "No source changes since last backup, but --no-exclude: running "
-            "rsync anyway so the backup gets what the default excludes skip "
-            "(.Trash-*, .cache, unfinished_downloads)."
-        ) in caplog.messages
+            "Diff: 0 dir moves, 0 file moves, 0 modified, 7 created, 0 deleted."
+            in caplog.messages
+        )
         assert sorted(tree_signature(dest)) == sorted(
             [SNAPSHOT_FILENAME, "Films/a.mkv", *self.SKIPPED]
         )
```

- [ ] **Step 2: Run them and see them fail**

Run: `pixi run test tests/test_excluded_names.py tests/test_backup.py -q`
Expected: 10 failures and no errors.

In `tests/test_excluded_names.py`, the seven tests marked red above fail and the four guards pass:

- `test_a_file_the_backup_lacks_is_created_not_moved`, `test_a_stale_copy_is_deleted_and_created_not_moved` and `...[a-second-apart]`: the `Diff:` line says `1 file moves`.
- `test_changes_the_backup_never_held_are_neither_listed_nor_counted`: the exit code is 2 (refused: 8 of 15).
- `test_a_folder_moved_out_is_a_directory_move_with_its_files_checked`: the `Diff:` line says `1 dir moves, 0 file moves, 0 modified, 0 created, 0 deleted`.
- `test_a_backup_that_cannot_be_read_refuses_the_run`: the exit code is 0 and rsync was started.
- `test_the_note_says_the_lists_were_checked_against_the_backup`: the note still has its old lines.

In `tests/test_backup.py`, three fail:

- `test_checked_lists_get_the_note_that_names_what_they_cannot_show`: `TypeError: _format_preview() got an unexpected keyword argument 'checked'`.
- `test_unchanged_source_copies_what_earlier_runs_skipped`: the `Diff:` line says `0 created`.
- `test_5th_h2_xdev_returns_clean_error_no_partial_state_no_snapshot`: the patch no longer reaches the call, so the exit code is 0.

- [ ] **Step 3: Implement**

Change `src/irsync/backup.py`. Under `--no-exclude` with a local destination, `_plan_changes` asks the destination about every baseline row under a default-excluded name; every other run still diffs what rsync transfers, exactly as before. The replay goes through `destination.replay`, the note takes a `checked` flag, and a lookup failure is a refusal.

```diff
--- a/src/irsync/backup.py
+++ b/src/irsync/backup.py
@@ -13,6 +13,8 @@
 from collections.abc import Iterator, Sequence
 from pathlib import Path

+from irsync.baseline import checked_rows, split_hints
+from irsync.destination import Destination, DestinationUnreadable, LocalDestination
 from irsync.diff import Changes, compute_changes
 from irsync.display import display_name
 from irsync.options import Endpoints, Options, resolve_endpoints, resolve_source
@@ -31,7 +33,7 @@
     split_top_level,
     top_level_names,
 )
-from irsync.replay import CrossDeviceMoveError, apply_moves
+from irsync.replay import CrossDeviceMoveError
 from irsync.rsync_runner import (
     build_rsync_command,
     exclude_matcher,
@@ -159,7 +161,9 @@
     _atomic_write_snapshot(rows, src_root, src_root / SNAPSHOT_FILENAME)


-def _format_preview(changes: Changes, *, unexcluded: Sequence[str] = ()) -> str:
+def _format_preview(
+    changes: Changes, *, unexcluded: Sequence[str] = (), checked: bool = False
+) -> str:
     """Build the full pre-confirmation preview text — every move, deletion, creation.

     The deletion list is the safety-critical part: rsync's ``--delete-before`` will
@@ -171,9 +175,12 @@
     Args:
         changes: The snapshot diff to list.
         unexcluded: The default exclude patterns this run does not apply
-            (``--no-exclude``). When non-empty, a note opens the preview:
-            rsync also syncs those names on the backup, copying and deleting
-            files the lists do not show.
+            (``--no-exclude``). When non-empty, a note opens the preview
+            saying that rsync also syncs those names on the backup.
+        checked: Whether the lists cover those names, checked against the
+            backup (a local destination). The note then names the one thing
+            the lists cannot show; otherwise it says they show only what
+            changed on the source.

     Returns:
         The preview text, ending in a newline.
@@ -181,15 +188,30 @@
     lines: list[str] = []
     if unexcluded:
         lines.append(f"=== --no-exclude: rsync also syncs {', '.join(unexcluded)} ===")
-        lines.append(
-            "On the backup, rsync makes these match the source, copying and DELETING"
-        )
-        lines.append(
-            "files there. The lists below show only what changed on the source since"
-        )
-        lines.append(
-            "the last backup; --dry-run shows every file rsync will copy or delete."
-        )
+        if checked:
+            lines.append(
+                "The lists below cover these names too, checked against the backup. "
+                "They"
+            )
+            lines.append(
+                "cannot show one thing: a file there that only the backup has and no "
+                "earlier"
+            )
+            lines.append(
+                "run recorded, which rsync DELETES. --dry-run shows every file."
+            )
+        else:
+            lines.append(
+                "On the backup, rsync makes these match the source, copying and "
+                "DELETING"
+            )
+            lines.append(
+                "files there. The lists below show only what changed on the source "
+                "since"
+            )
+            lines.append(
+                "the last backup; --dry-run shows every file rsync will copy or delete."
+            )
     lines.append(f"=== Directory moves ({len(changes.dir_moves)}) ===")
     for o, n in changes.dir_moves:
         lines.append(f"  {display_name(o)}  ->  {display_name(n)}")
@@ -402,12 +424,61 @@


 def _show_preview(
-    changes: Changes, *, interactive: bool, unexcluded: Sequence[str] = ()
+    changes: Changes,
+    *,
+    interactive: bool,
+    unexcluded: Sequence[str] = (),
+    checked: bool = False,
 ) -> None:
     """Render the change preview, paging through ``less`` if interactive."""
     _emit_preview(
-        _format_preview(changes, unexcluded=unexcluded), interactive=interactive
-    )
+        _format_preview(changes, unexcluded=unexcluded, checked=checked),
+        interactive=interactive,
+    )
+
+
+def _plan_changes(
+    before_rows: list[Row],
+    fresh_rows: list[Row],
+    *,
+    exclude_dirs: Sequence[str],
+    no_exclude: bool,
+    destination: Destination | None,
+) -> tuple[Changes, list[Row]]:
+    """Diff the baseline against the fresh snapshot, asking the backup where needed.
+
+    A baseline row under a default-excluded name says what the source had,
+    not what the backup holds, since a run with the excludes never copies
+    those names. A ``--no-exclude`` run to a local destination syncs those
+    names, so it asks the destination about every such row and diffs
+    against what the backup holds (AD-36). Every other run diffs only what
+    rsync transfers, as before (AD-32).
+
+    Args:
+        before_rows: The baseline's rows.
+        fresh_rows: The source's fresh snapshot rows.
+        exclude_dirs: The default exclude patterns.
+        no_exclude: Whether rsync syncs the excluded names in this run.
+        destination: Where to look, or None for a remote destination.
+
+    Returns:
+        ``(changes, before)``: the diff, and the baseline rows it was
+        computed against, which the >50% guard counts deletions against.
+
+    Raises:
+        DestinationUnreadable: If the destination cannot be examined.
+    """
+    skipped = exclude_matcher([] if no_exclude else exclude_dirs)
+    after = [r for r in fresh_rows if not skipped(r["path"])]
+    if no_exclude and destination is not None:
+        trusted, hints = split_hints(before_rows, exclude_matcher(exclude_dirs))
+        # No hints, no lookup: with an empty exclude list a run that has
+        # nothing to do must still leave the backup drive alone (AD-2).
+        seen = destination.look([r["path"] for r in hints]) if hints else {}
+        before = trusted + checked_rows(hints, seen)
+    else:
+        before = [r for r in before_rows if not skipped(r["path"])]
+    return compute_changes(before, after), before


 def _confirm_or_abort(args: argparse.Namespace) -> bool:
@@ -787,16 +858,35 @@
     # copied these, so the backup can lack them however unchanged the source
     # is, and rsync syncs them without the diff showing it.
     unexcluded = options.exclude_dirs if args.no_exclude else []
+    # Where the backup can be examined and renamed on; None for a remote one.
+    destination: Destination | None = (
+        LocalDestination(dest_root) if dest_root is not None else None
+    )
     if have_before:
         # Diff only what rsync transfers. A move into an excluded directory,
         # such as a file sent to the desktop trash, is then a deletion from
         # the backup instead of a move replayed where --delete-before never
-        # reaches it. before_rows is narrowed too, so the >50% guard below
-        # counts deletions against the same tree.
-        before_rows = [r for r in before_rows if not excluded(r["path"])]
-        changes = compute_changes(
-            before_rows, [r for r in fresh_rows if not excluded(r["path"])]
-        )
+        # reaches it. Under an excluded name the backup is asked what it
+        # holds (AD-36). before_rows becomes the rows the diff was computed
+        # against, so the >50% guard below counts deletions against the
+        # same tree.
+        try:
+            changes, before_rows = _plan_changes(
+                before_rows,
+                fresh_rows,
+                exclude_dirs=options.exclude_dirs,
+                no_exclude=args.no_exclude,
+                destination=destination,
+            )
+        except DestinationUnreadable as e:
+            logging.error(
+                "Refusing: cannot read the backup at %s (%s). irsync checks what "
+                "the backup holds there before it lists any change; nothing has "
+                "been written to it.",
+                display_name(e.path),
+                e.reason,
+            )
+            return EXIT_REFUSED
         logging.info(
             "Diff: %d dir moves, %d file moves, %d modified, %d created, %d deleted.",
             len(changes.dir_moves),
@@ -903,7 +993,12 @@
     # through less so the user can scroll through the deletion list. In --yes
     # mode, dump it once so the run is at least auditable in scrollback / logs.
     if changes is not None:
-        _show_preview(changes, interactive=not args.yes, unexcluded=unexcluded)
+        _show_preview(
+            changes,
+            interactive=not args.yes,
+            unexcluded=unexcluded,
+            checked=destination is not None,
+        )
     else:
         _emit_preview(
             _format_first_run_preview(
@@ -948,12 +1043,10 @@
         _cleanup_orphan_tempfiles(dest_root)

     # Apply moves on dest tree first (only if we have a local dest and a diff).
-    if changes is not None and dest_root is not None:
+    if changes is not None and destination is not None:
         try:
-            result = apply_moves(
-                dir_moves=changes.dir_moves,
-                file_moves=changes.file_moves,
-                dest_root=dest_root,
+            result = destination.replay(
+                dir_moves=changes.dir_moves, file_moves=changes.file_moves
             )
         except CrossDeviceMoveError as e:
             # NEW-H2 (5th-pass). Refuse upfront so we don't leave dest in
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `pixi run test tests/test_excluded_names.py tests/test_backup.py -q` → no failures.
Run: `pixi run test -q` → `494 passed, 1 skipped`.

- [ ] **Step 5: Gate and commit**

```bash
pixi run pre-commit run --all-files
pixi run test-cov
git add src/irsync/backup.py tests/test_excluded_names.py tests/test_backup.py
git commit -m "fix: ask the backup about excluded names under --no-exclude

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: A run with the excludes moves a file out when the backup holds it

**Goal:** A run with the default excludes asks the backup about a file that moved out of an excluded name and lists a file move when the backup holds it; nothing else under an excluded name is listed, and a run without such a file never looks at the backup.

**Files:**
- Modify: `src/irsync/backup.py` (the `irsync.baseline` import; `_plan_changes`)
- Modify: `tests/test_excluded_names.py` (one import, one helper, one class)

**Acceptance Criteria:**
- [ ] Spec case 3: in a run with the excludes, a file moved out of an excluded name whose copy the backup holds is a file move; the copy keeps its inode and nothing is left at the old path.
- [ ] A stale copy is not used: the file is created, and the stale copy stays where it is.
- [ ] A folder moved out is handled file by file: the folder is created, each file the backup holds is a file move, and whatever else the backup has in the old folder is untouched.
- [ ] No path under an excluded name is listed as modified, created or deleted.
- [ ] `LocalDestination.look` is not called when nothing changed, when the only change is inside an excluded folder, or when the change involves no file moved out.
- [ ] A run to a remote destination behaves as before.
- [ ] `pixi run test -q` ends with `501 passed, 1 skipped`.

**Verify:** `pixi run test tests/test_excluded_names.py -q` → `18 passed`; then `pixi run test -q` → `501 passed, 1 skipped`

**What each test is for** (all in `tests/test_excluded_names.py::TestRunWithTheExcludes`):

| Test | Red first? | Bug it would catch |
|---|---|---|
| `test_a_file_the_backup_holds_is_moved_out_with_nothing_sent` | red | The file was listed as created and sent in full, and the old copy stayed, so the backup held it twice (case 3). |
| `test_a_stale_copy_is_left_alone_and_the_file_is_sent` | guard | A rescue that only checked that something exists at the old path would rename a stale copy and list a move rsync has to re-send. |
| `test_a_folder_moved_out_is_handled_file_by_file` | red | Renaming the whole folder would carry `leftover.part` into the synced tree, where rsync deletes it without the preview having listed it. |
| `test_nothing_under_an_excluded_name_is_listed` | guard | Admitting the row of a file that was edited and then moved out would list its old path as deleted. |
| `test_the_backup_is_not_looked_at_without_a_file_moved_out` (3 cases) | guard | Asking on every run would wake the backup drive on a run that has nothing to do (AD-2). |

**Steps:**

- [ ] **Step 1: Write the failing tests**

In `tests/test_excluded_names.py`, add this import after the `irsync.backup` import:

```python
from irsync.destination import LocalDestination
```

Add this helper after `_make_stale`:

```python
def _listed(text: str) -> list[str]:
    """Return the preview's modified, created and deleted lines."""
    return [ln for ln in text.splitlines() if ln[:4] in ("  ~ ", "  + ", "  - ")]
```

And append this class to the end of the file:

```python
class TestRunWithTheExcludes:
    """An ordinary run can only take a file out of an excluded name."""

    def test_a_file_the_backup_holds_is_moved_out_with_nothing_sent(
        self, tmp_path, basic_options, caplog
    ):
        # Behavior (reproduced case 3): the backup's copy under the
        # excluded name is renamed to the file's new place.
        # Bug caught: the file was listed as created and sent in full,
        # and the old copy stayed, so the backup held it twice.
        # Expected values: the copy keeps its inode, which only a rename
        # does; nothing is left at the old path.
        src, dest = _tree(tmp_path, f"{DL}/ep.mkv", "finished/other.txt", "keep")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        copy_inode = inode_for(dest / DL / "ep.mkv")
        (src / DL / "ep.mkv").rename(src / "finished" / "ep.mkv")

        rc, diffs = _backup(src, dest, basic_options, caplog)

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 1 file moves, 0 modified, 0 created, 0 deleted."
        ]
        assert inode_for(dest / "finished" / "ep.mkv") == copy_inode
        assert not (dest / DL / "ep.mkv").exists()

    def test_a_stale_copy_is_left_alone_and_the_file_is_sent(
        self, tmp_path, basic_options, caplog
    ):
        # Behavior: a copy that is not the file the source has now is not
        # used; the file is created, and the run changes nothing under the
        # excluded name. Passes before this change too.
        # Bug caught: a rescue that only checked that something exists at
        # the old path would rename the stale copy and list a move rsync
        # then has to re-send.
        # Expected values: the stale copy keeps its place and content.
        src, dest = _tree(tmp_path, f"{DL}/ep.mkv", "finished/other.txt", "keep")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        _make_stale(dest / DL / "ep.mkv")
        (src / DL / "ep.mkv").rename(src / "finished" / "ep.mkv")

        rc, diffs = _backup(src, dest, basic_options, caplog)

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 0 file moves, 0 modified, 1 created, 0 deleted."
        ]
        assert (dest / "finished" / "ep.mkv").read_text() == f"{DL}/ep.mkv"
        assert (dest / DL / "ep.mkv").read_text() == "partial"

    def test_a_folder_moved_out_is_handled_file_by_file(
        self, tmp_path, basic_options, caplog
    ):
        # Behavior: the folder arrives as a new folder; each file the
        # backup holds is moved into it, the others are sent; whatever
        # else the backup has in the old folder stays where it is.
        # Bug caught: renaming the whole folder would carry leftover.part
        # into the synced tree, where rsync deletes it without the preview
        # having listed it.
        # Expected values: hand count. Created: finished/Pack, e2 (stale
        # on the backup) and e3 (missing there); e1 is the one move.
        pack = f"{DL}/Pack"
        src, dest = _tree(
            tmp_path, f"{pack}/e1", f"{pack}/e2", f"{pack}/e3", "finished/other.txt"
        )
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        e1_inode = inode_for(dest / pack / "e1")
        _make_stale(dest / pack / "e2")
        (dest / pack / "e3").unlink()
        _write(dest / pack / "leftover.part", "only on the backup")
        (src / pack).rename(src / "finished" / "Pack")

        rc, diffs = _backup(src, dest, basic_options, caplog)

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 1 file moves, 0 modified, 3 created, 0 deleted."
        ]
        moved = dest / "finished" / "Pack"
        assert inode_for(moved / "e1") == e1_inode
        assert (moved / "e2").read_text() == f"{pack}/e2"
        assert (moved / "e3").read_text() == f"{pack}/e3"
        assert sorted(p.name for p in moved.iterdir()) == ["e1", "e2", "e3"]
        assert (dest / pack / "leftover.part").read_text() == "only on the backup"
        assert (dest / pack / "e2").read_text() == "partial"

    def test_nothing_under_an_excluded_name_is_listed(
        self, tmp_path, basic_options, caplog, capsys
    ):
        # Behavior: a run with the excludes changes nothing under an
        # excluded name, so its lists name no path there. Passes before
        # this change too.
        # Bug caught: admitting the row of a file that was edited and then
        # moved out would list its old path as deleted.
        # Expected values: one creation (the edited file at its new
        # place); the deleted partial download is not the backup's
        # business in this run.
        src, dest = _tree(tmp_path, f"{DL}/a.part", f"{DL}/b.part", "docs/keep")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        _write(src / DL / "a.part", "finished downloading")
        (src / DL / "a.part").rename(src / "docs" / "a.mkv")
        (src / DL / "b.part").unlink()
        capsys.readouterr()

        rc, diffs = _backup(src, dest, basic_options, caplog)

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 0 file moves, 0 modified, 1 created, 0 deleted."
        ]
        assert _listed(capsys.readouterr().out) == ["  + docs/a.mkv"]

    @pytest.mark.parametrize(
        "change",
        ["nothing", "inside-the-excluded-folder", "elsewhere"],
    )
    def test_the_backup_is_not_looked_at_without_a_file_moved_out(
        self, tmp_path, basic_options, caplog, monkeypatch, change
    ):
        # Behavior (AD-2): the backup is asked only when a file under an
        # excluded name has turned up in the synced tree.
        # Bug caught: asking on every run would wake the backup drive on a
        # run that has nothing to do.
        # Expected values: no lookup in any of the three cases; the first
        # two also stop at the no-change short-circuit.
        src, dest = _tree(tmp_path, f"{DL}/part", "docs/keep")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        if change == "inside-the-excluded-folder":
            _write(src / DL / "part", "grown")
        elif change == "elsewhere":
            _write(src / "docs" / "new.txt", "new")
        looked: list[list[str]] = []
        monkeypatch.setattr(
            LocalDestination,
            "look",
            lambda self, paths: looked.append(list(paths)) or {},
        )

        rc, _diffs = _backup(src, dest, basic_options, caplog)

        assert rc == 0
        assert looked == []
        assert (
            "No source changes since last backup; backup drive untouched."
            in caplog.messages
        ) is (change != "elsewhere")
```

- [ ] **Step 2: Run them and see them fail**

Run: `pixi run test tests/test_excluded_names.py -q`
Expected: 2 failures, 16 passed.

- `test_a_file_the_backup_holds_is_moved_out_with_nothing_sent`: the `Diff:` line says `0 file moves, 0 modified, 1 created`.
- `test_a_folder_moved_out_is_handled_file_by_file`: the `Diff:` line says `0 file moves, 0 modified, 4 created`.

The other five new tests are guards and pass.

- [ ] **Step 3: Implement**

Change `src/irsync/backup.py`: for a local destination, `_plan_changes` now splits the baseline in every run, and a run with the excludes asks about the files that moved out of an excluded name.

```diff
--- a/src/irsync/backup.py
+++ b/src/irsync/backup.py
@@ -13,7 +13,7 @@
 from collections.abc import Iterator, Sequence
 from pathlib import Path

-from irsync.baseline import checked_rows, split_hints
+from irsync.baseline import checked_rows, holds, moved_out_candidates, split_hints
 from irsync.destination import Destination, DestinationUnreadable, LocalDestination
 from irsync.diff import Changes, compute_changes
 from irsync.display import display_name
@@ -449,10 +449,17 @@

     A baseline row under a default-excluded name says what the source had,
     not what the backup holds, since a run with the excludes never copies
-    those names. A ``--no-exclude`` run to a local destination syncs those
-    names, so it asks the destination about every such row and diffs
-    against what the backup holds (AD-36). Every other run diffs only what
-    rsync transfers, as before (AD-32).
+    those names. For a local destination such a row enters the diff only as
+    far as the destination confirms it (AD-36):
+
+    - a run with the excludes changes nothing under those names, so it asks
+      only about a file that has moved out of one, and keeps the row when
+      the backup holds that file, which makes it a file move;
+    - a ``--no-exclude`` run syncs those names, so it asks about every such
+      row and diffs against what the backup holds.
+
+    A remote destination cannot be examined: its runs diff only what rsync
+    transfers, as before (AD-32).

     Args:
         before_rows: The baseline's rows.
@@ -470,14 +477,19 @@
     """
     skipped = exclude_matcher([] if no_exclude else exclude_dirs)
     after = [r for r in fresh_rows if not skipped(r["path"])]
-    if no_exclude and destination is not None:
-        trusted, hints = split_hints(before_rows, exclude_matcher(exclude_dirs))
+    if destination is None:
+        before = [r for r in before_rows if not skipped(r["path"])]
+        return compute_changes(before, after), before
+    trusted, hints = split_hints(before_rows, exclude_matcher(exclude_dirs))
+    if no_exclude:
         # No hints, no lookup: with an empty exclude list a run that has
         # nothing to do must still leave the backup drive alone (AD-2).
         seen = destination.look([r["path"] for r in hints]) if hints else {}
         before = trusted + checked_rows(hints, seen)
     else:
-        before = [r for r in before_rows if not skipped(r["path"])]
+        candidates = moved_out_candidates(hints, trusted, after)
+        seen = destination.look([r["path"] for r in candidates]) if candidates else {}
+        before = trusted + [r for r in candidates if holds(r, seen[r["path"]])]
     return compute_changes(before, after), before


```

- [ ] **Step 4: Run the tests and see them pass**

Run: `pixi run test tests/test_excluded_names.py -q` → `18 passed`.
Run: `pixi run test -q` → `501 passed, 1 skipped`.

- [ ] **Step 5: Gate and commit**

```bash
pixi run pre-commit run --all-files
pixi run test-cov
git add src/irsync/backup.py tests/test_excluded_names.py
git commit -m "fix: move a file out of an excluded name when the backup holds it

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: A remote destination claims no moves

**Goal:** For a remote destination the lists show what rsync does: no moves, each moved entry deleted and created and counted by the >50% guard, with a note, a warning and a help text that say so.

**Files:**
- Modify: `src/irsync/backup.py` (new `_REMOTE_RENAME_COUNTS`, `_moves_text`; `_format_preview`; `_show_preview`; `_plan_changes`; `_run_backup_for_endpoints`)
- Modify: `src/irsync/cli.py` (the parser's description)
- Modify: `tests/conftest.py` (new `remote_host` fixture)
- Create: `tests/test_remote_destination.py`
- Modify: `tests/test_backup.py` (`TestFormatPreview`)
- Modify: `tests/test_cli.py` (one test appended)

**Acceptance Criteria:**
- [ ] For a remote destination the diff has no moves: a renamed folder and a moved file are listed entry by entry as deleted and created.
- [ ] When moves were found, the preview carries the note `=== Remote destination: <counts> cannot be replayed ===` with its two lines, a count of zero left out, placed after the `--no-exclude` note, and a warning is logged.
- [ ] The >50% guard counts those entries: a large rename is refused with exit 2, the refusal carries the added sentence, and `--allow-massive-delete` lets it through.
- [ ] Under `--no-exclude` the lists name no path under an excluded name and the note keeps its original text; rsync still syncs those names.
- [ ] `--help` says the replay needs a local destination.
- [ ] Runs to a local destination are unchanged: no remote note, the same refusal text.
- [ ] `pixi run test -q` ends with `509 passed, 1 skipped`.

**Verify:** `pixi run test tests/test_remote_destination.py tests/test_backup.py tests/test_cli.py -q` → no failures; then `pixi run test -q` → `509 passed, 1 skipped`

**What each test is for:**

| Test | Red first? | Bug it would catch |
|---|---|---|
| `tests/test_remote_destination.py::...::test_moves_are_listed_as_deleted_and_created` | red | The preview listed a directory move and a file move, nothing was renamed, and rsync deleted and re-sent everything that had moved. |
| `...::test_a_large_rename_is_refused_and_says_why` | red | The diff reported 0 deleted, so the >50% guard stayed silent while rsync deleted 7 of the backup's entries before sending them again. |
| `...::test_no_exclude_leaves_the_excluded_names_out_of_the_lists` | red | An edited partial download was listed as "Modified in place, will be re-transferred", though the remote backup had never received it. |
| `tests/test_backup.py::TestFormatPreview::test_unreplayed_moves_get_a_note_after_the_no_exclude_note` (3 cases) | red | "0 directory moves and 3 file moves" reads as if a folder were involved; a note placed first would push the `--no-exclude` warning about DELETING below it. |
| `tests/test_backup.py::TestFormatPreview::test_no_remote_note_when_every_move_can_be_replayed` | guard | A note on every run would say moves were turned into deletions when there were none. |
| `tests/test_cli.py::test_help_says_the_replay_needs_a_local_destination` | red | The help promised the replay for every destination. |

The three end-to-end tests use the new `remote_host` fixture: a stand-in `ssh`, first on `PATH`, that drops the options and the host name and runs the rest through `sh -c`. rsync treats the destination as remote and irsync sees a remote destination, while the "remote" rsync runs on this machine. The fixture skips when this rsync cannot be driven that way. **If those three tests are reported as skipped in Step 2 or Step 4, stop and report it:** the task cannot be verified on this machine.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Change `tests/conftest.py`:

```diff
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -3,6 +3,7 @@
 import argparse
 import os
 import random
+import subprocess
 from collections.abc import Sequence
 from pathlib import Path

@@ -191,3 +192,51 @@

 def inode_for(path: Path) -> int:
     return path.stat().st_ino
+
+
+_STAND_IN_SSH = """#!/bin/sh
+# Stand-in for ssh in tests: drop the options and the host name, then run
+# the remote command on this machine through a shell, as sshd would.
+while [ $# -gt 0 ]; do
+  case "$1" in
+    -p|-i|-l|-o) shift 2 ;;
+    -*) shift ;;
+    *) shift; break ;;
+  esac
+done
+exec sh -c "$*"
+"""
+
+
+@pytest.fixture
+def remote_host(tmp_path, monkeypatch):
+    """Make ``<host>:/path`` reach this machine, and return the host name.
+
+    Puts a stand-in ``ssh`` first on ``PATH``, so rsync treats the
+    destination as remote while the "remote" rsync runs locally. irsync sees
+    a remote destination (a string, never a ``Path``), which is the code
+    path under test. Skips when this rsync cannot be driven that way, rather
+    than failing for a reason that has nothing to do with irsync.
+    """
+    bindir = tmp_path / "stand-in-bin"
+    bindir.mkdir()
+    ssh = bindir / "ssh"
+    ssh.write_text(_STAND_IN_SSH)
+    ssh.chmod(0o755)
+    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
+    probe_src = tmp_path / "ssh-probe-src"
+    probe_src.mkdir()
+    (probe_src / "probe").write_text("probe")
+    probe_dest = tmp_path / "ssh-probe-dest"
+    result = subprocess.run(
+        ["rsync", "-a", "-e", "ssh", f"{probe_src}/", f"standin:{probe_dest}/"],
+        capture_output=True,
+        text=True,
+        check=False,
+    )
+    if result.returncode != 0 or not (probe_dest / "probe").is_file():
+        pytest.skip(
+            "this rsync cannot be driven through the stand-in ssh: "
+            f"{result.stderr.strip()}"
+        )
+    return "standin"
```

Create `tests/test_remote_destination.py`:

```python
"""End to end: a remote destination claims no moves (2026-11-25 design, section 3).

irsync can neither look at a remote backup nor rename on it, so a run must
list what rsync does there. The ``remote_host`` fixture makes
``<host>:/path`` reach this machine through a stand-in ``ssh``; irsync still
sees a remote destination, which is the code path under test.
"""

import logging
from pathlib import Path

from irsync.backup import EXIT_REFUSED, run_backup
from irsync.snapshot import LOCKFILE_NAME, SNAPSHOT_FILENAME

from .conftest import cli_args as _args
from .conftest import tree_signature

_INTERNAL_FILES = {SNAPSHOT_FILENAME, LOCKFILE_NAME}


def _tree(tmp_path: Path, *files: str) -> tuple[Path, Path]:
    """Create src holding ``files`` (each file's content is its path); dest does not exist yet."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    for rel in files:
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(rel)
    return src, dest


def _backup(src, dest, host, options, caplog, **flags):
    """Back up to ``host:dest``; return the exit code and the "Diff:" log lines."""
    caplog.clear()
    with caplog.at_level(logging.INFO):
        rc = run_backup(
            source_arg=str(src),
            destination_arg=f"{host}:{dest}",
            options=options,
            args=_args(**flags),
        )
    return rc, [m for m in caplog.messages if m.startswith("Diff:")]


def _user_files(root: Path) -> dict[str, tuple[int, bytes]]:
    """Return the tree's files, irsync's own left out."""
    return {
        path: sig
        for path, sig in tree_signature(root).items()
        if path not in _INTERNAL_FILES
    }


class TestRemoteDestinationClaimsNoMoves:
    def test_moves_are_listed_as_deleted_and_created(
        self, tmp_path, basic_options, caplog, capsys, remote_host
    ):
        # Behavior: a renamed folder and a moved file are listed entry by
        # entry as deleted and created, with a note and a warning saying
        # why, because that is what rsync does on a remote backup.
        # Bug caught: the preview listed "Directory moves (1)" and "File
        # moves (1)", nothing was renamed, and rsync deleted and re-sent
        # everything that had moved.
        # Expected values: hand count. Deleted: big and its 3 files, and
        # a/movie.mkv (5). Created: big2 and its 3 files, and b/movie.mkv
        # (5). 5 of the 17 recorded entries is under the >50% guard.
        keep = [f"keep{i}" for i in range(8)]
        src, dest = _tree(
            tmp_path, "big/f1", "big/f2", "big/f3", "a/movie.mkv", "b/other", *keep
        )
        assert _backup(src, dest, remote_host, basic_options, caplog)[0] == 0
        assert _user_files(dest) == _user_files(src)
        (src / "big").rename(src / "big2")
        (src / "a" / "movie.mkv").rename(src / "b" / "movie.mkv")
        capsys.readouterr()

        rc, diffs = _backup(src, dest, remote_host, basic_options, caplog)

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 0 file moves, 0 modified, 5 created, 5 deleted."
        ]
        lines = capsys.readouterr().out.splitlines()
        start = lines.index(
            "=== Remote destination: 1 directory move and 1 file move "
            "cannot be replayed ==="
        )
        assert lines[start + 1 : start + 4] == [
            "They are listed below as deleted and created: rsync deletes them on the",
            "backup and sends them again.",
            "=== Directory moves (0) ===",
        ]
        for line in ("  - big", "  - big/f1", "  - a/movie.mkv", "  + big2/f1"):
            assert line in lines
        assert (
            "The destination is remote, so 1 directory move and 1 file move "
            "cannot be replayed there: rsync deletes them on the backup and "
            "sends them again."
        ) in caplog.messages
        assert _user_files(dest) == _user_files(src)

    def test_a_large_rename_is_refused_and_says_why(
        self, tmp_path, basic_options, caplog, remote_host
    ):
        # Behavior: a rename that makes rsync delete most of a remote
        # backup trips the >50% guard, and the refusal explains that a
        # rename counts there; --allow-massive-delete lets it through.
        # Bug caught: the diff reported 0 deleted, so the guard stayed
        # silent while rsync deleted 7 of the backup's entries before
        # sending them again.
        # Expected values: 7 of 10 recorded rows (the root, big and its 6
        # files, k1, k2) is 70%.
        files = [f"big/f{i}" for i in range(1, 7)]
        src, dest = _tree(tmp_path, *files, "k1", "k2")
        assert _backup(src, dest, remote_host, basic_options, caplog)[0] == 0
        before = _user_files(dest)
        (src / "big").rename(src / "big2")

        rc, _diffs = _backup(src, dest, remote_host, basic_options, caplog)

        assert rc == EXIT_REFUSED
        refusals = [m for m in caplog.messages if m.startswith("Refusing:")]
        assert len(refusals) == 1
        assert "would delete 7 of 10 previously-recorded entries (70%)" in refusals[0]
        assert (
            "args were reversed. On a remote destination a moved or renamed "
            "entry counts, because rsync deletes it there and sends it again. "
            "Pass --allow-massive-delete to override."
        ) in refusals[0]
        assert _user_files(dest) == before

        rc, _diffs = _backup(
            src, dest, remote_host, basic_options, caplog, allow_massive_delete=True
        )

        assert rc == 0
        assert _user_files(dest) == _user_files(src)

    def test_no_exclude_leaves_the_excluded_names_out_of_the_lists(
        self, tmp_path, basic_options, caplog, capsys, remote_host
    ):
        # Behavior: irsync cannot look at a remote backup, so under
        # --no-exclude its lists make no claim about the excluded names
        # and the note keeps saying so; rsync still syncs them.
        # Bug caught: the edited partial download was listed as "Modified
        # in place, will be re-transferred", though the remote backup had
        # never received it.
        # Expected values: hand count. Created: docs/new.txt only.
        src, dest = _tree(tmp_path, "unfinished_downloads/part", "docs/keep")
        assert _backup(src, dest, remote_host, basic_options, caplog)[0] == 0
        assert not (dest / "unfinished_downloads").exists()
        (src / "unfinished_downloads" / "part").write_text("grown since")
        (src / "docs" / "new.txt").write_text("new")
        capsys.readouterr()

        rc, diffs = _backup(
            src, dest, remote_host, basic_options, caplog, no_exclude=True
        )

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 0 file moves, 0 modified, 1 created, 0 deleted."
        ]
        lines = capsys.readouterr().out.splitlines()
        start = lines.index(
            "=== --no-exclude: rsync also syncs .Trash-*, .cache, "
            "unfinished_downloads ==="
        )
        assert lines[start + 1] == (
            "On the backup, rsync makes these match the source, copying and DELETING"
        )
        listed = [ln for ln in lines if ln[:4] in ("  ~ ", "  + ", "  - ")]
        assert listed == ["  + docs/new.txt"]
        assert (dest / "unfinished_downloads" / "part").read_text() == "grown since"
```

In `tests/test_backup.py`, add these two tests to `class TestFormatPreview`, after `test_checked_lists_get_the_note_that_names_what_they_cannot_show`:

```python
    @pytest.mark.parametrize(
        ("unreplayed", "header"),
        [
            (
                (1, 1),
                "=== Remote destination: 1 directory move and 1 file move "
                "cannot be replayed ===",
            ),
            ((0, 3), "=== Remote destination: 3 file moves cannot be replayed ==="),
            (
                (2, 0),
                "=== Remote destination: 2 directory moves cannot be replayed ===",
            ),
        ],
        ids=["both", "files-only", "folders-only"],
    )
    def test_unreplayed_moves_get_a_note_after_the_no_exclude_note(
        self, unreplayed, header
    ):
        # Behavior: when a remote destination cannot replay moves, a note
        # says how many and that they are listed as deleted and created.
        # It follows the --no-exclude note, and a count of zero is left
        # out.
        # Bug caught: "0 directory moves and 3 file moves" reads as if a
        # folder were involved; a note placed first would push the
        # --no-exclude warning about DELETING below it.
        # Expected values: the text in the 2026-11-25 design, section 3.
        lines = _format_preview(
            Changes(), unexcluded=[".cache"], unreplayed=unreplayed
        ).splitlines()
        assert lines[0] == "=== --no-exclude: rsync also syncs .cache ==="
        assert lines[4:8] == [
            header,
            "They are listed below as deleted and created: rsync deletes them on the",
            "backup and sends them again.",
            "=== Directory moves (0) ===",
        ]

    def test_no_remote_note_when_every_move_can_be_replayed(self):
        # Bug caught: a note shown on every run to a remote destination
        # would say moves were turned into deletions when there were none.
        text = _format_preview(Changes(file_moves=[("a", "b")]))
        assert "Remote destination" not in text
```

Append this test to the end of `tests/test_cli.py`:

```python
def test_help_says_the_replay_needs_a_local_destination():
    # Behavior: the description says where moves are replayed and what
    # happens to a moved file where they are not.
    # Bug caught: the help promised the replay for every destination, but
    # a remote one never gets it: a renamed file is deleted there and sent
    # again (2026-11-25 design, case 5).
    from irsync.cli import _build_parser

    description = " ".join(str(_build_parser().description).split())
    assert (
        "The replay needs a local destination: a remote destination "
        "(host:/path) cannot be renamed on yet, so a moved file is deleted "
        "there and sent again."
    ) in description
```

- [ ] **Step 2: Run them and see them fail**

Run: `pixi run test tests/test_remote_destination.py tests/test_backup.py tests/test_cli.py -q`
Expected: 7 failures, no errors, nothing skipped in `tests/test_remote_destination.py`.

- `test_moves_are_listed_as_deleted_and_created`: the `Diff:` line says `1 dir moves, 1 file moves`.
- `test_a_large_rename_is_refused_and_says_why`: the exit code is 0.
- `test_no_exclude_leaves_the_excluded_names_out_of_the_lists`: the `Diff:` line says `1 modified`.
- `test_unreplayed_moves_get_a_note_after_the_no_exclude_note` (3 cases): `TypeError: _format_preview() got an unexpected keyword argument 'unreplayed'`.
- `test_help_says_the_replay_needs_a_local_destination`: the sentence is not in the description.

`test_no_remote_note_when_every_move_can_be_replayed` is a guard and passes.

- [ ] **Step 3: Implement**

Change `src/irsync/backup.py`: for a remote destination `_plan_changes` drops the excluded names from both sides and, when the diff holds moves, computes it again without move detection; it returns how many moves could not be replayed, which feeds the note, the warning and the sentence added to the >50% refusal.

```diff
--- a/src/irsync/backup.py
+++ b/src/irsync/backup.py
@@ -161,8 +161,38 @@
     _atomic_write_snapshot(rows, src_root, src_root / SNAPSHOT_FILENAME)


+_REMOTE_RENAME_COUNTS = (
+    " On a remote destination a moved or renamed entry counts, because rsync "
+    "deletes it there and sends it again."
+)
+"""Added to the >50% refusal when a remote destination turned moves into deletions."""
+
+
+def _moves_text(dir_moves: int, file_moves: int) -> str:
+    """Name a number of directory and file moves, leaving out a count of zero.
+
+    Args:
+        dir_moves: How many directory moves.
+        file_moves: How many file moves.
+
+    Returns:
+        For example ``"1 directory move and 3 file moves"`` or
+        ``"3 file moves"``; empty when both counts are zero.
+    """
+    parts: list[str] = []
+    if dir_moves:
+        parts.append(_count(dir_moves, "directory move", "directory moves"))
+    if file_moves:
+        parts.append(_count(file_moves, "file move", "file moves"))
+    return " and ".join(parts)
+
+
 def _format_preview(
-    changes: Changes, *, unexcluded: Sequence[str] = (), checked: bool = False
+    changes: Changes,
+    *,
+    unexcluded: Sequence[str] = (),
+    checked: bool = False,
+    unreplayed: tuple[int, int] = (0, 0),
 ) -> str:
     """Build the full pre-confirmation preview text — every move, deletion, creation.

@@ -181,6 +211,9 @@
             backup (a local destination). The note then names the one thing
             the lists cannot show; otherwise it says they show only what
             changed on the source.
+        unreplayed: How many directory and file moves a remote destination
+            cannot replay. When either is non-zero, a second note says they
+            are listed as deleted and created.

     Returns:
         The preview text, ending in a newline.
@@ -212,6 +245,14 @@
             lines.append(
                 "the last backup; --dry-run shows every file rsync will copy or delete."
             )
+    if any(unreplayed):
+        lines.append(
+            f"=== Remote destination: {_moves_text(*unreplayed)} cannot be replayed ==="
+        )
+        lines.append(
+            "They are listed below as deleted and created: rsync deletes them on the"
+        )
+        lines.append("backup and sends them again.")
     lines.append(f"=== Directory moves ({len(changes.dir_moves)}) ===")
     for o, n in changes.dir_moves:
         lines.append(f"  {display_name(o)}  ->  {display_name(n)}")
@@ -429,10 +470,13 @@
     interactive: bool,
     unexcluded: Sequence[str] = (),
     checked: bool = False,
+    unreplayed: tuple[int, int] = (0, 0),
 ) -> None:
     """Render the change preview, paging through ``less`` if interactive."""
     _emit_preview(
-        _format_preview(changes, unexcluded=unexcluded, checked=checked),
+        _format_preview(
+            changes, unexcluded=unexcluded, checked=checked, unreplayed=unreplayed
+        ),
         interactive=interactive,
     )

@@ -444,22 +488,22 @@
     exclude_dirs: Sequence[str],
     no_exclude: bool,
     destination: Destination | None,
-) -> tuple[Changes, list[Row]]:
+) -> tuple[Changes, list[Row], tuple[int, int]]:
     """Diff the baseline against the fresh snapshot, asking the backup where needed.

     A baseline row under a default-excluded name says what the source had,
     not what the backup holds, since a run with the excludes never copies
-    those names. For a local destination such a row enters the diff only as
-    far as the destination confirms it (AD-36):
+    those names. Such a row enters the diff only as far as the destination
+    confirms it (AD-36):

     - a run with the excludes changes nothing under those names, so it asks
       only about a file that has moved out of one, and keeps the row when
       the backup holds that file, which makes it a file move;
     - a ``--no-exclude`` run syncs those names, so it asks about every such
-      row and diffs against what the backup holds.
-
-    A remote destination cannot be examined: its runs diff only what rsync
-    transfers, as before (AD-32).
+      row and diffs against what the backup holds;
+    - a remote destination can be neither examined nor renamed on (AD-37):
+      the lists leave those names to rsync and claim no moves, so an entry
+      whose path changed is listed as deleted and created.

     Args:
         before_rows: The baseline's rows.
@@ -469,18 +513,25 @@
         destination: Where to look, or None for a remote destination.

     Returns:
-        ``(changes, before)``: the diff, and the baseline rows it was
-        computed against, which the >50% guard counts deletions against.
+        ``(changes, before, unreplayed)``: the diff; the baseline rows it
+        was computed against, which the >50% guard counts deletions
+        against; and how many directory and file moves a remote destination
+        cannot replay (``(0, 0)`` for a local one).

     Raises:
         DestinationUnreadable: If the destination cannot be examined.
     """
     skipped = exclude_matcher([] if no_exclude else exclude_dirs)
+    unvouched = exclude_matcher(exclude_dirs)
+    trusted, hints = split_hints(before_rows, unvouched)
     after = [r for r in fresh_rows if not skipped(r["path"])]
     if destination is None:
-        before = [r for r in before_rows if not skipped(r["path"])]
-        return compute_changes(before, after), before
-    trusted, hints = split_hints(before_rows, exclude_matcher(exclude_dirs))
+        after = [r for r in after if not unvouched(r["path"])]
+        changes = compute_changes(trusted, after)
+        unreplayed = (len(changes.dir_moves), len(changes.file_moves))
+        if any(unreplayed):
+            changes = compute_changes(trusted, after, detect_moves=False)
+        return changes, trusted, unreplayed
     if no_exclude:
         # No hints, no lookup: with an empty exclude list a run that has
         # nothing to do must still leave the backup drive alone (AD-2).
@@ -490,7 +541,7 @@
         candidates = moved_out_candidates(hints, trusted, after)
         seen = destination.look([r["path"] for r in candidates]) if candidates else {}
         before = trusted + [r for r in candidates if holds(r, seen[r["path"]])]
-    return compute_changes(before, after), before
+    return compute_changes(before, after), before, (0, 0)


 def _confirm_or_abort(args: argparse.Namespace) -> bool:
@@ -874,6 +925,7 @@
     destination: Destination | None = (
         LocalDestination(dest_root) if dest_root is not None else None
     )
+    unreplayed = (0, 0)
     if have_before:
         # Diff only what rsync transfers. A move into an excluded directory,
         # such as a file sent to the desktop trash, is then a deletion from
@@ -883,7 +935,7 @@
         # against, so the >50% guard below counts deletions against the
         # same tree.
         try:
-            changes, before_rows = _plan_changes(
+            changes, before_rows, unreplayed = _plan_changes(
                 before_rows,
                 fresh_rows,
                 exclude_dirs=options.exclude_dirs,
@@ -899,6 +951,12 @@
                 e.reason,
             )
             return EXIT_REFUSED
+        if any(unreplayed):
+            logging.warning(
+                "The destination is remote, so %s cannot be replayed there: "
+                "rsync deletes them on the backup and sends them again.",
+                _moves_text(*unreplayed),
+            )
         logging.info(
             "Diff: %d dir moves, %d file moves, %d modified, %d created, %d deleted.",
             len(changes.dir_moves),
@@ -930,10 +988,11 @@
                     "Refusing: diff would delete %d of %d previously-recorded "
                     "entries (%.0f%%). This usually means the source tree has "
                     "been swapped, the snapshot is from a different tree, or "
-                    "args were reversed. Pass --allow-massive-delete to override.",
+                    "args were reversed.%s Pass --allow-massive-delete to override.",
                     len(changes.deleted),
                     len(before_rows),
                     ratio * 100,
+                    _REMOTE_RENAME_COUNTS if any(unreplayed) else "",
                 )
                 return EXIT_REFUSED

@@ -1010,6 +1069,7 @@
             interactive=not args.yes,
             unexcluded=unexcluded,
             checked=destination is not None,
+            unreplayed=unreplayed,
         )
     else:
         _emit_preview(
```

Change `src/irsync/cli.py`:

```diff
--- a/src/irsync/cli.py
+++ b/src/irsync/cli.py
@@ -25,8 +25,10 @@
             "rsync. Before each backup, irsync compares an inode snapshot of the source tree "
             "against the previous snapshot to detect moves/renames; those moves are replayed "
             "atomically on the backup tree before rsync runs, so renamed multi-GB files are "
-            "not retransferred. If nothing changed since the last backup, the backup drive is "
-            "never accessed."
+            "not retransferred. The replay needs a local destination: a remote destination "
+            "(host:/path) cannot be renamed on yet, so a moved file is deleted there and sent "
+            "again. If nothing changed since the last backup, the backup drive is never "
+            "accessed."
         ),
     )
     parser.add_argument(
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `pixi run test tests/test_remote_destination.py tests/test_backup.py tests/test_cli.py -q` → no failures, nothing skipped in `tests/test_remote_destination.py`.
Run: `pixi run test -q` → `509 passed, 1 skipped`.

- [ ] **Step 5: Gate and commit**

```bash
pixi run pre-commit run --all-files
pixi run test-cov
git add src/irsync/backup.py src/irsync/cli.py tests/conftest.py tests/test_remote_destination.py tests/test_backup.py tests/test_cli.py
git commit -m "fix: list a move to a remote destination as what rsync does with it

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Record the decisions in the docs

**Goal:** The architecture doc, README, `PROGRESS.md` and the spec describe what the code now does: AD-36, AD-37 and AD-38, the decisions they amend, the module and test tables, and the test total.

**Files:**
- Modify: `docs/design/irsync-architecture.md`
- Modify: `README.md`
- Modify: `PROGRESS.md`
- Modify: `docs/superpowers/specs/2026-11-25-excluded-names-backup-truth-design.md` (the Status line)

**Acceptance Criteria:**
- [ ] `docs/design/irsync-architecture.md` has sections 36, 37 and 38 with the text in this task, the amended sentences in AD-2, AD-6, AD-9, AD-32 and AD-35, the algorithm section, both module rows, the four test-file rows, and a test total of 510.
- [ ] `README.md` says the replay needs a local backup, has the two new Safety bullets, and lists `destination.py` and `baseline.py`.
- [ ] `PROGRESS.md` lists AD-36, AD-37 and AD-38, points Current work at the branch and this plan, and has the two new gotchas.
- [ ] The spec's Status line says it is implemented on branch `ask-the-backup`.
- [ ] No source or test file changes; `pixi run test -q` still ends with `509 passed, 1 skipped`.

**Verify:** `rg -n '^### 3[678]\.' docs/design/irsync-architecture.md` → three headings; `rg -c 'AD-3[678]' PROGRESS.md` → at least 3; `rg -n 'destination.py|baseline.py' README.md` → both listed

No test changes in this task. Wrap the prose you add at the width its file already uses. Where a step says "append", add the text to the end of the named paragraph or table cell and leave the rest as it is.

**Steps:**

- [ ] **Step 1: `docs/design/irsync-architecture.md`, the three new decisions**

Insert these three sections after section 35 and before `## Appendix A — bug catalog`:

```markdown
### 36. Under an excluded name, the backup is asked

A snapshot records every row of the source, but a run with the default
excludes never copies the excluded names, so a baseline row under one of them
says what the source had, not what the backup holds. Taking such a row as a
fact went wrong in both directions (reproduced 2026-11-25). After runs with
the excludes, `--no-exclude` previewed a file moved out of
`unfinished_downloads` as a move though the backup had no copy, and listed
deletions the backup never held, counting them toward the >50% guard (AD-6),
which refused a run that deleted nothing. After a `--no-exclude` run, a run
with the excludes copied such a file in full though the backup held an
identical copy at the old path, which then stayed.

Such a row is now a hint (`baseline.split_hints`), and before the diff the
local destination is asked what it holds there (`Destination.look`, an `lstat`,
never a read of file contents; AD-38):

- A `--no-exclude` run asks about every hint (`baseline.checked_rows`). Nothing
  there: the hint is dropped, so the path is created if the source still has
  it. A matching entry: the hint is kept and can become a move. Anything else:
  the hint is kept with the destination's type, size and mtime, so the path is
  modified, or deleted and created.
- A run with the excludes changes nothing under those names, so it asks only
  about a file that has moved out of one (`baseline.moved_out_candidates`: a
  regular file with one path in the baseline and one in the synced tree, the
  same file by `diff.same_file`), and keeps the hint only when the backup holds
  it, which makes it a file move. A folder moved out is handled file by file:
  renamed whole, it would carry along whatever else the backup has in it, and
  rsync would delete that without the preview having listed it.

"Holds", for a regular file, is rsync's own quick check: the same size, and the
same mtime to the whole second (`baseline.holds`; checked on rsync 3.4.1, which
leaves alone a copy whose mtime differs only in nanoseconds and re-sends one a
second apart). irsync then never lists a move that rsync follows with a
transfer, and never refuses one rsync would have left alone. The identity check
between baseline and source (AD-3, AD-13) is untouched: the lookup is a further
condition. `compute_changes` is unchanged; its "before" side now describes the
backup. A lookup happens only on a run that reaches the backup anyway: a
`--no-exclude` run (AD-35), or a run in which a file has appeared in the synced
tree (AD-2). A lookup that fails for any reason but absence refuses the run
with exit 2 before anything is written (`DestinationUnreadable`). The snapshot
format is unchanged, and a row elsewhere stays trusted: it is what the last
successful run synced.

Not covered: a file renamed inside the excluded folder after the backup got it
(one unneeded copy), and what the backup holds under those names that no
baseline recorded (rsync deletes it under `--no-exclude`; the preview's note
says so, AD-35). Rejected: diffing with the default excludes on both sides
whatever the flags (it copies what the backup already holds), recording the
exclude list in the snapshot header (lost at the next run with the excludes),
and carrying rows forward in the snapshot (a format change that cannot see
what irsync did not record itself). Design:
`docs/superpowers/specs/2026-11-25-excluded-names-backup-truth-design.md`.

### 37. A remote destination claims no moves

`apply_moves` renames with `os.rename` (AD-9), so a remote destination
(`host:/path`) never got the replay, from 0.1.0 on: the preview listed moves,
nothing logged the skip, and rsync's `--delete-before` deleted everything that
had moved and sent it again, unseen by the >50% guard. Reproduced 2026-11-25
with a stand-in ssh: for a renamed folder of 12 files and one moved file, 14
deletions and 6.8 MB sent, and a transfer cut off part-way left the backup with
neither copy of what was not yet sent.

Until a helper run over ssh exists (the next design), a run to a remote
destination says what happens. The diff is computed without move detection
(`compute_changes(detect_moves=False)`), so an entry whose path changed is
listed as deleted and created, entry by entry. When moves were found, a note
opens the preview and a warning is logged. The >50% guard counts those entries,
so a large rename is refused until `--allow-massive-delete`, and the refusal
says that a rename counts there. The destination cannot be examined either, so
AD-36 does not apply: a `--no-exclude` run leaves the excluded names out of the
lists on both sides and keeps AD-35's original note. What rsync transfers is
unchanged.

### 38. One interface for the destination

`destination.Destination` is what irsync may ask of a backup: `look(paths)`,
which reports per path what is there (`Seen`: type, size, mtime) or nothing,
and `replay(dir_moves, file_moves)`. `LocalDestination` implements it with
`lstat` and `apply_moves`; the orchestrator holds `None` for a remote
destination (AD-37), and a helper run over ssh can become the second
implementation without changing the callers. `look` takes every path in one
call, so that such an implementation needs one round trip. It never follows a
symlinked directory below the root, the rule the replay has (AD-9): a path
behind one is reported as absent. Each directory on the way is examined once,
so a folder the backup lacks costs one `lstat` whatever lies below it, and
asking about no path touches nothing.
```

- [ ] **Step 2: `docs/design/irsync-architecture.md`, the decisions these amend**

Append one paragraph or sentence to each section named here.

To the end of section 2 ("Source-side snapshot is the source of truth ..."):

```markdown
Since AD-36 the backup's own files can be examined before the preview (an
`lstat` of paths under the default-excluded names), but only on a run that
reaches the backup anyway.
```

To the end of section 6 ("Sanity-threshold for catastrophic diffs"):

```markdown
`before_rows` is what the diff was computed against: the rows rsync syncs
(AD-32), with rows under a default-excluded name only as far as the backup
holds them (AD-36). On a remote destination a moved or renamed entry counts as
deleted, because rsync deletes it there (AD-37).
```

To the end of section 9 ("apply_moves is in-process, not a generated script"):

```markdown
The orchestrator reaches it through `LocalDestination.replay` (AD-38). A remote
destination gets no replay, and its run says so (AD-37).
```

To the end of section 32 ("The diff covers only what rsync transfers"), as a new paragraph:

```markdown
Amended 2026-11-25 (AD-36): a baseline row under a default-excluded name is no
longer dropped or trusted wholesale. The local backup is asked what it holds
there, so a file moved out of an excluded name is a creation only when the
backup lacks it and a move when the backup holds it, and under `--no-exclude`
the diff is taken against what the backup holds rather than against "nothing
is dropped".
```

To the end of section 35, as a new paragraph:

```markdown
Amended 2026-11-25 (AD-36): for a local destination the lists of a
`--no-exclude` run now cover the excluded names, checked against the backup,
so on an unchanged source whose backup lacks them they are listed as created
and the "No source changes ... but --no-exclude" line is logged only when the
backup already holds every recorded row. The note there keeps its first line
and now names the one thing the lists cannot show: a file under those names
that only the backup has and no earlier run recorded, which rsync deletes. The
original note stays for a remote destination (AD-37).
```

- [ ] **Step 3: `docs/design/irsync-architecture.md`, the algorithm section and the tables**

In "Algorithm / data flow", append to step 2 ("Diff against the baseline."):

```markdown
A baseline row under a default-excluded name is a hint: the local backup is
asked what it holds there before the diff (AD-36). A remote destination cannot
be asked, and its diff reports no moves (AD-37).
```

Append to step 3 ("Replay detected moves on the dest."):

```markdown
The orchestrator reaches it through `LocalDestination.replay` (AD-38); a remote
destination gets no replay.
```

Append to the paragraph that starts "**No-change runs never touch the backup drive.**":

```markdown
Looking at the backup (AD-36) happens only on a run that reaches it anyway.
```

In the `src/irsync/` table, add these two rows after the `replay.py` row:

```markdown
| `destination.py` | What irsync may ask of a backup (AD-38): the `Destination` protocol (`look(paths)`, `replay(dir_moves, file_moves)`); `LocalDestination` (`look` by `lstat` under the resolved root, never following a symlinked directory below it, one `lstat` per directory on the way, no access for no paths; `replay` through `apply_moves`); `Seen` (type, size, mtime of one entry); `DestinationUnreadable` (a lookup failed for a reason other than absence) |
| `baseline.py` | What a baseline may say about the backup under the default-excluded names (AD-36), with no I/O: `split_hints`, `holds` (rsync's quick check: size and whole-second mtime), `checked_rows` (hints replaced by what the destination holds), `moved_out_candidates` (the hints a run with the excludes asks about) |
```

Append to the `diff.py` row's cell:

```markdown
`same_file` is the one identity check (AD-3, AD-13), shared with `baseline.py`; `compute_changes(detect_moves=False)` reports a path change as deleted and created (AD-37).
```

Append to the `backup.py` row's cell:

```markdown
`_plan_changes` builds the diff's inputs: it asks the destination about baseline rows under the default-excluded names (AD-36) and, for a remote destination, turns moves into deletions and creations (AD-37).
```

In the `tests/` table, append to the `conftest.py` row's cell:

```markdown
`remote_host` puts a stand-in `ssh` first on `PATH` so that `host:/path` reaches this machine, and skips where rsync cannot be driven that way.
```

And add these four rows after the `test_replay.py` row:

```markdown
| `test_destination.py` | `LocalDestination` on real files: types, absence, a missing root, a symlinked folder not followed, a symlinked root followed, an unreadable folder, no access for no paths, one lookup for a missing folder, replay |
| `test_baseline.py` | `split_hints`, `holds` (both edges of the whole second), the 2026-11-25 design's section 1 table through `compute_changes`, `moved_out_candidates`, and the invariant that a run with the excludes lists nothing under an excluded name |
| `test_excluded_names.py` | end to end with the real rsync and a local destination (AD-36): the reproduced cases 1 to 4, folders moved out, the whole-second rule against rsync itself, a backup that cannot be read, the note, and no lookup without a file moved out |
| `test_remote_destination.py` | end to end to a remote destination through `remote_host` (AD-37): moves listed as deleted and created, the note and the warning, the >50% guard and its added sentence, `--no-exclude` leaving the excluded names out of the lists |
```

Replace the test-total paragraph under the `tests/` table. It starts "Total: 432 tests as of 2026-11-23 (425 on 2026-11-22, ..." and ends "... reports either 432 passed or 431 passed, 1 skipped." Keep its list of earlier totals and make it read, with today's date for `<today>`:

```markdown
Total: 510 tests as of <today> (432 on 2026-11-23, 425 on 2026-11-22, 381 on
2026-10-19, 337 at the 2026-10-18 hygiene pass, 168 at the pass-10 migration).
One of them, the kernel inode-reuse btime test, skips where btime collapses to
mtime (e.g. tmpfs), so a run reports either 510 passed or 509 passed, 1
skipped.
```

- [ ] **Step 4: `README.md`**

Replace the opening paragraph (it starts "Rename-aware Python wrapper around `rsync`.") with:

```markdown
Rename-aware Python wrapper around `rsync`. When files are moved or renamed on
the source drive, the original `rsync` deletes the old path on the backup drive
and re-transfers the (often huge) file from scratch. `irsync` avoids that by
keeping an inode snapshot at the root of the source tree, comparing it against
a fresh snapshot on each run, and replaying just the moves/renames atomically
on the backup tree before invoking `rsync` for the actual content delta. The
replay needs a local backup: on a remote destination (`host:/path`) a moved
file is deleted and sent again, and the preview says so. When no source
changes are detected at all, the backup drive is never accessed.
```

In "## Safety", add these two bullets after the bullet that starts "**The snapshot is corrupt.**" and before the one that starts "**`--dry-run` changes nothing.**":

```markdown
- **The backup has to be looked at, and cannot be read.** For the names it
  normally skips (`.Trash-*`, `.cache`, `unfinished_downloads`), irsync does
  not assume the backup holds what the source had. Before listing changes it
  looks at the backup there (file metadata only, never contents): a file is
  listed as a move only when the backup holds it with the same size and
  modification time, and is otherwise listed as created and copied. If that
  look fails for any reason other than the file not being there (permission
  denied, an I/O error), the run refuses with exit code 2 before writing
  anything.
- **A remote destination gets no renames.** Moves are replayed only on a
  local destination. With `host:/path` as the destination, anything moved or
  renamed is listed as deleted and created, because rsync deletes it there
  and sends it again, and a note in the preview says so. Those entries count
  toward the more-than-half check above, so a large rename to a remote
  destination is refused until you pass `--allow-massive-delete`.
```

In "## Project structure", add these two lines after the `replay.py` line:

```
  destination.py    # Destination, LocalDestination — look at the backup, rename on it
  baseline.py       # what a baseline may claim about the backup under excluded names
```

- [ ] **Step 5: `PROGRESS.md` and the spec**

In `PROGRESS.md`, "## Cross-cutting decisions", add after the AD-35 line:

```markdown
- **AD-36** — Under a default-excluded name a baseline row is a hint: before the diff the local backup is asked what it holds (an `lstat`, rsync's own size-and-whole-second-mtime test), about every such row under `--no-exclude` and only about a file moved out of one in a run with the excludes; a lookup that fails refuses the run.
- **AD-37** — A remote destination claims no moves: its diff runs without move detection, so a moved entry is listed as deleted and created and counts toward the >50% guard, with a note, a warning, and a sentence in the refusal; replay over ssh is the next design.
- **AD-38** — One `Destination` interface (`look(paths)`, `replay(moves)`) with a local implementation; `look` takes every path at once, never follows a symlinked directory below the root, and touches nothing when asked nothing.
```

In "## Current work": relabel the existing `- **Before that (2026-11-22/23):**` bullet as `- **Earlier (2026-11-22/23):**`, relabel the existing `- **Status:**` bullet as `- **Before that (2026-11-23):**`, and put this new bullet first:

```markdown
- **Status:** "Ask the backup" (design: [`docs/superpowers/specs/2026-11-25-excluded-names-backup-truth-design.md`](docs/superpowers/specs/2026-11-25-excluded-names-backup-truth-design.md); plan and task tracker: [`docs/superpowers/plans/2026-11-25-ask-the-backup.md`](docs/superpowers/plans/2026-11-25-ask-the-backup.md) and its `.tasks.json`) is implemented on branch `ask-the-backup`, not merged and not released. Under a default-excluded name the local backup is asked what it holds before the diff, so a file is listed as a move only when the backup holds it and copied only when it does not (AD-36); a remote destination claims no moves (AD-37); both go through one destination interface (AD-38).
```

Replace the `- **Next action:**` bullet with:

```markdown
- **Next action:** The owner decides how to integrate branch `ask-the-backup` (merge, then a 0.2.0 release by `RELEASING.md`, since the behaviour changes). After that, design the replay over ssh, the first item in Open questions.
```

In "## Gotchas", add:

```markdown
- **An inode number proves a rename, never a copy.** A test may assert that a backup file kept its inode: only a rename does that, since a file rsync re-sends is written while the old one still holds the number. It must not assert that a re-sent file got a different inode: the filesystem can hand a just-deleted file's number to the next file rsync writes (seen 2026-11-25).
- **`monkeypatch.setattr("irsync.<module>.os.lstat", ...)` patches the one global `os` module,** so `Path.resolve()` and everything else that calls `os.lstat` goes through the patch too; a recording wrapper has to ignore the paths it is not about.
```

In "## Open questions", append this sentence to the bullet that starts "**Copies made by `--no-exclude` stay frozen on the backup":

```markdown
Since AD-36 a run with the excludes renames a file out of such a frozen copy when the file is moved into the synced tree and the copy still matches; everything else there stays frozen.
```

In `docs/superpowers/specs/2026-11-25-excluded-names-backup-truth-design.md`, replace the two-line `Status:` entry with:

```markdown
Status: implemented on branch `ask-the-backup` (plan:
`docs/superpowers/plans/2026-11-25-ask-the-backup.md`); not merged
```

- [ ] **Step 6: Check, gate and commit**

Run: `rg -n '^### 3[678]\.' docs/design/irsync-architecture.md` → three headings.
Run: `rg -c 'AD-3[678]' PROGRESS.md` → at least 3.
Run: `rg -n 'destination.py|baseline.py' README.md` → both listed.
Run: `git status --short` → only the four doc files are modified.

```bash
pixi run pre-commit run --all-files
pixi run test-cov
git add docs/design/irsync-architecture.md README.md PROGRESS.md docs/superpowers/specs/2026-11-25-excluded-names-backup-truth-design.md
git commit -m "docs: record AD-36 to AD-38 and what they amend

Co-Authored-By: Claude <noreply@anthropic.com>"
```
