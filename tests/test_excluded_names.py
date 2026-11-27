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
from irsync.destination import DestinationUnreadable, LocalDestination
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


def _listed(text: str) -> list[str]:
    """Return the preview's modified, created and deleted lines."""
    return [ln for ln in text.splitlines() if ln[:4] in ("  ~ ", "  + ", "  - ")]


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
        # the folder and its 9 parts) is 53%. A local refusal ends as it
        # always has: the sentence a remote destination adds (AD-37) is
        # not in it.
        parts = [f"{DL}/part{i}" for i in range(1, 10)]
        src, dest = _tree(tmp_path, *parts, "docs/real1", "docs/real2", "docs/real3")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        for rel in parts[:8]:
            (src / rel).unlink()

        rc, _diffs = _backup(src, dest, basic_options, caplog, no_exclude=True)

        assert rc == EXIT_REFUSED
        refusals = [m for m in caplog.messages if m.startswith("Refusing:")]
        assert len(refusals) == 1
        assert "would delete 8 of 15 previously-recorded entries (53%)" in refusals[0]
        assert refusals[0].endswith(
            "args were reversed. Pass --allow-massive-delete to override."
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

    def test_a_dry_run_asks_the_backup_too(
        self, tmp_path, basic_options, caplog, monkeypatch
    ):
        # Behavior (design section 5): --dry-run does the lookups, which
        # only read, so its preview is the one a real run would show.
        # Bug caught: skipping the lookups under --dry-run would leave the
        # hints unchecked: dropped, the moved file is listed as created;
        # kept on trust, a stale copy would be listed as a move. Either
        # way the dry run's preview is not the real run's.
        # Expected values: every row under the excluded name asked about
        # once; the diff the real run gives in the test above; the backup
        # and the baseline byte for byte as they were.
        src, dest = _tree(tmp_path, f"{DL}/ep.mkv", "finished/other.txt", "keep")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        (src / DL / "ep.mkv").rename(src / "finished" / "ep.mkv")
        backup_before = tree_signature(dest)
        baseline_before = (src / SNAPSHOT_FILENAME).read_bytes()
        looked: list[list[str]] = []
        real_look = LocalDestination.look

        def recording_look(self, paths):
            paths = list(paths)
            looked.append(sorted(paths))
            return real_look(self, paths)

        monkeypatch.setattr(LocalDestination, "look", recording_look)

        rc, diffs = _backup(
            src, dest, basic_options, caplog, no_exclude=True, dry_run=True
        )

        assert rc == 0
        assert looked == [[DL, f"{DL}/ep.mkv"]]
        assert diffs == [
            "Diff: 0 dir moves, 1 file moves, 0 modified, 0 created, 0 deleted."
        ]
        assert tree_signature(dest) == backup_before
        assert (src / SNAPSHOT_FILENAME).read_bytes() == baseline_before

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
        # been written while the renamed one still held that number. In
        # both cases nothing is left at the old path: the copy is renamed
        # away, or deleted by rsync as the preview lists it.
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
        assert not (dest / DL / "ep.mkv").exists()

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
        # the source") would now be false, and so would "no earlier run
        # recorded": the baseline holds only what the last run recorded.
        # Expected values: the note's text in the design's section 2, as
        # amended 2026-11-26.
        src, dest = _tree(tmp_path, f"{DL}/part", "keep")
        assert _backup(src, dest, basic_options, caplog)[0] == 0
        capsys.readouterr()

        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0

        lines = capsys.readouterr().out.splitlines()
        start = lines.index(NOTE_HEADER)
        assert lines[start + 1 : start + 4] == [
            "The lists below cover these names too, checked against the backup. They",
            "cannot show one thing: a file there that only the backup has and the last",
            "run did not record, which rsync DELETES. --dry-run shows every file.",
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

    def test_a_hard_link_out_of_an_excluded_folder_is_not_a_move(
        self, tmp_path, basic_options, caplog
    ):
        # Behavior (design section 5): a file given a second name in the
        # synced tree while keeping its first under the excluded name is
        # created there, and the backup's copy under the excluded name is
        # left where it is, since the source still has that path.
        # Bug caught: counting the inode's paths only in the synced part of
        # the source listed "File moves (1)", and the replay renamed the
        # backup's copy out of the excluded folder (the final review's C1).
        # Expected values: hand count, as on main. Created: finished/ep.mkv.
        # The copy keeps its inode and its place, which no rename would.
        src, dest = _tree(tmp_path, f"{DL}/ep.mkv", "finished/other.txt", "keep")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        copy_inode = inode_for(dest / DL / "ep.mkv")
        os.link(src / DL / "ep.mkv", src / "finished" / "ep.mkv")

        rc, diffs = _backup(src, dest, basic_options, caplog)

        assert rc == 0
        assert diffs == [
            "Diff: 0 dir moves, 0 file moves, 0 modified, 1 created, 0 deleted."
        ]
        assert inode_for(dest / DL / "ep.mkv") == copy_inode
        assert (dest / DL / "ep.mkv").read_text() == f"{DL}/ep.mkv"
        assert (dest / "finished" / "ep.mkv").read_text() == f"{DL}/ep.mkv"

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


@pytest.mark.parametrize("no_exclude", [False, True], ids=["excludes", "no-exclude"])
class TestEitherKindOfLocalRun:
    """What a run with the excludes and a ``--no-exclude`` run share."""

    def test_the_guard_counts_only_rows_the_backup_holds(
        self, tmp_path, basic_options, caplog, no_exclude
    ):
        # Behavior (design section 1): the >50% guard counts deletions
        # against a "before" that holds only rows the backup has.
        # Bug caught: dividing by the whole baseline let six partial
        # downloads the backup never received dilute the guard to 3 of 12
        # (25%), and rsync deleted every real file in docs unrefused.
        # Expected values: hand count. The backup holds the root, docs and
        # its 3 files (5 rows); deleting the 3 files is 3 of 5, 60%. The
        # message is the local refusal, word for word.
        parts = [f"{DL}/part{i}" for i in range(1, 7)]
        docs = ["docs/real1", "docs/real2", "docs/real3"]
        src, dest = _tree(tmp_path, *parts, *docs)
        assert _backup(src, dest, basic_options, caplog)[0] == 0
        assert not (dest / DL).exists()
        for rel in docs:
            (src / rel).unlink()

        rc, _diffs = _backup(src, dest, basic_options, caplog, no_exclude=no_exclude)

        assert rc == EXIT_REFUSED
        assert [m for m in caplog.messages if m.startswith("Refusing:")] == [
            "Refusing: diff would delete 3 of 5 previously-recorded entries (60%). "
            "This usually means the source tree has been swapped, the snapshot is "
            "from a different tree, or args were reversed. Pass "
            "--allow-massive-delete to override."
        ]
        assert sorted(p.name for p in (dest / "docs").iterdir()) == [
            "real1",
            "real2",
            "real3",
        ]

    def test_a_lookup_error_refuses_the_run_before_anything_is_written(
        self, tmp_path, basic_options, caplog, monkeypatch, no_exclude
    ):
        # Behavior (design section 5): when the backup cannot be read, the
        # run is refused with exit 2 before the replay or rsync, whatever
        # the uid.
        # Bug caught: catching the error on one kind of run only, or
        # reading it as "nothing there", would list the moved file as
        # created and hand the run to rsync. The chmod test above skips
        # under root and covers only --no-exclude.
        # Expected values: the paths each kind of run asks about (every
        # row under the excluded name, or the one file moved out); the
        # refusal naming the path and the reason the stand-in raises;
        # rsync never started; the backup and the baseline byte for byte
        # as they were.
        src, dest = _tree(tmp_path, f"{DL}/ep.mkv", "finished/other.txt", "keep")
        assert _backup(src, dest, basic_options, caplog, no_exclude=True)[0] == 0
        (src / DL / "ep.mkv").rename(src / "finished" / "ep.mkv")
        backup_before = tree_signature(dest)
        baseline_before = (src / SNAPSHOT_FILENAME).read_bytes()
        unreadable = str(dest / DL / "ep.mkv")
        looked: list[list[str]] = []

        def failing_look(self, paths):
            looked.append(sorted(paths))
            raise DestinationUnreadable(unreadable, "Input/output error")

        monkeypatch.setattr(LocalDestination, "look", failing_look)
        rsync_runs: list[list[str]] = []
        monkeypatch.setattr(
            "irsync.backup.run_real_sync", lambda cmd: rsync_runs.append(cmd) or 0
        )

        rc, _diffs = _backup(src, dest, basic_options, caplog, no_exclude=no_exclude)

        assert rc == EXIT_REFUSED
        assert looked == [[DL, f"{DL}/ep.mkv"] if no_exclude else [f"{DL}/ep.mkv"]]
        assert [m for m in caplog.messages if m.startswith("Refusing:")] == [
            f"Refusing: cannot read the backup at {unreadable} (Input/output "
            "error). irsync checks what the backup holds there before it lists "
            "any change; nothing has been written to it."
        ]
        assert rsync_runs == []
        assert tree_signature(dest) == backup_before
        assert (src / SNAPSHOT_FILENAME).read_bytes() == baseline_before
