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
