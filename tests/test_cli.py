import logging
import os
import subprocess
import sys
from pathlib import Path

from irsync.cli import main

from .conftest import subprocess_pythonpath

ROOT = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "PYTHONPATH": subprocess_pythonpath()}


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
    # branch (the "no changes" short-circuit). With --no-snapshot, that check
    # is bypassed and --force is silently inert — a usability footgun.
    # Reject the combination at parse time so the user gets an immediate,
    # clear error rather than running with one of their flags ignored.
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


def test_allow_massive_delete_with_no_snapshot_rejected(tmp_path):
    # NEW-M2 (5th-pass): --allow-massive-delete only does anything inside the
    # snapshot diff branch (the 50% deletion threshold). With --no-snapshot,
    # that check is bypassed and --allow-massive-delete is silently inert — a
    # usability footgun. Reject the combination at parse time so the user
    # gets an immediate, clear error rather than running with one of their
    # flags ignored.
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
            "--allow-massive-delete",
            "--yes",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=ENV,
    )
    assert result.returncode != 0, (
        "--allow-massive-delete with --no-snapshot must be rejected at parse time"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "--allow-massive-delete" in combined and "--no-snapshot" in combined, (
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


class TestNoConfigBehaviour:
    """Nothing about a plain-path run may depend on a drive config existing."""

    @staticmethod
    def _isolate(monkeypatch, tmp_path):
        """Point discovery at an empty config home, so no config is findable."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("DRIVECFG_CONFIG", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

    def test_plain_paths_work_without_any_config(self, tmp_path, monkeypatch):
        # THE compatibility test: catches eager config loading, which would
        # break every installation that has no drives.toml at all.
        self._isolate(monkeypatch, tmp_path)
        source = tmp_path / "src"
        dest = tmp_path / "dst"
        source.mkdir()
        dest.mkdir()
        (source / "file.txt").write_text("hello", encoding="utf-8")
        assert (
            main([str(source), str(dest), "--yes", "--no-snapshot", "--dry-run"]) == 0
        )

    def test_plain_path_run_never_touches_discovery(self, tmp_path, monkeypatch):
        # Stronger than the exit code above: a config that would REFUSE (an
        # explicit DRIVECFG_CONFIG pointing at nothing) must not even be
        # consulted for a plain-path run.
        monkeypatch.setenv("DRIVECFG_CONFIG", str(tmp_path / "nope.toml"))
        source = tmp_path / "src"
        dest = tmp_path / "dst"
        source.mkdir()
        dest.mkdir()
        (source / "file.txt").write_text("hello", encoding="utf-8")
        assert (
            main([str(source), str(dest), "--yes", "--no-snapshot", "--dry-run"]) == 0
        )

    def test_help_does_not_load_a_config(self, tmp_path, monkeypatch, capsys):
        # Rendering help must not require (or refuse over) a config file.
        monkeypatch.setenv("DRIVECFG_CONFIG", str(tmp_path / "nope.toml"))
        assert main([]) == 0
        assert "usage:" in capsys.readouterr().out

    def test_help_text_names_no_private_paths(self, tmp_path, monkeypatch, capsys):
        # The parser used to interpolate one machine's backup directories into
        # --help. Catches any such value coming back.
        monkeypatch.setenv("DRIVECFG_CONFIG", str(tmp_path / "nope.toml"))
        main([])
        out = capsys.readouterr().out
        assert "homedir_backup" not in out
        assert "/media/" not in out
        assert "--config" in out

    def test_shorthand_without_config_exits_two(self, tmp_path, monkeypatch, capsys):
        # Catches a missing config surfacing as a traceback rather than a
        # refusal that tells the user where to put the file.
        self._isolate(monkeypatch, tmp_path)
        assert main(["G"]) == 2
        err = capsys.readouterr().err
        assert "drives.toml" in err
        assert "DRIVECFG_CONFIG" in err
        assert str(tmp_path / "empty" / "drivecfg" / "drives.toml") in err

    def test_all_without_config_exits_two(self, tmp_path, monkeypatch, capsys):
        # ALL has no meaning without a backup order; it must refuse rather
        # than quietly back up nothing and report success.
        self._isolate(monkeypatch, tmp_path)
        assert main(["ALL"]) == 2
        assert "drives.toml" in capsys.readouterr().err

    def test_named_endpoint_without_config_exits_two(
        self, tmp_path, monkeypatch, capsys
    ):
        self._isolate(monkeypatch, tmp_path)
        assert main(["mypython"]) == 2
        assert "drives.toml" in capsys.readouterr().err


class TestConfigFlag:
    def test_config_flag_overrides_discovery(self, tmp_path, monkeypatch):
        # --config must win over an unusable discovered location, and the
        # drive must resolve to the directory THAT file names.
        from irsync.snapshot import SNAPSHOT_FILENAME

        from .conftest import write_drive_config

        monkeypatch.setenv("DRIVECFG_CONFIG", str(tmp_path / "never-read.toml"))
        config = write_drive_config(tmp_path)
        drive_dir = tmp_path / "media" / "B"
        drive_dir.mkdir()
        (drive_dir / "payload.bin").write_bytes(b"x" * 3)

        # --allow-unmounted: the config's base_dir is a tmp directory, so the
        # mount gate (which is what base_dir is still for) would refuse first.
        argv = ["B", "--snapshot-only", "--yes", "--allow-unmounted"]
        assert main([*argv, "--config", str(config)]) == 0
        assert (drive_dir / SNAPSHOT_FILENAME).is_file(), (
            "the snapshot must land in the drive directory the config names"
        )

    def test_all_backs_up_every_entry_in_the_configured_order(
        self, tmp_path, monkeypatch
    ):
        # 'ALL' must follow the config's backup_order verbatim: no entry
        # filtered out, no order of its own, nothing appended.
        from .conftest import write_drive_config

        monkeypatch.delenv("DRIVECFG_CONFIG", raising=False)
        config = write_drive_config(tmp_path, backup_order=["C", "mypython", "B"])
        attempted = []

        def fake_backup(*, source_arg, destination_arg, options, args):
            attempted.append(source_arg)
            return 0

        monkeypatch.setattr("irsync.backup.run_backup", fake_backup)

        assert main(["ALL", "--yes", "--config", str(config)]) == 0
        assert attempted == ["C", "mypython", "B"]

    def test_unconfigured_letter_exits_two_naming_the_configured_drives(
        self, tmp_path, monkeypatch, caplog
    ):
        # Intentional behaviour change: an unconfigured letter used to resolve
        # to /media/$USER/<letter>. It must now refuse, and say what IS
        # configured.
        from .conftest import write_drive_config

        monkeypatch.delenv("DRIVECFG_CONFIG", raising=False)
        config = write_drive_config(tmp_path)
        with caplog.at_level(logging.ERROR):
            assert main(["Z", "--yes", "--config", str(config)]) == 2
        assert "Configured drives: A, B, C, D" in caplog.text

    def test_broken_config_path_exits_two_without_a_traceback(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.delenv("DRIVECFG_CONFIG", raising=False)
        missing = tmp_path / "absent.toml"
        assert main(["G", "--config", str(missing)]) == 2
        assert str(missing) in capsys.readouterr().err


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
