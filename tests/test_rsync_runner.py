import io

import pytest

from irsync.rsync_runner import build_rsync_command, parse_rsync_output, run_real_sync


class TestBuildRsyncCommand:
    def test_canonical_flags_present(self):
        cmd = build_rsync_command(source="/src/", dest="/dst/", dry_run=False)
        for flag in (
            "--sparse",
            "-a",
            "-h",
            "-P",
            "-i",
            "--stats",
            "--one-file-system",
            "--delete-before",
        ):
            assert flag in cmd

    def test_endpoints_at_end(self):
        cmd = build_rsync_command(source="/src/", dest="/dst/", dry_run=False)
        assert cmd[-2] == "/src/"
        assert cmd[-1] == "/dst/"

    def test_dry_run_flag(self):
        cmd = build_rsync_command(source="/src/", dest="/dst/", dry_run=True)
        assert "--dry-run" in cmd

    def test_no_dry_run_by_default(self):
        cmd = build_rsync_command(source="/src/", dest="/dst/", dry_run=False)
        assert "--dry-run" not in cmd

    def test_excludes_snapshot_file_anchored_to_root(self):
        # The exclude pattern MUST be anchored with a leading slash so that
        # a user file named .irsync_snapshot.jsonl in a subdirectory still
        # gets backed up. Without the anchor, rsync matches the basename
        # at every level.
        cmd = build_rsync_command(source="/src/", dest="/dst/", dry_run=False)
        excluded = [cmd[j + 1] for j, x in enumerate(cmd[:-1]) if x == "--exclude"]
        assert "/.irsync_snapshot.jsonl" in excluded, (
            f"snapshot exclude must be anchored to root; got {excluded!r}"
        )
        assert ".irsync_snapshot.jsonl" not in excluded, (
            "unanchored .irsync_snapshot.jsonl exclude would skip subdir files of that name"
        )

    def test_excludes_lockfile_anchored_to_root(self):
        # NEW-H2: the lockfile is created by irsync at the source root and
        # must not be transferred to the dest tree. Anchored so a user file
        # at <src>/sub/.irsync.lock still gets backed up.
        cmd = build_rsync_command(source="/src/", dest="/dst/", dry_run=False)
        excluded = [cmd[j + 1] for j, x in enumerate(cmd[:-1]) if x == "--exclude"]
        assert "/.irsync.lock" in excluded, (
            f"lockfile exclude must be anchored to root; got {excluded!r}"
        )

    def test_excludes_snapshot_tempfile_pattern(self):
        # NEW-M1: orphan .irsync-snap-* tempfiles from a killed _atomic_write_snapshot
        # must not be transferred. Anchored to the source root.
        cmd = build_rsync_command(source="/src/", dest="/dst/", dry_run=False)
        excluded = [cmd[j + 1] for j, x in enumerate(cmd[:-1]) if x == "--exclude"]
        assert "/.irsync-snap-*" in excluded, (
            f"snap-tempfile exclude must be anchored to root; got {excluded!r}"
        )

    def test_user_excludes_added(self):
        cmd = build_rsync_command(
            source="/src/",
            dest="/dst/",
            dry_run=False,
            exclude_dirs=[".cache", "tmp"],
        )
        # Each user-supplied dir paired with --exclude
        for ex in (".cache", "tmp"):
            assert ex in cmd

    def test_ssh_port_and_key_for_remote(self):
        cmd = build_rsync_command(
            source="user@host:/src/",
            dest="/dst/",
            dry_run=False,
            ssh_port=2222,
            ssh_key="/home/u/.ssh/id_ed25519",
        )
        assert "-e" in cmd
        i = cmd.index("-e")
        ssh_cmd = cmd[i + 1]
        assert "ssh" in ssh_cmd
        assert "-p 2222" in ssh_cmd
        assert "/home/u/.ssh/id_ed25519" in ssh_cmd

    def test_no_ssh_options_for_local(self):
        cmd = build_rsync_command(source="/src/", dest="/dst/", dry_run=False)
        assert "-e" not in cmd


class TestRunRealSyncIsolation:
    def test_7th_h2_starts_child_in_new_session(self, monkeypatch):
        # 7th-NEW-H2: without start_new_session=True, an irsync parent
        # killed by SIGKILL leaves the rsync child running in the parent's
        # process group. The kernel releases the lockfile fd on parent
        # death, so the very next irsync invocation can acquire the lock
        # and start a SECOND rsync that races the orphan on the same dest
        # — overlapping writes and --delete-before sweeps. Putting the
        # child in its own session/pgrp also detaches it from the
        # controlling tty so a Ctrl+C delivered via the terminal doesn't
        # double-deliver to both processes.
        captured: dict[str, object] = {}

        class _FakeProc:
            stderr = io.StringIO("")
            returncode = 0

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        def _fake_popen(cmd, **kwargs):
            captured.update(kwargs)
            return _FakeProc()

        monkeypatch.setattr("irsync.rsync_runner.subprocess.Popen", _fake_popen)
        rc = run_real_sync(["rsync", "/a/", "/b/"])
        assert rc == 0
        assert captured.get("start_new_session") is True, (
            "rsync child must run in its own session/pgrp so a parent "
            "SIGKILL doesn't leave the child as an orphan racing future runs"
        )

    def test_7th_h2_keyboardinterrupt_during_stream_terminates_child(self, monkeypatch):
        # 7th-NEW-H2: when the stderr-streaming loop is interrupted (Ctrl+C
        # → KeyboardInterrupt, or any other BaseException), the rsync
        # child must be terminated before the exception propagates.
        # Otherwise the child keeps writing to dest while the parent's
        # cleanup runs, and the next irsync run can race it.
        events: dict[str, bool] = {"terminated": False, "killed": False}

        class _RaisingStderr:
            closed = False

            def __iter__(self):
                return self

            def __next__(self):
                raise KeyboardInterrupt

            def close(self):
                self.closed = True

        class _FakeProc:
            stderr = _RaisingStderr()
            returncode = -2

            def wait(self, timeout=None):
                return -2

            def terminate(self):
                events["terminated"] = True

            def kill(self):
                events["killed"] = True

        monkeypatch.setattr(
            "irsync.rsync_runner.subprocess.Popen", lambda *a, **k: _FakeProc()
        )
        with pytest.raises(KeyboardInterrupt):
            run_real_sync(["rsync", "/a/", "/b/"])
        assert events["terminated"], (
            "rsync child must be terminate()'d when the stderr loop is "
            "interrupted, so it can't continue writing to dest after the "
            "parent has aborted"
        )


class TestParseRsyncOutput:
    def test_extracts_count_and_size(self):
        sample = "Number of files: 1,234\ntotal size is 567,890,123 bytes\n"
        n, sz = parse_rsync_output(sample)
        assert n == 1234
        assert sz is not None and "567" in sz

    def test_returns_none_when_absent(self):
        n, sz = parse_rsync_output("nothing useful here")
        assert n is None
        assert sz is None
