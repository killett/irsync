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
