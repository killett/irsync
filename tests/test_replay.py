import errno
import os
from unittest.mock import patch

import pytest

from irsync.replay import CrossDeviceMoveError, ReplayResult, apply_moves


class TestApplyMoves:
    def test_simple_file_rename(self, tmp_path):
        (tmp_path / "old.txt").write_text("data")
        result = apply_moves(
            dir_moves=[], file_moves=[("old.txt", "new.txt")], dest_root=tmp_path
        )
        assert (tmp_path / "new.txt").read_text() == "data"
        assert not (tmp_path / "old.txt").exists()
        assert isinstance(result, ReplayResult)
        assert result.files_moved == 1
        assert result.dirs_moved == 0

    def test_file_move_creates_parent_dirs(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        apply_moves(
            dir_moves=[],
            file_moves=[("a.txt", "deep/nested/b.txt")],
            dest_root=tmp_path,
        )
        assert (tmp_path / "deep" / "nested" / "b.txt").read_text() == "x"

    def test_directory_rename_moves_contents(self, tmp_path):
        (tmp_path / "old").mkdir()
        (tmp_path / "old" / "f.txt").write_text("hi")
        result = apply_moves(
            dir_moves=[("old", "new")], file_moves=[], dest_root=tmp_path
        )
        assert (tmp_path / "new" / "f.txt").read_text() == "hi"
        assert not (tmp_path / "old").exists()
        assert result.dirs_moved == 1

    def test_directory_swap_uses_temp(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "marker_a").write_text("A")
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "marker_b").write_text("B")
        apply_moves(
            dir_moves=[("a", "b"), ("b", "a")], file_moves=[], dest_root=tmp_path
        )
        assert (tmp_path / "a" / "marker_b").read_text() == "B"
        assert (tmp_path / "b" / "marker_a").read_text() == "A"

    def test_refuses_to_clobber_existing_destination(self, tmp_path):
        (tmp_path / "src.txt").write_text("src")
        (tmp_path / "dst.txt").write_text("preexisting")
        result = apply_moves(
            dir_moves=[],
            file_moves=[("src.txt", "dst.txt")],
            dest_root=tmp_path,
        )
        # Source is left in place; destination is untouched
        assert (tmp_path / "src.txt").read_text() == "src"
        assert (tmp_path / "dst.txt").read_text() == "preexisting"
        assert result.skipped == 1
        assert result.files_moved == 0

    def test_dir_moves_have_paths_rewritten_for_files(self, tmp_path):
        # When dir 'old' is renamed to 'new', a file move that referenced 'old/f.txt'
        # at the source must be looked up at 'new/f.txt' on the destination tree
        # because the dir move ran first.
        (tmp_path / "old").mkdir()
        (tmp_path / "old" / "f.txt").write_text("hi")
        apply_moves(
            dir_moves=[("old", "new")],
            file_moves=[("old/f.txt", "new/renamed.txt")],
            dest_root=tmp_path,
        )
        assert (tmp_path / "new" / "renamed.txt").read_text() == "hi"
        assert not (tmp_path / "new" / "f.txt").exists()

    def test_exdev_is_raised(self, tmp_path):
        (tmp_path / "src.txt").write_text("x")
        real_rename = os.rename

        def fake_rename(src, dst):
            err = OSError(errno.EXDEV, "Invalid cross-device link")
            err.errno = errno.EXDEV
            raise err

        with patch("irsync.replay.os.rename", side_effect=fake_rename):
            with pytest.raises(OSError) as exc_info:
                apply_moves(
                    dir_moves=[],
                    file_moves=[("src.txt", "dst.txt")],
                    dest_root=tmp_path,
                )
            assert exc_info.value.errno == errno.EXDEV
        # Ensure we didn't accidentally call the real rename
        assert real_rename is os.rename

    def test_5th_h2_preflight_refuses_crossing_devices_before_any_rename(
        self, tmp_path
    ):
        # NEW-H2 (5th-pass): if ANY planned move would cross a filesystem
        # boundary inside dest_root, apply_moves must raise
        # CrossDeviceMoveError BEFORE running os.rename on any move. The
        # pre-flight protects against the perpetual-retry trap where a
        # mid-flight EXDEV leaves dest in partial state and the next run
        # hits the same EXDEV at the same point forever.
        (tmp_path / "stay.txt").write_text("a")
        (tmp_path / "cross.txt").write_text("b")
        (tmp_path / "early.txt").write_text("c")

        real_dev = tmp_path.stat().st_dev
        cross_path = (tmp_path / "cross.txt").resolve()

        def fake_dev_of(path):
            # Pretend `cross.txt` lives on a different filesystem; everything
            # else is on the same fs as dest_root.
            if path.resolve() == cross_path:
                return real_dev + 1
            return real_dev

        with patch("irsync.replay._dev_of", side_effect=fake_dev_of):
            with pytest.raises(CrossDeviceMoveError):
                apply_moves(
                    dir_moves=[],
                    file_moves=[
                        ("early.txt", "early-new.txt"),
                        ("stay.txt", "stay-new.txt"),
                        ("cross.txt", "cross-new.txt"),
                    ],
                    dest_root=tmp_path,
                )
        # No partial state: all originals still in place, no new paths created.
        assert (tmp_path / "early.txt").exists(), (
            "pre-flight must refuse upfront — no rename should have happened"
        )
        assert (tmp_path / "stay.txt").exists()
        assert (tmp_path / "cross.txt").exists()
        assert not (tmp_path / "early-new.txt").exists()
        assert not (tmp_path / "stay-new.txt").exists()
        assert not (tmp_path / "cross-new.txt").exists()

    def test_5th_h2_preflight_passes_when_all_paths_share_dest_dev(self, tmp_path):
        # Sanity: when all source/dest paths are on the same filesystem (the
        # normal case), pre-flight is a no-op and apply_moves proceeds.
        (tmp_path / "a.txt").write_text("x")
        result = apply_moves(
            dir_moves=[],
            file_moves=[("a.txt", "b.txt")],
            dest_root=tmp_path,
        )
        assert result.files_moved == 1
        assert (tmp_path / "b.txt").read_text() == "x"

    def test_5th_partial_replay_recovers_idempotently_on_rerun(self, tmp_path):
        # Verifies the load-bearing self-healing claim from architectural
        # decision 9: if apply_moves was killed mid-flight on a previous
        # run, the dest now has SOME files at their new paths and the rest
        # at their old paths. The next run computes the same plan against
        # the unchanged source baseline; _move_no_clobber must skip the
        # already-moved files (target exists) and apply the rest, with no
        # error. Until this test, this behavior was claimed but unverified.
        for i in range(6):
            (tmp_path / f"old_{i}.txt").write_text(f"content {i}")
        # Simulate a prior killed run: pre-apply the first three moves.
        for i in range(3):
            (tmp_path / f"old_{i}.txt").rename(tmp_path / f"new_{i}.txt")
        # Sanity: dest is in mid-flight state.
        assert all((tmp_path / f"new_{i}.txt").exists() for i in range(3))
        assert all((tmp_path / f"old_{i}.txt").exists() for i in range(3, 6))

        result = apply_moves(
            dir_moves=[],
            file_moves=[(f"old_{i}.txt", f"new_{i}.txt") for i in range(6)],
            dest_root=tmp_path,
        )
        # First 3 already at destination → counted as skipped (target exists).
        assert result.skipped == 3, f"expected 3 already-moved skips, got {result}"
        # Remaining 3 still needed renaming → counted as moved.
        assert result.files_moved == 3, f"expected 3 fresh moves, got {result}"
        # Final state: all 6 at their new paths.
        for i in range(6):
            assert (tmp_path / f"new_{i}.txt").read_text() == f"content {i}"
            assert not (tmp_path / f"old_{i}.txt").exists()

    def test_missing_source_is_skipped_not_fatal(self, tmp_path):
        # If the dest tree is missing a path that the diff says was moved,
        # that means rsync hasn't put it there yet (first backup, or new file).
        # Replay should skip it gracefully.
        result = apply_moves(
            dir_moves=[],
            file_moves=[("never_existed.txt", "wont_exist.txt")],
            dest_root=tmp_path,
        )
        assert result.files_moved == 0
        assert result.skipped == 1
