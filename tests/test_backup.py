"""End-to-end orchestration tests for irsync.run_backup."""

import argparse
import shutil

import pytest

from irsync.backup import _format_preview, run_backup
from irsync.diff import Changes
from irsync.snapshot import LOCKFILE_NAME, SNAPSHOT_FILENAME

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


def _args(**overrides):
    defaults = dict(
        ssh_port=None,
        ssh_key=None,
        no_exclude=False,
        yes=True,
        force=False,
        no_snapshot=False,
        snapshot_only=False,
        dry_run=False,
        debug=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


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
    def test_majority_deletion_refused_without_force(
        self, src_dest, basic_options, monkeypatch
    ):
        # M3 regression: if the diff says >50% of the previously-snapshotted
        # entries are gone, that almost certainly means the wrong source or a
        # stale snapshot. Refuse to proceed without --force.
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
        assert rc != 0, "catastrophic diff should refuse without --force"
        assert calls == [], "rsync must not run when sanity check fires"
        # And the dest tree should be untouched (still has all its files).
        dest_files = [
            p for p in dest.rglob("*") if p.is_file() and p.name != SNAPSHOT_FILENAME
        ]
        assert dest_files, "dest should not have been wiped"

    def test_majority_deletion_proceeds_with_force(self, src_dest, basic_options):
        # With --force, the user is explicitly overriding the safety check.
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
            args=_args(force=True),
        )
        assert rc == 0


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
