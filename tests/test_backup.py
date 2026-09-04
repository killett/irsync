"""End-to-end orchestration tests for irsync.run_backup."""

import logging
import os
import shutil
import subprocess

import pytest

from irsync.backup import (
    EXIT_REFUSED,
    _format_first_run_preview,
    _format_preview,
    _format_size,
    run_all_backups,
    run_backup,
)
from irsync.diff import Changes
from irsync.snapshot import LOCKFILE_NAME, SNAPSHOT_FILENAME, snapshot_tree

from .conftest import cli_args as _args
from .conftest import inode_for, tree_signature

_INTERNAL_FILES = {SNAPSHOT_FILENAME, LOCKFILE_NAME}


class TestFormatPreview:
    def test_includes_all_deletion_paths(self):
        # H4 regression: the preview MUST list every deletion path so the user
        # can review what's about to be removed by rsync --delete-before.
        changes = Changes(
            dir_moves=[("oldDir", "newDir")],
            file_moves=[("a.txt", "b.txt"), ("c.txt", "d.txt")],
            created=["new1.txt", "new2.txt"],
            deleted=["gone1.txt", "lost/important.doc", "removed/dir/file.bin"],
        )
        text = _format_preview(changes)
        for path in ("gone1.txt", "lost/important.doc", "removed/dir/file.bin"):
            assert path in text, f"deletion path {path!r} missing from preview"
        assert "oldDir" in text and "newDir" in text
        assert "a.txt" in text and "b.txt" in text
        assert "new1.txt" in text and "new2.txt" in text

    def test_empty_changes_renders_zero_counts(self):
        text = _format_preview(Changes())
        assert "(0)" in text  # all five sections show zero counts
        assert text.count("(0)") == 5

    def test_modified_files_listed_with_paths(self):
        # NEW-H1 regression: in-place modifications must appear in the preview
        # so the user knows which files rsync will re-transfer.
        changes = Changes(
            modified=["docs/report.md", "src/app.py"],
        )
        text = _format_preview(changes)
        assert "docs/report.md" in text
        assert "src/app.py" in text
        assert "Modified" in text


@pytest.fixture
def src_dest(tmp_path, make_tree):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    make_tree(src, num_files=6, depth=2)
    dest.mkdir()
    return src, dest


class TestFirstBackup:
    def test_first_backup_copies_tree_and_writes_snapshots(
        self, src_dest, basic_options
    ):
        src, dest = src_dest
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        # Dest tree mirrors src (excluding the snapshot file)
        src_sig = {
            k: v for k, v in tree_signature(src).items() if k not in _INTERNAL_FILES
        }
        dest_sig = {
            k: v for k, v in tree_signature(dest).items() if k not in _INTERNAL_FILES
        }
        assert src_sig == dest_sig
        # Snapshot files written on both sides
        assert (src / SNAPSHOT_FILENAME).exists()
        assert (dest / SNAPSHOT_FILENAME).exists()


class TestNoChangesShortCircuit:
    def test_second_run_with_no_changes_skips_rsync(
        self, src_dest, basic_options, monkeypatch
    ):
        src, dest = src_dest
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        # Wipe destination to PROVE rsync isn't called the second time
        # (we just want to verify run_real_sync isn't invoked).
        calls: list[list[str]] = []

        def fake_run(cmd):
            calls.append(cmd)
            return 0

        monkeypatch.setattr("irsync.backup.run_real_sync", fake_run)
        monkeypatch.setattr("irsync.backup.run_dry_run", lambda cmd: "")

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        assert calls == [], "rsync should not have been invoked when nothing changed"


class TestRenameAndEdit:
    def test_rename_plus_edit_converges_on_dest(self, src_dest, basic_options):
        # NEW-H3 regression: a rename combined with an in-place edit produces a
        # snapshot inode whose path AND content both differ from before. The
        # rename is rejected by the inode-reuse defense AND the modification
        # detector skips it because paths differ. Without H3's fix, the file
        # would be invisible to the diff and the dest would silently keep the
        # old content at the old path.
        src, dest = src_dest
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        # Pick a file, rename it, and rewrite its content. Use os.rename to
        # preserve the inode (so this is genuinely the rename+edit case).
        target = next(p for p in src.rglob("*.bin") if p.is_file())
        old_rel = target.relative_to(src).as_posix()
        new_rel = "renamed_and_edited.bin"
        new_content = b"BRAND NEW CONTENT FOR THE RENAME-AND-EDIT TEST" * 4
        target.rename(src / new_rel)
        with (src / new_rel).open("wb") as f:
            f.write(new_content)

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        # Dest must have the file at the new path with the new content,
        # and the old path must no longer exist on dest.
        assert (dest / new_rel).read_bytes() == new_content
        assert not (dest / old_rel).exists(), (
            "old path must be removed from dest by rsync's --delete-before"
        )


class TestInPlaceModification:
    def test_in_place_edit_triggers_backup_and_updates_dest(
        self, src_dest, basic_options
    ):
        # NEW-H1 regression: modifying a file in place (same path, same inode,
        # different content) MUST cause the next backup to run and update the
        # dest. Without the modification check, irsync's "no changes"
        # short-circuit would skip rsync entirely and the dest would stay
        # silently stale.
        src, dest = src_dest
        # First backup to seed.
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        # Pick a file and rewrite its content in place (no rename, no
        # delete+create — just a write to the existing path).
        target = next(p for p in src.rglob("*.bin") if p.is_file())
        rel = target.relative_to(src).as_posix()
        new_content = b"COMPLETELY DIFFERENT CONTENT FOR REGRESSION TEST" * 4
        # Use open("wb") so we don't change the inode (writing in place).
        with target.open("wb") as f:
            f.write(new_content)
        # Sanity: dest still has the old content right now.
        assert (dest / rel).read_bytes() != new_content

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        # The dest must now have the new content. If irsync skipped rsync,
        # this assertion fails and the bug is confirmed.
        assert (dest / rel).read_bytes() == new_content


class TestRenameOptimization:
    def test_renamed_file_keeps_dest_inode(self, src_dest, basic_options):
        src, dest = src_dest
        # First backup to establish baseline
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        # Pick a file in src and rename it
        src_files = sorted(p for p in src.rglob("*.bin") if p.is_file())
        assert src_files, "fixture should produce at least one file"
        src_file = src_files[0]
        src_rel = src_file.relative_to(src).as_posix()
        new_rel = "renamed_top_level.bin"
        src_file.rename(src / new_rel)

        # Capture the inode of the corresponding dest file BEFORE the second backup
        dest_file_old = dest / src_rel
        assert dest_file_old.exists()
        old_inode = inode_for(dest_file_old)
        old_content = dest_file_old.read_bytes()

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0

        # The renamed file on dest must keep the SAME inode — proving rsync
        # didn't delete-and-retransfer it.
        dest_file_new = dest / new_rel
        assert dest_file_new.exists()
        assert inode_for(dest_file_new) == old_inode
        assert dest_file_new.read_bytes() == old_content
        # Old path is gone on dest too
        assert not dest_file_old.exists()


