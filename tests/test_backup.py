"""End-to-end orchestration tests for irsync.run_backup."""

import argparse
import shutil

import pytest

from irsync.backup import run_backup
from irsync.snapshot import SNAPSHOT_FILENAME

from .conftest import inode_for, tree_signature


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
            k: v for k, v in tree_signature(src).items() if k != SNAPSHOT_FILENAME
        }
        dest_sig = {
            k: v for k, v in tree_signature(dest).items() if k != SNAPSHOT_FILENAME
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


class TestSnapshotOnly:
    def test_snapshot_only_writes_source_snapshot_and_skips_backup(
        self, src_dest, basic_options, monkeypatch
    ):
        src, dest = src_dest
        # Wipe dest so we can assert it stays empty
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
