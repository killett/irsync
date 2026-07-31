"""`python -m irsync` must exit with the code `cli.main` returns."""

import fcntl
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

import irsync
from irsync.snapshot import LOCKFILE_NAME

# The subprocess doesn't inherit pytest's pythonpath setting, so point it at
# the same source tree this test process imported irsync from.
_SRC_DIR = str(Path(irsync.__file__).resolve().parent.parent)


def _run_module_main():
    """Execute src/irsync/__main__.py the way `python -m irsync` does."""
    runpy.run_module("irsync", run_name="__main__")


class TestExitCodePropagation:
    def test_nonzero_return_becomes_the_process_exit_code(self, monkeypatch):
        # 75 (EX_TEMPFAIL) is what run_backup returns when another run holds
        # the lock — the case a cron wrapper most needs to see.
        monkeypatch.setattr("irsync.cli.main", lambda argv=None: 75)

        with pytest.raises(SystemExit) as excinfo:
            _run_module_main()

        assert excinfo.value.code == 75

    def test_zero_return_stays_a_success_exit(self, monkeypatch):
        monkeypatch.setattr("irsync.cli.main", lambda argv=None: 0)

        with pytest.raises(SystemExit) as excinfo:
            _run_module_main()

        assert excinfo.value.code == 0


class TestExitCodePropagationEndToEnd:
    def test_lock_conflict_exits_75_through_python_dash_m(self, tmp_path):
        """No stubbing: a real held lock must surface as a real exit code 75."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.bin").write_bytes(b"x")
        dest = tmp_path / "dest"
        dest.mkdir()

        lock = (src / LOCKFILE_NAME).open("w")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            env = {**os.environ, "PYTHONPATH": _SRC_DIR}
            proc = subprocess.run(
                [sys.executable, "-m", "irsync", str(src), str(dest), "--yes"],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
        finally:
            lock.close()

        assert proc.returncode == 75, (
            f"expected EX_TEMPFAIL from a held lock, got {proc.returncode}. "
            f"stderr:\n{proc.stderr}"
        )