class TestForceFlag:
    def test_force_runs_rsync_even_with_no_changes(
        self, src_dest, basic_options, monkeypatch
    ):
        src, dest = src_dest
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        calls: list[list[str]] = []
        real_run = __import__(
            "irsync.rsync_runner", fromlist=["run_real_sync"]
        ).run_real_sync

        def recording(cmd):
            calls.append(cmd)
            return real_run(cmd)

        monkeypatch.setattr("irsync.backup.run_real_sync", recording)

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(force=True),
        )
        assert rc == 0
        assert calls, "--force should invoke rsync even when nothing changed"


class TestLockfileNotTransferred:
    def test_lockfile_does_not_appear_on_dest(self, src_dest, basic_options):
        # NEW-H2 regression: the lockfile lives at <src>/.irsync.lock for the
        # duration of a run, but it must not be backed up to <dest>/.irsync.lock.
        src, dest = src_dest
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        assert not (dest / LOCKFILE_NAME).exists(), (
            "lockfile must be excluded from rsync transfer"
        )


class TestOrphanTempfileCleanup:
    def test_orphan_snap_tempfile_removed_from_source_on_startup(
        self, src_dest, basic_options
    ):
        # NEW-M1 regression: a leftover .irsync-snap-* tempfile from a previous
        # killed run must be cleaned up, not backed up.
        src, dest = src_dest
        from irsync.snapshot import SNAPSHOT_TEMPFILE_PREFIX

        orphan = src / f"{SNAPSHOT_TEMPFILE_PREFIX}leftover123"
        orphan.write_text("garbage from a crashed run")
        assert orphan.exists()

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        assert not orphan.exists(), (
            "orphan .irsync-snap-* tempfile should be cleaned up at startup"
        )
        # And it never made it to dest.
        assert not (dest / orphan.name).exists()

    def test_8th_m1_pager_subprocess_starts_new_session(self, monkeypatch):
        # 8th-M1: parity with run_real_sync's 7th-pass session isolation —
        # the `less` subprocess that pages the diff preview must also be
        # detached, so a SIGKILL of the parent doesn't leave less attached
        # to the parent's controlling tty as a zombie / orphan.
        from irsync import backup as backup_mod

        captured: dict[str, object] = {}

        class _FakeProc:
            def communicate(self, input=None):
                return ("", "")

            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, **kwargs):
            captured["kwargs"] = kwargs
            return _FakeProc()

        monkeypatch.setattr(backup_mod.shutil, "which", lambda name: "/usr/bin/less")
        monkeypatch.setattr(backup_mod.subprocess, "Popen", _fake_popen)
        backup_mod._page_output("preview text")
        kwargs = captured.get("kwargs", {})
        assert kwargs.get("start_new_session") is True, (
            "less pager must run in its own session/pgrp so a parent "
            "SIGKILL doesn't leave it zombied on the parent's tty"
        )

    def test_8th_h1_orphan_snap_tempfile_at_dest_root_also_cleaned(
        self, src_dest, basic_options
    ):
        # 8th-NEW-H1 regression: 7th-M1 flipped _persist_snapshots to write
        # the dest snapshot first. If the process dies between mkstemp and
        # os.replace during the dest write, an orphan .irsync-snap-* lands
        # at dest_root and nothing reaps it: rsync excludes the anchored
        # /.irsync-snap-* pattern, and _cleanup_orphan_tempfiles only
        # scanned src_root. The dest tempfile then accumulates indefinitely.
        src, dest = src_dest
        from irsync.snapshot import SNAPSHOT_TEMPFILE_PREFIX

        # Seed a baseline so the next run takes the snapshot/diff path.
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0

        dest_orphan = (
            dest / f"{SNAPSHOT_TEMPFILE_PREFIX}leftover_from_killed_dest_write"
        )
        dest_orphan.write_text("garbage from a kill mid dest-snapshot-write")
        assert dest_orphan.exists()

        # Need something to backup so the run actually goes through the
        # cleanup path (a no-changes run still cleans, but be explicit).
        (src / "trigger_change.bin").write_bytes(b"trigger")

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        assert not dest_orphan.exists(), (
            "orphan .irsync-snap-* tempfile at dest_root must be cleaned up — "
            "rsync excludes the anchored pattern so without an explicit cleanup "
            "call, the orphan accumulates forever"
        )


class TestF3FsyncBeforeReplace:
    """10th-pass F3: _atomic_write_snapshot must fsync the tempfile before
    os.replace. Without that, a power loss between rename and the kernel's
    delayed data flush leaves a directory entry pointing at empty/partial
    content, breaking the next run's snapshot read."""

    def test_10th_f3_fsync_called_on_tempfile_before_replace(
        self, tmp_path, monkeypatch
    ):
        from irsync import backup as backup_mod

        events: list[tuple[str, object]] = []
        real_fsync = os.fsync
        real_replace = os.replace

        def recording_fsync(fd):
            events.append(("fsync", fd))
            return real_fsync(fd)

        def recording_replace(src, dst):
            events.append(("replace", (str(src), str(dst))))
            return real_replace(src, dst)

        monkeypatch.setattr(backup_mod.os, "fsync", recording_fsync)
        monkeypatch.setattr(backup_mod.os, "replace", recording_replace)

        target = tmp_path / "snap.jsonl"
        rows = snapshot_tree(tmp_path)
        backup_mod._atomic_write_snapshot(rows, tmp_path, target)

        names = [name for name, _ in events]
        assert names.count("fsync") >= 1, "fsync must be called on the tempfile"
        assert names.count("replace") == 1, "replace must be called exactly once"
        # fsync must happen BEFORE replace, otherwise the durability hole stays open.
        assert names.index("fsync") < names.index("replace"), (
            "fsync must precede os.replace so the new content is on disk "
            "before the directory entry flips"
        )


class TestF2DestUntouchedOnNoChange:
    """10th-pass F2: AD-2 says the backup drive must NEVER be accessed when
    the source has no changes since the last run. _cleanup_orphan_tempfiles
    used to run on dest_root before the no-changes short-circuit, waking the
    drive on every run. The cleanup must move into the path that actually
    writes to dest."""

    def test_10th_f2_dest_root_not_iterdir_d_when_no_changes(
        self, src_dest, basic_options, monkeypatch
    ):
        from irsync import backup as backup_mod

        src, dest = src_dest
        # Seed so the second run takes the no-changes path.
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0

        # Spy on _cleanup_orphan_tempfiles. Record which roots it was called
        # with on the second (no-change) run. dest_root must not appear.
        cleanup_calls: list[str] = []
        real_cleanup = backup_mod._cleanup_orphan_tempfiles

        def recording_cleanup(root):
            cleanup_calls.append(str(root))
            return real_cleanup(root)

        monkeypatch.setattr(backup_mod, "_cleanup_orphan_tempfiles", recording_cleanup)

        # Belt-and-suspenders: assert rsync isn't invoked (proves no-changes path).
        rsync_calls: list[list[str]] = []
        monkeypatch.setattr(
            backup_mod, "run_real_sync", lambda cmd: rsync_calls.append(cmd) or 0
        )

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        assert rsync_calls == [], "no-change run should not invoke rsync"
        assert str(dest) not in cleanup_calls, (
            "dest tempfile cleanup must not run on a no-change backup — AD-2 "
            "says the backup drive must remain untouched"
        )

    def test_10th_f2_dest_orphan_still_cleaned_when_changes_present(
        self, src_dest, basic_options
    ):
        # Regression guard for 8th-NEW-H1: when the run actually touches dest
        # (changes present), the dest orphan-tempfile cleanup must still fire.
        from irsync.snapshot import SNAPSHOT_TEMPFILE_PREFIX

        src, dest = src_dest
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        dest_orphan = dest / f"{SNAPSHOT_TEMPFILE_PREFIX}f2_regression"
        dest_orphan.write_text("orphan from a killed dest write")
        # Trigger an actual change so we go through the "touch dest" path.
        (src / "f2_change.bin").write_bytes(b"trigger")

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        assert not dest_orphan.exists(), (
            "dest orphan must still be cleaned when the run actually writes "
            "to dest — 8th-NEW-H1 invariant"
        )


