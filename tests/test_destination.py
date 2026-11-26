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
