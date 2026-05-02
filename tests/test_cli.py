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