class TestSubdirFileWithSnapshotName:
    def test_subdir_file_named_like_snapshot_is_backed_up(
        self, src_dest, basic_options
    ):
        # H1 regression: a user file at <src>/sub/.irsync_snapshot.jsonl must NOT
        # be excluded just because it shares the basename of the snapshot file.
        src, dest = src_dest
        sub = src / "sub"
        sub.mkdir()
        nested = sub / SNAPSHOT_FILENAME
        nested.write_text("legitimate user data")
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        assert (dest / "sub" / SNAPSHOT_FILENAME).read_text() == "legitimate user data"
        # And the root-level snapshot is still NOT in the dest tree as a transferred
        # user file — it's written separately by irsync.
        # (We can't tell those apart by content, but we CAN assert that excluding
        # it from rsync didn't break the subdir file above.)


class TestCatastrophicDiffSanityCheck:
    def test_majority_deletion_refused_without_allow_massive_delete(
        self, src_dest, basic_options, monkeypatch
    ):
        # M3 regression: if the diff says >50% of the previously-snapshotted
        # entries are gone, that almost certainly means the wrong source or a
        # stale snapshot. Refuse to proceed without --allow-massive-delete.
        src, dest = src_dest
        # First backup to seed the snapshot.
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        # Now wipe most of the source to simulate the catastrophic diff.
        for child in src.iterdir():
            if child.name != SNAPSHOT_FILENAME:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        # Capture rsync to make sure it never runs.
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: calls.append(cmd) or 0
        )
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc != 0, "catastrophic diff should refuse without --allow-massive-delete"
        assert calls == [], "rsync must not run when sanity check fires"
        # And the dest tree should be untouched (still has all its files).
        dest_files = [
            p for p in dest.rglob("*") if p.is_file() and p.name != SNAPSHOT_FILENAME
        ]
        assert dest_files, "dest should not have been wiped"

    def test_majority_deletion_proceeds_with_allow_massive_delete(
        self, src_dest, basic_options
    ):
        # With --allow-massive-delete, the user is explicitly overriding the
        # safety check.
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
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(allow_massive_delete=True),
        )
        assert rc == 0

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


class TestSnapshotOnlyHonorsLock:
    def test_snapshot_only_blocked_by_held_lock(
        self, tmp_path, make_tree, basic_options
    ):
        # NEW-M1 (4th pass): --snapshot-only against a source whose lock is
        # held by another irsync run must refuse to race rather than risk a
        # concurrent _cleanup_orphan_tempfiles deleting the snapshot-only
        # tempfile mid-write.
        import fcntl

        from irsync.backup import LOCKFILE_NAME

        src = tmp_path / "src_only_locked"
        make_tree(src, num_files=3, depth=1)
        lock_path = src / LOCKFILE_NAME
        with lock_path.open("w") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            rc = run_backup(
                source_arg=str(src),
                destination_arg=None,
                options=basic_options,
                args=_args(snapshot_only=True),
            )
        assert rc != 0, "--snapshot-only should refuse when lock is held"
        # Snapshot file should NOT have been written (lock prevented it).
        assert not (src / SNAPSHOT_FILENAME).exists()


