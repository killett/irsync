import io

import pytest

from irsync.rsync_runner import build_rsync_command, run_real_sync


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

    def test_8th_h3_run_real_sync_installs_sigterm_handler_during_run(
        self, monkeypatch
    ):
        # 8th-NEW-H3: Python's default SIGTERM handler kills the process
        # without raising, so the new try/except BaseException cleanup
        # never runs and rsync orphans on cron timeout / systemctl stop —
        # exactly the case 7th-NEW-H2's start_new_session was supposed to
        # mitigate. Install a SIGTERM handler in run_real_sync so the
        # signal reaches the cleanup path. Restore the original handler
        # on exit so this doesn't leak global state into other tests.
        import signal as _signal

        signal_calls: list[tuple] = []
        original_signal = _signal.signal

        def _record_signal(signum, handler):
            signal_calls.append((signum, handler))
            return original_signal(signum, handler)

        monkeypatch.setattr("irsync.rsync_runner.signal.signal", _record_signal)

        class _FakeProc:
            stderr = io.StringIO("")
            returncode = 0
            pid = 1234

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        monkeypatch.setattr(
            "irsync.rsync_runner.subprocess.Popen", lambda *a, **k: _FakeProc()
        )

        run_real_sync(["rsync", "/a/", "/b/"])

        sigterm_sets = [c for c in signal_calls if c[0] == _signal.SIGTERM]
        assert len(sigterm_sets) >= 2, (
            f"expected SIGTERM to be installed then restored; "
            f"got {len(sigterm_sets)} signal.signal(SIGTERM, ...) calls: "
            f"{sigterm_sets!r}"
        )
        # The first call installs our handler (callable, not the default).
        installed_handler = sigterm_sets[0][1]
        assert callable(installed_handler), (
            f"installed SIGTERM handler must be callable; got {installed_handler!r}"
        )
        # The last call restores something (could be the previous handler,
        # SIG_DFL, SIG_IGN, or None — the exact value depends on the env).
        # We just want to confirm restoration happened.
        assert sigterm_sets[-1][1] != installed_handler, (
            "SIGTERM handler must be restored on run_real_sync exit; "
            "the last signal.signal call should not still be our handler"
        )

    def test_8th_h3_sigterm_handler_raises_keyboardinterrupt(self, monkeypatch):
        # The installed SIGTERM handler must raise so the BaseException
        # cleanup fires. KeyboardInterrupt is the natural choice — already
        # caught by the existing except BaseException block.
        import signal as _signal

        captured: dict[str, object] = {}

        def _capture(signum, handler):
            if signum == _signal.SIGTERM and callable(handler):
                captured.setdefault("handler", handler)
            return _signal.SIG_DFL

        monkeypatch.setattr("irsync.rsync_runner.signal.signal", _capture)

        class _FakeProc:
            stderr = io.StringIO("")
            returncode = 0
            pid = 5555

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        monkeypatch.setattr(
            "irsync.rsync_runner.subprocess.Popen", lambda *a, **k: _FakeProc()
        )

        run_real_sync(["rsync", "/a/", "/b/"])

        handler = captured.get("handler")
        assert handler is not None, "SIGTERM handler was never installed"
        with pytest.raises(KeyboardInterrupt):
            handler(_signal.SIGTERM, None)

    def test_8th_h2_cancel_sends_sigterm_to_process_group_not_just_leader(
        self, monkeypatch
    ):
        # 8th-NEW-H2: with start_new_session=True, the rsync child is the
        # leader of its own process group. proc.terminate() targets only
        # the leader — when rsync has spawned ssh for a remote endpoint,
        # ssh inherits the new pgrp but doesn't get the signal and keeps
        # writing to dest. The cleanup must signal the whole pgrp via
        # os.killpg so the ssh child dies with rsync.
        import signal as _signal

        events: dict[str, object] = {"killpg_calls": []}

        class _RaisingStderr:
            def __iter__(self):
                return self

            def __next__(self):
                raise KeyboardInterrupt

            def close(self):
                pass

        class _FakeProc:
            stderr = _RaisingStderr()
            pid = 12345

            def wait(self, timeout=None):
                return -2

            def terminate(self):
                events["plain_terminate_called"] = True

            def kill(self):
                events["plain_kill_called"] = True

        monkeypatch.setattr(
            "irsync.rsync_runner.subprocess.Popen", lambda *a, **k: _FakeProc()
        )
        monkeypatch.setattr("irsync.rsync_runner.os.getpgid", lambda pid: 99999)

        def _record_killpg(pgid, sig):
            events["killpg_calls"].append((pgid, sig))

        monkeypatch.setattr("irsync.rsync_runner.os.killpg", _record_killpg)

        with pytest.raises(KeyboardInterrupt):
            run_real_sync(["rsync", "/a/", "/b/"])

        calls = events["killpg_calls"]
        assert calls, (
            "cleanup must call os.killpg to signal the whole process group "
            "(rsync + any forked ssh), not just the rsync leader"
        )
        first_pgid, first_sig = calls[0]
        assert first_pgid == 99999, (
            f"killpg must target the rsync child's pgid; got {first_pgid!r}"
        )
        assert first_sig == _signal.SIGTERM, (
            f"first killpg must be SIGTERM, escalating to SIGKILL only after "
            f"timeout; got {first_sig!r}"
        )

    def test_7th_h2_keyboardinterrupt_during_stream_terminates_child(self, monkeypatch):
        # 7th-NEW-H2: when the stderr-streaming loop is interrupted (Ctrl+C
        # → KeyboardInterrupt, or any other BaseException), the rsync
        # child must be terminated before the exception propagates.
        # Otherwise the child keeps writing to dest while the parent's
        # cleanup runs, and the next irsync run can race it. The 8th pass
        # routes the kill through os.killpg (whole process group, so ssh
        # forks die too); accept either signaling path here.
        events: dict[str, object] = {
            "terminated": False,
            "killed": False,
            "killpg_calls": [],
        }

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
            pid = 4242

            def wait(self, timeout=None):
                return -2

            def terminate(self):
                events["terminated"] = True

            def kill(self):
                events["killed"] = True

        monkeypatch.setattr(
            "irsync.rsync_runner.subprocess.Popen", lambda *a, **k: _FakeProc()
        )
        monkeypatch.setattr("irsync.rsync_runner.os.getpgid", lambda pid: 7777)
        monkeypatch.setattr(
            "irsync.rsync_runner.os.killpg",
            lambda pgid, sig: events["killpg_calls"].append((pgid, sig)),
        )
        with pytest.raises(KeyboardInterrupt):
            run_real_sync(["rsync", "/a/", "/b/"])
        assert events["terminated"] or events["killpg_calls"], (
            "rsync child must be signaled when the stderr loop is "
            "interrupted, so it can't continue writing to dest after the "
            "parent has aborted"
        )
