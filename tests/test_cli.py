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