class TestLockfile:
    def test_concurrent_run_refused(self, src_dest, basic_options):
        # M2 regression: a second irsync against the same source while the
        # first is in-flight must refuse rather than race on the snapshot.
        import fcntl

        from irsync.backup import LOCKFILE_NAME

        src, dest = src_dest
        # Simulate the first run by holding the lock open ourselves.
        lock_path = src / LOCKFILE_NAME
        with lock_path.open("w") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            rc = run_backup(
                source_arg=str(src),
                destination_arg=str(dest),
                options=basic_options,
                args=_args(),
            )
        # The held lock should have caused run_backup to refuse.
        assert rc != 0, "second run should have refused while lock was held"

    def test_lock_released_after_normal_run(self, src_dest, basic_options):
        # After a normal run, the lock should be released so the next run works.
        import fcntl

        from irsync.backup import LOCKFILE_NAME

        src, dest = src_dest
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        lock_path = src / LOCKFILE_NAME
        assert lock_path.exists()
        # Should be acquireable now.
        with lock_path.open("w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class TestCrossDeviceReplayHandled:
    def test_5th_h2_xdev_returns_clean_error_no_partial_state_no_snapshot(
        self, src_dest, basic_options, monkeypatch
    ):
        # NEW-H2 (5th-pass): when apply_moves's pre-flight detects that a
        # planned move would cross filesystems on dest, run_backup must
        # surface a clean non-zero exit code, leave dest untouched, AND
        # NOT persist the new snapshot. Persisting would advance the
        # baseline past the unfinished work and silently lose the diff
        # the next run would have used to recover.
        from irsync.replay import CrossDeviceMoveError

        src, dest = src_dest
        # Seed the baseline so the second run actually produces a diff.
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        # Cause a rename on src so the next backup has at least one move
        # to plan; otherwise apply_moves wouldn't be invoked.
        target = next(p for p in src.rglob("*.bin") if p.is_file())
        target.rename(src / "renamed_for_xdev_test.bin")

        # Snapshot the source-side snapshot file's mtime so we can assert
        # it isn't overwritten by the failed run.
        snap_path = src / SNAPSHOT_FILENAME
        snap_before = snap_path.read_bytes()

        # Force the pre-flight to refuse, simulating a mount boundary inside dest.
        def always_refuse(*, dir_moves, file_moves, dest_root):
            raise CrossDeviceMoveError(18, "simulated EXDEV from pre-flight")

        monkeypatch.setattr("irsync.backup.apply_moves", always_refuse)
        # Capture rsync to make sure it doesn't run after the refusal.
        rsync_calls: list[list[str]] = []
        monkeypatch.setattr(
            "irsync.backup.run_real_sync",
            lambda cmd: rsync_calls.append(cmd) or 0,
        )

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc != 0, "xdev refusal must surface as a non-zero exit code"
        assert rsync_calls == [], (
            "rsync must not run after pre-flight refused the replay"
        )
        # Snapshot file unchanged — a future re-run with corrected dest
        # config will diff against the same baseline as this run.
        assert snap_path.read_bytes() == snap_before, (
            "snapshot must NOT be persisted after a refused replay"
        )


class TestProvenanceMismatchRejected:
    def test_snapshot_from_different_tree_is_rejected(
        self, tmp_path, make_tree, basic_options, monkeypatch
    ):
        # H3 regression: if a stale snapshot from another tree lands at
        # <src>/.irsync_snapshot.jsonl, irsync MUST refuse to use it as the
        # diff baseline (otherwise the diff would look catastrophic and
        # rsync's --delete-before would wipe legitimate dest data).
        src_a = tmp_path / "src_a"
        dest_a = tmp_path / "dest_a"
        make_tree(src_a, num_files=4, depth=1, seed=1)
        dest_a.mkdir()
        rc = run_backup(
            source_arg=str(src_a),
            destination_arg=str(dest_a),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        # Now stage a fresh, unrelated source tree, and plant src_a's snapshot
        # there as if the user copied it over by mistake.
        src_b = tmp_path / "src_b"
        dest_b = tmp_path / "dest_b"
        make_tree(src_b, num_files=4, depth=1, seed=2)
        dest_b.mkdir()
        shutil.copy2(src_a / SNAPSHOT_FILENAME, src_b / SNAPSHOT_FILENAME)

        # Capture rsync invocations; we want to assert that rsync DID run
        # (because the provenance mismatch falls back to "first backup"),
        # not that apply_moves replayed the wrong moves.
        replays: list[tuple] = []
        from irsync import backup as backup_mod
        from irsync.replay import apply_moves as real_apply_moves

        def recording_apply(*, dir_moves, file_moves, dest_root):
            replays.append((dir_moves, file_moves))
            return real_apply_moves(
                dir_moves=dir_moves, file_moves=file_moves, dest_root=dest_root
            )

        monkeypatch.setattr(backup_mod, "apply_moves", recording_apply)

        rc = run_backup(
            source_arg=str(src_b),
            destination_arg=str(dest_b),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        # apply_moves must NOT have been called with any moves — the
        # mismatched snapshot was rejected, so irsync treated this as a
        # first backup.
        assert all(not dm and not fm for dm, fm in replays), (
            f"replays should be empty after provenance rejection; got {replays!r}"
        )
        # And dest_b should match src_b's content (fresh backup worked).
        from .conftest import tree_signature

        src_sig = {
            k: v for k, v in tree_signature(src_b).items() if k not in _INTERNAL_FILES
        }
        dest_sig = {
            k: v for k, v in tree_signature(dest_b).items() if k not in _INTERNAL_FILES
        }
        assert src_sig == dest_sig


class TestSnapshotPersistedAfterRsyncSucceeds:
    def test_5th_snapshot_not_persisted_when_rsync_fails(
        self, src_dest, basic_options, monkeypatch
    ):
        # Verifies AD-equivalent: backup.py:401-406 guarantees that
        # _persist_snapshots runs only after rsync returns 0. If rsync
        # fails, the on-disk source-side snapshot must remain the OLD
        # baseline so the next run can still reconcile correctly.
        # Until this test, this behavior was claimed but unverified.
        src, dest = src_dest
        # First (successful) backup writes a baseline snapshot.
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        snap_path = src / SNAPSHOT_FILENAME
        baseline_bytes = snap_path.read_bytes()

        # Make a change so the second run actually does work.
        (src / "new_file.bin").write_bytes(b"new data")

        # Force rsync to fail.
        monkeypatch.setattr("irsync.backup.run_real_sync", lambda cmd: 23)

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc != 0, "failing rsync must propagate as non-zero"
        # The snapshot file must NOT have been overwritten with the new
        # rows (otherwise the next run would baseline against a snapshot
        # that includes new_file.bin even though it was never backed up).
        assert snap_path.read_bytes() == baseline_bytes, (
            "source snapshot must NOT be persisted after rsync fails"
        )


class TestSnapshotPersistOrdering:
    def test_7th_m1_dest_persisted_first_so_src_unchanged_when_dest_fails(
        self, src_dest, basic_options, monkeypatch
    ):
        # 7th-M1: _persist_snapshots used to write src before dest, so a
        # failure / SIGKILL between the two writes left src updated to the
        # fresh state and dest stale. On a DR restore that brought the
        # stale dest snapshot back as the new baseline, the user inherited
        # a snapshot that didn't reflect what dest actually contained.
        # The fix is to write dest first; if it fails, src stays at the
        # OLD baseline and the next run can re-attempt with consistent
        # state on both sides.
        import irsync.backup as backup_mod

        src, dest = src_dest
        # First (successful) backup writes a baseline snapshot to both.
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        snap_path = src / SNAPSHOT_FILENAME
        baseline_bytes = snap_path.read_bytes()

        # Make a change so the second run actually reaches the persist step.
        (src / "new_file.bin").write_bytes(b"new data")

        real_write = backup_mod._atomic_write_snapshot

        def _fail_on_dest(rows, source_root, target):
            if str(target).startswith(str(dest)):
                raise OSError("simulated dest snapshot write failure")
            return real_write(rows, source_root, target)

        monkeypatch.setattr(backup_mod, "_atomic_write_snapshot", _fail_on_dest)

        with pytest.raises(OSError, match="simulated dest snapshot write failure"):
            run_backup(
                source_arg=str(src),
                destination_arg=str(dest),
                options=basic_options,
                args=_args(),
            )

        assert snap_path.read_bytes() == baseline_bytes, (
            "source snapshot must remain at the old baseline when the dest "
            "snapshot write fails — DR-restored backups would otherwise "
            "inherit a snapshot newer than the dest content"
        )


class TestCycleBreakOrphanCleanup:
    def test_5th_orphan_mvtmp_dir_removed_by_rsync_delete_before(
        self, src_dest, basic_options
    ):
        # Verifies the architectural-decision-10 claim that orphan
        # __mvtmp__ directories from a killed cycle-break replay are
        # cleaned up by rsync's --delete-before on the next successful
        # run (because they exist on dest but not on source). Until
        # this test, this was claimed but unverified.
        src, dest = src_dest
        # Seed.
        run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        # Plant an orphan __mvtmp__ directory and file on dest, as if a
        # prior cycle-breaking apply_moves was killed mid-swap.
        orphan_dir = dest / "leftover.__mvtmp__deadbeef"
        orphan_dir.mkdir()
        (orphan_dir / "stale.txt").write_text("from a killed prior run")
        # Force a real backup (any change so rsync runs).
        (src / "trigger.bin").write_bytes(b"trigger")
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        assert not orphan_dir.exists(), (
            "rsync --delete-before must remove the orphan cycle-break dir "
            "from dest because it has no counterpart on source"
        )


class TestNoSnapshotYesAuditability:
    def test_5th_m1_no_snapshot_yes_prints_dry_run_preview_to_stdout(
        self, src_dest, basic_options, monkeypatch, capsys
    ):
        # NEW-M1 (5th-pass): AD-7 says --yes preserves the preview as
        # stdout so cron logs are auditable. The snapshot path honors this
        # via _show_preview(..., interactive=not args.yes); the --no-snapshot
        # path used to skip the preview entirely under --yes, leaving cron
        # users with no record of what rsync was about to do.
        src, dest = src_dest

        dry_run_marker = "==SIMULATED DRY-RUN OUTPUT==\nfile1.txt\nfile2.txt\n"
        monkeypatch.setattr("irsync.backup.run_dry_run", lambda cmd: dry_run_marker)
        monkeypatch.setattr("irsync.backup.run_real_sync", lambda cmd: 0)

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(no_snapshot=True),
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert dry_run_marker in captured.out, (
            "--no-snapshot --yes must print the dry-run preview to stdout "
            "for cron auditability (AD-7)"
        )


class TestDryRunFailureHandled:
    """rsync failing during the preview must abort cleanly, not raise."""

    @staticmethod
    def _raise_rsync_failure(returncode):
        def _fake_dry_run(cmd):
            raise subprocess.CalledProcessError(returncode, cmd)

        return _fake_dry_run

    def test_dry_run_flag_returns_rsync_exit_code(
        self, src_dest, basic_options, monkeypatch
    ):
        # rsync exits 23 (partial transfer) / 24 (vanished files) routinely on
        # a live tree. The --dry-run path used to let CalledProcessError escape
        # through cli.main, so the user got a traceback and exit 1 instead of
        # rsync's actual code.
        src, dest = src_dest
        monkeypatch.setattr("irsync.backup.run_dry_run", self._raise_rsync_failure(23))

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(dry_run=True),
        )
        assert rc == 23

    def test_no_snapshot_preview_failure_aborts_before_real_rsync(
        self, src_dest, basic_options, monkeypatch
    ):
        # The --no-snapshot path always runs a preview first. If that preview
        # fails (255 = ssh failure on a remote endpoint), irsync must report
        # the code AND must not fall through to the real transfer.
        src, dest = src_dest
        monkeypatch.setattr("irsync.backup.run_dry_run", self._raise_rsync_failure(255))
        real_sync_calls = []
        monkeypatch.setattr(
            "irsync.backup.run_real_sync",
            lambda cmd: real_sync_calls.append(cmd) or 0,
        )

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(no_snapshot=True),
        )
        assert rc == 255
        assert real_sync_calls == [], (
            "a failed preview must not fall through to the real rsync run"
        )

    def test_signal_killed_rsync_maps_to_a_positive_exit_code(
        self, src_dest, basic_options, monkeypatch
    ):
        # A subprocess killed by a signal reports a NEGATIVE returncode
        # (-SIGTERM). Returning that verbatim would hand SystemExit a negative
        # value, which the shell wraps around to a meaningless status.
        src, dest = src_dest
        monkeypatch.setattr("irsync.backup.run_dry_run", self._raise_rsync_failure(-15))

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(dry_run=True),
        )
        assert rc == EXIT_REFUSED
        assert rc > 0


