import errno
import os
from unittest.mock import patch

import pytest

from irsync.replay import ReplayResult, apply_moves


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
