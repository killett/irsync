from irsync.rsync_runner import build_rsync_command, parse_rsync_output


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

    def test_excludes_snapshot_file_always(self):
        cmd = build_rsync_command(source="/src/", dest="/dst/", dry_run=False)
        i = cmd.index("--exclude")
        # Find the one whose value is the snapshot filename
        assert ".irsync_snapshot.jsonl" in cmd[i + 1 : i + 2] or any(
            cmd[j + 1] == ".irsync_snapshot.jsonl"
            for j, x in enumerate(cmd)
            if x == "--exclude"
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
