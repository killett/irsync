import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}


def test_version_prints_and_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "irsync", "--version"],
        capture_output=True,
        text=True,
        check=True,
        env=ENV,
    )
    assert "0.1.0" in (result.stdout + result.stderr)


def test_help_runs():
    result = subprocess.run(
        [sys.executable, "-m", "irsync", "--help"],
        capture_output=True,
        text=True,
        check=True,
        env=ENV,
    )
    assert "rename-aware" in result.stdout or "Intelligent rsync" in result.stdout


def test_no_args_shows_help():
    result = subprocess.run(
        [sys.executable, "-m", "irsync"],
        capture_output=True,
        text=True,
        check=False,
        env=ENV,
    )
    assert "usage:" in (result.stdout + result.stderr).lower()


def test_5th_m2_force_with_no_snapshot_rejected_at_parse_time(tmp_path):
    # NEW-M2 (5th-pass): --force only does anything inside the snapshot diff
    # branch (the "no changes" short-circuit and the 50% deletion threshold).
    # With --no-snapshot, both checks are bypassed and --force is silently
    # inert — a usability footgun. Reject the combination at parse time so
    # the user gets an immediate, clear error rather than running with one
    # of their flags ignored.
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "irsync",
            str(src),
            str(dest),
            "--no-snapshot",
            "--force",
            "--yes",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=ENV,
    )
    assert result.returncode != 0, (
        "--force with --no-snapshot must be rejected at parse time"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "--force" in combined and "--no-snapshot" in combined, (
        f"error message should mention both flags; got: {combined!r}"
    )


def test_missing_source_path_exits_cleanly(tmp_path):
    # Reproduced against the old code: FileNotFoundError escaped cli.main as
    # a traceback and the exit code was flattened to 1.
    dest = tmp_path / "dest"
    dest.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "irsync",
            str(tmp_path / "definitely_missing"),
            str(dest),
            "--yes",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=ENV,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_unreadable_destination_exits_cleanly(tmp_path, make_tree):
    # Carried forward from Task 3: UnsafeDestination (raised by
    # foreign_dest_entries when the destination cannot be read) was left
    # uncaught, so an unreadable destination tracebacked with exit 1. This
    # is the single-drive path; run_all_backups already has its own
    # UnsafeDestination handling for ALL runs (untouched by this task).
    src = tmp_path / "src"
    make_tree(src, num_files=3, depth=1)
    dest = tmp_path / "dest"
    dest.mkdir()
    dest.chmod(0o000)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "irsync", str(src), str(dest), "--yes"],
            capture_output=True,
            text=True,
            check=False,
            env=ENV,
        )
    finally:
        dest.chmod(0o755)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_missing_rsync_binary_exits_cleanly(tmp_path):
    # Reproduced against the old code: FileNotFoundError from subprocess
    # escaped run_dry_run/run_real_sync as a stack trace, exit code 1.
    # sys.executable is an absolute path, so python itself still launches
    # with PATH pointed at a directory that has no rsync on it.
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    env = {**ENV, "PATH": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-m", "irsync", str(src), str(dest), "--yes"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "rsync" in (result.stdout + result.stderr).lower()


def test_identical_source_and_destination_exits_cleanly(tmp_path):
    # Reviewer Finding 1: options.resolve_endpoints raises ValueError for six
    # user-facing invocation errors (missing source/dest, remote-to-remote,
    # identical paths, source-inside-dest, dest-inside-source). This is
    # cli.main's ValueError catch, exercised end-to-end: source == dest is
    # the cleanest of the six to set up as a real CLI invocation.
    same = tmp_path / "same"
    same.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "irsync", str(same), str(same), "--yes"],
        capture_output=True,
        text=True,
        check=False,
        env=ENV,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_missing_destination_for_plain_path_source_exits_cleanly(tmp_path):
    # Second of the six ValueError refusals: an ordinary path (not a drive
    # letter, '~', or 'mypython') requires an explicit destination.
    # resolve_endpoints raises ValueError; cli.main's boundary must turn
    # that into a clean exit 2 rather than a traceback.
    src = tmp_path / "src"
    src.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "irsync", str(src), "--yes"],
        capture_output=True,
        text=True,
        check=False,
        env=ENV,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
