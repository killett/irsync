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
