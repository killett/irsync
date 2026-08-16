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


def _isolated_env(tmp_path, **overrides):
    """Return an env whose config discovery can only fail: no file anywhere.

    `HOME` and `XDG_CONFIG_HOME` are pointed at directories that do not exist
    and `DRIVECFG_CONFIG` is removed, so every discovery step misses.
    """
    env = {
        **ENV,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "empty"),
    }
    env.pop("DRIVECFG_CONFIG", None)
    env.update(overrides)
    return env


def _run_irsync(argv, env, cwd=None):
    """Run `python -m irsync` for real, so refusals are checked on real stderr."""
    return subprocess.run(
        [sys.executable, "-m", "irsync", *argv],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=cwd,
    )


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

    def test_tilde_with_a_destination_works_without_a_config(self, tmp_path):
        # `irsync '~' /backup` is a two-argument plain-path run: '~' is
        # expanded by Path.expanduser and the config is never consulted.
        # Catches the lazy-load gate demanding a config it then ignores.
        home = tmp_path / "home"
        home.mkdir()
        (home / "file.txt").write_text("hello", encoding="utf-8")
        dest = tmp_path / "backup"
        dest.mkdir()
        result = _run_irsync(
            ["~", str(dest), "--yes", "--no-snapshot", "--dry-run"],
            _isolated_env(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        assert str(home) in result.stderr, (
            "'~' must expand to the home directory, not to a configured endpoint"
        )

    def test_bare_name_snapshot_only_works_without_a_config(self, tmp_path):
        # `cd /mnt && irsync data --snapshot-only`: a bare relative directory
        # name with no destination. Catches the name-shaped-token rule turning
        # a working plain-path snapshot into a refusal.
        from irsync.snapshot import SNAPSHOT_FILENAME

        source = tmp_path / "data"
        source.mkdir()
        (source / "file.txt").write_text("hello", encoding="utf-8")
        result = _run_irsync(
            ["data", "--snapshot-only", "--yes"],
            _isolated_env(tmp_path),
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert (source / SNAPSHOT_FILENAME).is_file()

    def test_snapshot_only_tolerates_a_config_that_was_never_configured(
        self, tmp_path, monkeypatch
    ):
        # Discovery coming up empty is the normal state for someone who has
        # never written a drives.toml. Under --snapshot-only that must not be
        # a refusal — the source is simply taken as a literal directory.
        from irsync.snapshot import SNAPSHOT_FILENAME

        self._isolate(monkeypatch, tmp_path)
        source = tmp_path / "data"
        source.mkdir()
        (source / "file.txt").write_text("hello", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert main(["data", "--snapshot-only", "--yes"]) == 0
        assert (source / SNAPSHOT_FILENAME).is_file()

    def test_snapshot_only_still_refuses_an_explicit_config_that_is_missing(
        self, tmp_path, monkeypatch
    ):
        # The tolerance above is for a config nobody asked for. A config named
        # explicitly through $DRIVECFG_CONFIG that is not there is a typo, and
        # carrying on would snapshot from a layout the user did not name.
        missing = tmp_path / "typo.toml"
        monkeypatch.setenv("DRIVECFG_CONFIG", str(missing))
        source = tmp_path / "data"
        source.mkdir()
        (source / "file.txt").write_text("hello", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert main(["data", "--snapshot-only", "--yes"]) == 2

    def test_shorthand_without_config_exits_two(self, tmp_path):
        # Catches a missing config surfacing as a traceback rather than a
        # refusal that reaches the terminal and says where to put the file.
        result = _run_irsync(["X"], _isolated_env(tmp_path))
        assert result.returncode == 2
        assert "Traceback" not in result.stderr
        assert "drives.toml" in result.stderr
        assert "DRIVECFG_CONFIG" in result.stderr
        assert str(tmp_path / "empty" / "drivecfg" / "drives.toml") in result.stderr

    def test_all_without_config_exits_two(self, tmp_path):
        # ALL has no meaning without a backup order; it must refuse rather
        # than quietly back up nothing and report success.
        result = _run_irsync(["ALL"], _isolated_env(tmp_path))
        assert result.returncode == 2
        assert "drives.toml" in result.stderr

    def test_named_endpoint_without_config_exits_two(self, tmp_path):
        result = _run_irsync(["mypython"], _isolated_env(tmp_path))
        assert result.returncode == 2
        assert "drives.toml" in result.stderr


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

    def test_snapshot_only_still_prefers_a_configured_name(self, tmp_path):
        # The plain-path fallback must not swallow configured names: with a
        # config in play, `irsync mypython --snapshot-only` still snapshots the
        # configured source, not a directory of that name in the cwd.
        from irsync.snapshot import SNAPSHOT_FILENAME

        from .conftest import write_drive_config

        config = write_drive_config(tmp_path)
        code_dir = tmp_path / "media" / "A" / "code"
        (code_dir / "payload.bin").write_bytes(b"x" * 3)
        decoy = tmp_path / "mypython"
        decoy.mkdir()

        result = _run_irsync(
            ["mypython", "--snapshot-only", "--yes", "--allow-unmounted"],
            _isolated_env(tmp_path, DRIVECFG_CONFIG=str(config)),
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert (code_dir / SNAPSHOT_FILENAME).is_file()
        assert not (decoy / SNAPSHOT_FILENAME).exists(), (
            "a same-named directory in the cwd must not win over the config"
        )

    def test_endpoint_source_path_resolves_through_main(self, tmp_path):
        # Pre-migration, naming an endpoint's own source directory (rather
        # than its short name) resolved to that endpoint's backup. Catches the
        # alias being unreachable from the CLI because the gate never loads a
        # config for a path-shaped source.
        from .conftest import write_drive_config

        config = write_drive_config(tmp_path)
        home = tmp_path / "home"
        (home / "file.txt").write_text("hello", encoding="utf-8")
        home_backup = tmp_path / "media" / "D" / "home_backup"

        result = _run_irsync(
            [str(home), "--yes", "--no-snapshot", "--dry-run", "--allow-unmounted"],
            _isolated_env(tmp_path, DRIVECFG_CONFIG=str(config)),
        )
        assert result.returncode == 0, result.stderr
        assert str(home_backup) in result.stderr, (
            "the destination must come from the endpoint whose source was named"
        )

    def test_unconfigured_letter_exits_two_naming_the_configured_drives(self, tmp_path):
        # Intentional behaviour change: an unconfigured letter used to resolve
        # to /media/$USER/<letter>. It must now refuse on the terminal, and say
        # what IS configured.
        from .conftest import write_drive_config

        config = write_drive_config(tmp_path)
        result = _run_irsync(
            ["Z", "--yes", "--config", str(config)], _isolated_env(tmp_path)
        )
        assert result.returncode == 2
        assert "Traceback" not in result.stderr
        assert "Configured drives: A, B, C, D" in result.stderr

    def test_broken_config_path_exits_two_without_a_traceback(self, tmp_path):
        missing = tmp_path / "absent.toml"
        result = _run_irsync(["X", "--config", str(missing)], _isolated_env(tmp_path))
        assert result.returncode == 2
        assert "Traceback" not in result.stderr
        assert str(missing) in result.stderr

    def test_malformed_config_refuses_even_under_snapshot_only(self, tmp_path):
        # --snapshot-only tolerates an ABSENT config (it falls back to the
        # literal path), but a config that exists and is broken must still be
        # reported — silently ignoring it is how a run ends up elsewhere.
        source = tmp_path / "data"
        source.mkdir()
        (source / "file.txt").write_text("hello", encoding="utf-8")
        broken = tmp_path / "drives.toml"
        broken.write_text("schema_version = 1\nbase_dir = \n", encoding="utf-8")

        result = _run_irsync(
            ["data", "--snapshot-only", "--yes"],
            _isolated_env(tmp_path, DRIVECFG_CONFIG=str(broken)),
            cwd=tmp_path,
        )
        assert result.returncode == 2
        assert "Traceback" not in result.stderr
        assert str(broken) in result.stderr

    def test_empty_config_env_var_refuses_instead_of_falling_back(self, tmp_path):
        # A set-but-empty DRIVECFG_CONFIG is what a shell template that
        # interpolates an unset variable produces. drivecfg deliberately
        # refuses it rather than sliding down to XDG. irsync's "was a config
        # named explicitly?" test used to strip the value before asking, so
        # it treated the refusal as "no config configured" and snapshotted a
        # literal ./mypython in the cwd — exit 0, wrong tree, no warning.
        from irsync.snapshot import SNAPSHOT_FILENAME

        decoy = tmp_path / "mypython"
        decoy.mkdir()
        (decoy / "file.txt").write_text("hello", encoding="utf-8")

        result = _run_irsync(
            ["mypython", "--snapshot-only", "--yes"],
            _isolated_env(tmp_path, DRIVECFG_CONFIG=""),
            cwd=tmp_path,
        )
        assert result.returncode == 2, result.stdout
        assert "Traceback" not in result.stderr
        assert "DRIVECFG_CONFIG" in result.stderr
        assert not (decoy / SNAPSHOT_FILENAME).exists(), (
            "a refused config must not fall through to a literal directory"
        )


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