class TestNoSnapshotDryRunHonored:
    """Hazard H: ``--no-snapshot --dry-run`` must not run a real rsync.

    ``_run_rsync_only`` used to build a dry-run command for the preview and
    then unconditionally build a SECOND, real (``dry_run=False``) command
    and call ``run_real_sync`` — ``args.dry_run`` was never consulted on
    this path, so ``--no-snapshot --dry-run`` deleted anything at the
    destination not present on the source. The snapshot path already
    honored ``--dry-run`` correctly (``backup.py:696``); this brings the
    two paths in line.

    ``test_dry_run_leaves_destination_untouched`` deliberately does not
    monkeypatch ``run_dry_run``/``run_real_sync``: it runs the real rsync
    binary so a regression shows up as actual data loss on disk, not merely
    a changed return code. It fails against the pre-fix code.
    """

    def test_dry_run_leaves_destination_untouched(self, src_dest, basic_options):
        src, dest = src_dest
        precious = dest / "precious.txt"
        precious.write_text("precious original content")

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(no_snapshot=True, dry_run=True),
        )

        assert rc == 0
        assert precious.exists(), (
            "--no-snapshot --dry-run deleted a destination-only file "
            "(hazard H): --dry-run must change nothing"
        )
        assert precious.read_text() == "precious original content"

    def test_without_dry_run_still_performs_real_sync(self, src_dest, basic_options):
        # Guard against over-correcting hazard H into "--no-snapshot never
        # syncs": without --dry-run the real, destructive transfer must
        # still happen exactly as before.
        src, dest = src_dest
        stale = dest / "stale.txt"
        stale.write_text("not on source; a real sync must remove it")

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(no_snapshot=True, dry_run=False),
        )

        assert rc == 0
        assert not stale.exists(), (
            "--no-snapshot without --dry-run must still delete dest-only "
            "entries via a real rsync run"
        )

    def test_dry_run_never_calls_run_real_sync(
        self, src_dest, basic_options, monkeypatch
    ):
        src, dest = src_dest
        (dest / "precious.txt").write_text("precious original content")

        def _fail_if_called(cmd):
            pytest.fail("run_real_sync must not be called on the --dry-run path")

        monkeypatch.setattr("irsync.backup.run_real_sync", _fail_if_called)

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(no_snapshot=True, dry_run=True),
        )
        assert rc == 0


class TestRunAllBackups:
    """`irsync ALL` treats an unmounted drive as a skip, not a failure."""

    @staticmethod
    def _options_with(make_options, entries):
        """Options whose configured backup order is exactly ``entries``."""
        return make_options(backup_order=entries)

    def test_missing_drive_is_skipped_without_failing_the_run(
        self, make_options, monkeypatch, caplog
    ):
        # Most of the 16 configured entries are external drives that are not
        # mounted on any given day, so "drive absent" is the normal case for
        # an ALL run. Exiting non-zero here would make every nightly ALL run
        # look like a failure.
        def fake_backup(*, source_arg, destination_arg, options, args):
            if source_arg == "C":
                raise FileNotFoundError(f"Path '{source_arg}' does not exist.")
            return 0

        monkeypatch.setattr("irsync.backup.run_backup", fake_backup)

        with caplog.at_level(logging.WARNING):
            rc = run_all_backups(
                options=self._options_with(make_options, ["B", "C", "~"]),
                args=_args(),
            )

        assert rc == 0
        assert "C" in caplog.text

    def test_summary_names_the_drives_that_were_backed_up(
        self, make_options, monkeypatch, caplog
    ):
        # The run collected a `successful` list but never reported it, so an
        # ALL run that skipped a drive left no record of which drives DID get
        # backed up — exactly the audit trail a cron log needs.
        def fake_backup(*, source_arg, destination_arg, options, args):
            if source_arg == "C":
                raise FileNotFoundError(f"Path '{source_arg}' does not exist.")
            return 0

        monkeypatch.setattr("irsync.backup.run_backup", fake_backup)

        with caplog.at_level(logging.INFO):
            run_all_backups(
                options=self._options_with(make_options, ["B", "C", "~"]),
                args=_args(),
            )

        assert "B" in caplog.text and "~" in caplog.text
        summary = [r for r in caplog.records if "backed up" in r.getMessage()]
        assert summary, "the summary must report which drives were backed up"
        assert "B" in summary[0].getMessage()
        assert "~" in summary[0].getMessage()

    def test_a_real_backup_error_still_fails_the_run(self, make_options, monkeypatch):
        # The skip-is-not-an-error rule must not swallow genuine failures:
        # a non-zero return from run_backup is a real error and must surface.
        def fake_backup(*, source_arg, destination_arg, options, args):
            return EXIT_REFUSED if source_arg == "B" else 0

        monkeypatch.setattr("irsync.backup.run_backup", fake_backup)

        rc = run_all_backups(
            options=self._options_with(make_options, ["B", "~"]),
            args=_args(),
        )
        assert rc == 1


class TestSnapshotOnly:
    def test_snapshot_only_works_without_destination(
        self, tmp_path, make_tree, basic_options, monkeypatch
    ):
        # M1 regression: --snapshot-only should not require a destination.
        # The whole point is "establish a baseline before I touch anything",
        # which is useful for any local directory regardless of backup config.
        src = tmp_path / "src_only"
        make_tree(src, num_files=4, depth=1)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: calls.append(cmd) or 0
        )

        rc = run_backup(
            source_arg=str(src),
            destination_arg=None,
            options=basic_options,
            args=_args(snapshot_only=True),
        )
        assert rc == 0
        assert (src / SNAPSHOT_FILENAME).exists()
        assert calls == [], "rsync should not run when --snapshot-only is set"

    def test_snapshot_only_with_destination_still_works(
        self, src_dest, basic_options, monkeypatch
    ):
        # Backwards-compatible: passing a dest with --snapshot-only is allowed
        # but the dest is left untouched.
        src, dest = src_dest
        shutil.rmtree(dest)
        dest.mkdir()
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: calls.append(cmd) or 0
        )

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(snapshot_only=True),
        )
        assert rc == 0
        assert (src / SNAPSHOT_FILENAME).exists()
        assert not (dest / SNAPSHOT_FILENAME).exists()
        assert calls == []


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

    def test_snapshot_only_unmounted_source_under_base_dir_refuses_before_lock(
        self, src_dest, basic_options, monkeypatch
    ):
        # Acceptance criterion 4: --snapshot-only must run the same gate,
        # in the same order (before _source_lock), as a regular backup.
        # Same fixture-and-monkeypatch shape as
        # test_unmounted_source_refuses_and_writes_no_lockfile, but routed
        # through the --snapshot-only path (no destination).
        from irsync.preflight import EndpointNotMounted

        src, _dest = src_dest
        monkeypatch.setattr("os.path.ismount", lambda p: False)
        basic_options.base_dir = src.parent

        with pytest.raises(EndpointNotMounted):
            run_backup(
                source_arg=str(src),
                destination_arg=None,
                options=basic_options,
                args=_args(snapshot_only=True),
            )

        assert not (src / LOCKFILE_NAME).exists(), (
            "the gate must run before the lock is taken, even for --snapshot-only"
        )

    def test_require_mount_extends_snapshot_only_gate_outside_base_dir(
        self, tmp_path, basic_options, make_tree, monkeypatch
    ):
        # Regression for the asymmetry flagged in review: run_backup's gate
        # honors --require-mount (gate_outside_base=args.require_mount), but
        # _run_snapshot_only's gate used to hardcode gate_outside_base=False,
        # so --require-mount was silently inert for --snapshot-only runs
        # against a path outside base_dir. Source here is deliberately NOT
        # under basic_options.base_dir.
        from irsync.preflight import EndpointNotMounted

        src = tmp_path / "external_src"
        make_tree(src, num_files=3, depth=1)
        monkeypatch.setattr("os.path.ismount", lambda p: False)

        with pytest.raises(EndpointNotMounted):
            run_backup(
                source_arg=str(src),
                destination_arg=None,
                options=basic_options,
                args=_args(snapshot_only=True, require_mount=True),
            )

        assert not (src / LOCKFILE_NAME).exists(), (
            "the gate must run before the lock is taken"
        )

    def test_unmounted_destination_refuses_even_when_source_is_mounted(
        self, src_dest, basic_options, monkeypatch
    ):
        # Hazard B: an unmounted destination fills the root filesystem.
        # Every other TestMountGate test sets os.path.ismount to a constant,
        # so the SOURCE check (which runs first) always raises and the
        # DEST check is never exercised. Here only src reports as mounted,
        # so this fails closed on the source check ONLY if the dest check
        # were skipped entirely — it must not be.
        from irsync.preflight import EndpointNotMounted

        src, dest = src_dest
        basic_options.base_dir = src.parent
        monkeypatch.setattr("os.path.ismount", lambda p: str(p) == str(src))
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: pytest.fail("rsync ran")
        )

        with pytest.raises(EndpointNotMounted) as excinfo:
            run_backup(
                source_arg=str(src),
                destination_arg=str(dest),
                options=basic_options,
                args=_args(),
            )
        assert str(dest) in str(excinfo.value)


class TestRunAllBackupsMountSkips:
    def test_unmounted_drive_counts_as_missing_not_error(
        self, make_options, monkeypatch, caplog
    ):
        # Contract from commit 044c036: a drive that isn't there is a skip.
        # An unmounted drive is the same situation, so it must not flip the
        # exit code of an ALL run.
        from irsync.preflight import EndpointNotMounted

        def fake_backup(*, source_arg, destination_arg, options, args):
            if source_arg == "C":
                raise EndpointNotMounted("C is not a mountpoint")
            return 0

        monkeypatch.setattr("irsync.backup.run_backup", fake_backup)
        options = make_options(backup_order=["B", "C", "~"])

        with caplog.at_level(logging.INFO):
            rc = run_all_backups(options=options, args=_args())
        assert rc == 0
        # rc == 0 alone would still pass if C had been appended to
        # `successful` instead of `missing` (a bug that silently reports an
        # unmounted drive as backed up). Pin the actual summary line so the
        # drive is provably named under missing/skipped, not successful.
        summary = next(
            r.getMessage()
            for r in caplog.records
            if "missing/skipped" in r.getMessage()
        )
        assert "missing/skipped=1 (C)" in summary
        assert "backed up=2 (B, ~)" in summary


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

    def test_empty_dest_first_backup_still_works(self, src_dest, basic_options):
        # The gate must not break the ordinary first backup.
        src, dest = src_dest
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0

    def test_dry_run_against_nonempty_first_run_dest_does_not_refuse(
        self, tmp_path, make_tree, basic_options, monkeypatch
    ):
        # Deferred finding: --dry-run writes nothing, so it is the natural,
        # safe way to inspect a non-empty first-run destination before
        # deciding whether to adopt it with --allow-nonempty-dest. Blocking
        # it at exactly the same refusal as the real run pushes users toward
        # the flag they least want reached for reflexively.
        src = tmp_path / "src"
        make_tree(src, num_files=3, depth=1)
        dest = tmp_path / "dest"
        dest.mkdir()
        precious = dest / "old_backup.txt"
        precious.write_text("irreplaceable")
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: pytest.fail("rsync ran")
        )
        monkeypatch.setattr(
            "irsync.backup.run_dry_run", lambda cmd: "dry run preview output"
        )

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(dry_run=True),
        )

        assert rc == 0
        # Nothing was actually modified: the destination still holds exactly
        # the one pre-existing file, with its original content.
        assert precious.read_text() == "irreplaceable"
        assert [p.name for p in dest.iterdir()] == ["old_backup.txt"]


class TestEmptySourceGate:
    """A baseline-less backup from an EMPTY source is never legitimate."""

    def test_empty_source_no_baseline_refuses_and_preserves_dest(
        self, tmp_path, basic_options, monkeypatch, caplog
    ):
        # Hazard A again, but caught one layer earlier: even a destination
        # holding real data is refused here on the SOURCE side, before the
        # destination is even inspected for foreign entries.
        src = tmp_path / "src"
        src.mkdir()  # exists but empty: snapshot_tree will yield only "."
        dest = tmp_path / "dest"
        dest.mkdir()
        precious = dest / "old_backup.txt"
        precious.write_text("irreplaceable")
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
        assert precious.read_text() == "irreplaceable"
        # Pin the message to THIS guard (not Rule 2's foreign-dest refusal),
        # which also happens to be reachable in this same scenario.
        assert "--allow-empty-source" in caplog.text

    def test_empty_source_no_baseline_refuses_even_with_empty_dest(
        self, tmp_path, basic_options, monkeypatch
    ):
        # Proves the guard fires on SOURCE content alone: an empty dest
        # means Rule 2 (foreign_dest_entries) would never fire here, so any
        # refusal is unambiguously this guard.
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
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
        assert list(dest.iterdir()) == []

    def test_allow_empty_source_permits_it(self, tmp_path, basic_options):
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(allow_empty_source=True),
        )
        assert rc == 0

    def test_source_with_content_is_unaffected(self, src_dest, basic_options):
        # Regression: the guard must not get in the way of an ordinary
        # first backup just because there is no baseline yet.
        src, dest = src_dest
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0

    def test_no_snapshot_path_refuses_on_empty_source(
        self, tmp_path, basic_options, monkeypatch
    ):
        # --no-snapshot never walks the tree, so there is no fresh_rows to
        # check via the snapshot path's guard; this exercises the direct
        # source_root_is_empty check instead.
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: pytest.fail("rsync ran")
        )
        monkeypatch.setattr(
            "irsync.backup.run_dry_run", lambda cmd: pytest.fail("rsync dry-run ran")
        )

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(no_snapshot=True),
        )

        assert rc == EXIT_REFUSED

    def test_no_snapshot_path_allow_empty_source_permits_it(
        self, tmp_path, basic_options
    ):
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(no_snapshot=True, allow_empty_source=True),
        )
        assert rc == 0

    def test_empty_source_with_baseline_uses_massive_delete_guard_not_this_one(
        self, tmp_path, make_tree, basic_options
    ):
        # A baseline EXISTS here, so this must NOT be caught by the
        # empty-source guard (which only fires when there is no baseline).
        # A user who genuinely deleted everything is handled by the
        # existing catastrophic-delete guard and --allow-massive-delete;
        # firing both guards would be redundant and confusing.
        src = tmp_path / "src"
        make_tree(src, num_files=4, depth=1)
        dest = tmp_path / "dest"
        dest.mkdir()

        # Establish a baseline via a normal first backup.
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        assert (src / SNAPSHOT_FILENAME).exists()

        # Empty the source entirely, keeping the snapshot as the baseline.
        for child in src.iterdir():
            if child.name in (SNAPSHOT_FILENAME, LOCKFILE_NAME):
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

        # allow_massive_delete ALONE (no allow_empty_source) must be
        # sufficient: if the new guard fired here too, this would refuse.
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(allow_massive_delete=True),
        )
        assert rc == 0


class TestRunAllBackupsUnsafeDestination:
    def test_unsafe_destination_counts_as_error_not_skip_and_batch_continues(
        self, make_options, monkeypatch
    ):
        # An unreadable destination is not a benign absence like a missing or
        # unmounted drive - it must be a real error (exit code 1), unlike the
        # missing/unmounted skip contract from 044c036. It also must not
        # abort the whole ALL batch: drives after the bad one still get a
        # chance to run.
        from irsync.preflight import UnsafeDestination

        attempted: list[str] = []

        def fake_backup(*, source_arg, destination_arg, options, args):
            attempted.append(source_arg)
            if source_arg == "C":
                raise UnsafeDestination("C's destination cannot be read")
            return 0

        monkeypatch.setattr("irsync.backup.run_backup", fake_backup)
        options = make_options(backup_order=["B", "C", "~"])

        rc = run_all_backups(options=options, args=_args())

        assert attempted == ["B", "C", "~"], "batch must continue past the bad drive"
        assert rc == 1, "an unreadable destination must not keep the exit code at 0"


class TestRunAllBackupsDestinationMountGate:
    """An unmounted DESTINATION must fail an ALL run; an unmounted SOURCE must not."""

    def test_unmounted_destination_is_error_and_batch_continues(
        self, make_tree, make_options, monkeypatch, caplog
    ):
        # Real end-to-end wiring (no mocking of run_backup): drive B's SOURCE
        # is mounted but its DESTINATION (B_backup) is not. That must be
        # counted as a real error, not a skip, and must not stop the batch
        # from reaching C.
        options = make_options(backup_order=["B", "C"])
        base = options.base_dir
        b_src = base / "B"
        make_tree(b_src, num_files=2, depth=1)
        b_dest = base / "B_backup"
        b_dest.mkdir()
        c_src = base / "C"
        c_src.mkdir()

        # Only B's source reports as mounted. B's destination and C's source
        # (a plain unmounted drive) do not.
        monkeypatch.setattr("os.path.ismount", lambda p: str(p) == str(b_src))
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: pytest.fail("rsync ran")
        )

        with caplog.at_level(logging.INFO):
            rc = run_all_backups(options=options, args=_args())

        assert rc == 1, "an unmounted destination must not keep the exit code at 0"
        summary = next(
            r.getMessage()
            for r in caplog.records
            if "missing/skipped" in r.getMessage()
        )
        # B is neither successful nor missing: it's a counted error. C, whose
        # SOURCE is unmounted, is still the benign skip.
        assert "missing/skipped=1 (C)" in summary
        assert "errors=1" in summary
        assert "backed up=0" in summary

    def test_source_unmounted_in_the_same_run_still_keeps_exit_0(
        self, make_options, monkeypatch
    ):
        # Regression for the skip-is-not-an-error contract (commit 044c036):
        # this must hold even now that check_mounted distinguishes roles.
        # Only C is in the backup order here (source unmounted, real
        # check_mounted, no mocking of run_backup) so this test fails
        # independently of the destination-side assertions above.
        options = make_options(backup_order=["C"])
        base = options.base_dir
        c_src = base / "C"
        c_src.mkdir()

        monkeypatch.setattr("os.path.ismount", lambda p: False)
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: pytest.fail("rsync ran")
        )

        rc = run_all_backups(options=options, args=_args())

        assert rc == 0, "an unmounted SOURCE in an ALL run must still be a skip"


class TestFormatSize:
    @pytest.mark.parametrize(
        ("total_bytes", "expected"),
        [
            (0, "0 B"),
            (1023, "1023 B"),
            (1024, "1.0 KiB"),
            (1024 * 1024 - 1, "1.0 MiB"),
            (1024**3, "1.0 GiB"),
            (1024**4, "1.0 TiB"),
        ],
    )
    def test_exact_output_at_unit_boundaries(self, total_bytes, expected):
        # Pin the arithmetic, not just "contains GiB": a substring assertion
        # would pass even if the wrong number were rendered. 1024*1024 - 1
        # is the interesting boundary — the raw KiB value (1023.999...) is
        # < 1024 but rounds to "1024.0" at one decimal place, which reads
        # like a unit that should have rolled over. _format_size rounds
        # before comparing so it reports "1.0 MiB" instead of the
        # misleading "1024.0 KiB".
        assert _format_size(total_bytes) == expected


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

    def test_interactive_first_run_pages_instead_of_printing(
        self, src_dest, basic_options, monkeypatch, capsys
    ):
        # AD-7: only --yes writes straight to stdout. Interactively the
        # summary must go through the same pager as the diffed preview,
        # not straight to print().
        src, dest = src_dest
        paged: list[str] = []
        monkeypatch.setattr(
            "irsync.backup._page_output", lambda text: paged.append(text)
        )
        monkeypatch.setattr("builtins.input", lambda prompt: "yes")

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(yes=False),
        )

        assert rc == 0
        assert len(paged) == 1
        assert "FIRST BACKUP" in paged[0]
        out = capsys.readouterr().out
        assert "FIRST BACKUP" not in out, (
            "interactive mode must not also print the summary to stdout"
        )

    def test_summary_also_shown_when_snapshot_provenance_mismatched(
        self, tmp_path, make_tree, basic_options, capsys
    ):
        # The first-run summary has two triggers: no snapshot at all (tested
        # above), and a snapshot that exists but is rejected because its
        # recorded source_root doesn't match (see
        # TestProvenanceMismatchRejected). Both fall through to the same
        # "changes is None" branch, so both must render the summary — not
        # just the no-snapshot case.
        src_a = tmp_path / "src_a"
        dest_a = tmp_path / "dest_a"
        make_tree(src_a, num_files=4, depth=1, seed=1)
        dest_a.mkdir()
        rc = run_backup(
            source_arg=str(src_a),
            destination_arg=str(dest_a),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0

        # Stage a fresh, unrelated source tree and plant src_a's snapshot
        # there, as if the user copied it over by mistake.
        src_b = tmp_path / "src_b"
        dest_b = tmp_path / "dest_b"
        make_tree(src_b, num_files=4, depth=1, seed=2)
        dest_b.mkdir()
        shutil.copy2(src_a / SNAPSHOT_FILENAME, src_b / SNAPSHOT_FILENAME)
        capsys.readouterr()  # discard the first run's output

        rc = run_backup(
            source_arg=str(src_b),
            destination_arg=str(dest_b),
            options=basic_options,
            args=_args(),
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "FIRST BACKUP" in out
        assert str(src_b) in out
        assert str(dest_b) in out
        assert "DELETE" in out

    def test_remote_destination_reports_unchecked_not_a_fabricated_zero(self, tmp_path):
        # A remote destination is never scanned by foreign_dest_entries (Task
        # 3 only counts a local dest), so foreign_count is always 0 for a
        # remote dest -- not because it's empty, but because it was never
        # checked. The summary must say so instead of asserting "0 existing
        # entries" as if that were a verified fact.
        rows: list = [
            {
                "dev": 1,
                "ino": 1,
                "type": "f",
                "nlink": 1,
                "size": 10,
                "mtime_ns": 0,
                "btime_ns": -1,
                "path": "a.txt",
            }
        ]
        text = _format_first_run_preview(rows, tmp_path / "src", None, 0)
        assert "not checked" in text
        assert "0 existing entries" not in text


class TestCleanRefusals:
    def test_missing_rsync_binary_raises_before_touching_anything(
        self, src_dest, basic_options, monkeypatch
    ):
        # Reproduced against the old code: FileNotFoundError from subprocess
        # escaped run_dry_run/run_real_sync as a stack trace, exit code 1.
        #
        # run_backup RAISES here rather than returning a code, the same
        # design as EndpointNotMounted/UnsafeDestination: it's cli.main's
        # boundary (tested at the process level in test_cli.py) that turns
        # this into EXIT_REFUSED for a single-drive run. Asserting rc ==
        # EXIT_REFUSED directly against run_backup here would be testing for
        # behavior the function deliberately does not have.
        from irsync.preflight import RsyncUnavailable

        src, dest = src_dest
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: pytest.fail("rsync ran")
        )

        with pytest.raises(RsyncUnavailable):
            run_backup(
                source_arg=str(src),
                destination_arg=str(dest),
                options=basic_options,
                args=_args(),
            )

        assert not (src / LOCKFILE_NAME).exists(), (
            "the rsync-availability check must run before the lock is taken"
        )

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

    def test_destination_permission_error_is_not_misattributed_to_source(
        self, tmp_path, basic_options, caplog
    ):
        # Reproduced against the pre-fix code: the `try` around run_backup's
        # entire orchestrated run wrapped its `except PermissionError` around
        # far more than lock acquisition, so a PermissionError raised deep
        # inside the run (here: os.rename inside apply_moves, because the
        # DEST tree is read-only) was reported as "Cannot write to the
        # source root", even though the source was never the problem. Set up
        # a pending rename so the second run reaches apply_moves.
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        dest.mkdir()
        (src / "a.txt").write_text("hello")

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0

        (src / "a.txt").rename(src / "b.txt")
        dest.chmod(0o555)
        try:
            with caplog.at_level(logging.ERROR):
                rc = run_backup(
                    source_arg=str(src),
                    destination_arg=str(dest),
                    options=basic_options,
                    args=_args(),
                )
        finally:
            dest.chmod(0o755)

        assert rc == EXIT_REFUSED
        assert "source root" not in caplog.text, (
            "a destination-side PermissionError must not be reported as a "
            "source-root problem"
        )
        assert str(dest) in caplog.text or "a.txt" in caplog.text

    def test_snapshot_only_succeeds_without_rsync_no_destination(
        self, tmp_path, make_tree, basic_options, monkeypatch
    ):
        # Regression: check_rsync_available() used to run unconditionally as
        # the first statement of run_backup, making rsync a hard dependency
        # of --snapshot-only even though that mode never invokes rsync at
        # all (it only walks the source and writes a snapshot file). This is
        # the no-destination route (routed to _run_snapshot_only directly).
        src = tmp_path / "src_only"
        make_tree(src, num_files=4, depth=1)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: pytest.fail("rsync ran")
        )

        rc = run_backup(
            source_arg=str(src),
            destination_arg=None,
            options=basic_options,
            args=_args(snapshot_only=True),
        )
        assert rc == 0
        assert (src / SNAPSHOT_FILENAME).exists()

    def test_snapshot_only_succeeds_without_rsync_with_destination(
        self, src_dest, basic_options, monkeypatch
    ):
        # Same regression, the with-destination route: --snapshot-only plus
        # a destination goes through resolve_endpoints and
        # _run_backup_for_endpoints's own args.snapshot_only branch, which
        # also returns before ever touching rsync.
        src, dest = src_dest
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: pytest.fail("rsync ran")
        )

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(snapshot_only=True),
        )
        assert rc == 0
        assert (src / SNAPSHOT_FILENAME).exists()
