"""Unit tests for the pre-flight endpoint identity checks."""

import pytest

from irsync.preflight import (
    DestinationNotMounted,
    EndpointNotMounted,
    RsyncUnavailable,
    SourceNotMounted,
    UnsafeDestination,
    check_mounted,
    check_rsync_available,
    foreign_dest_entries,
    fresh_snapshot_is_empty,
    mount_gate_root,
    source_root_is_empty,
)
from irsync.snapshot import LOCKFILE_NAME, SNAPSHOT_FILENAME


class TestMountGateRoot:
    def test_drive_letter_endpoint_gates_on_itself(self, tmp_path):
        base = tmp_path / "media" / "u"
        assert mount_gate_root(base / "G", base) == base / "G"

    def test_subdirectory_gates_on_the_drive_above_it(self, tmp_path):
        # mypython resolves to a path INSIDE drive G, and the ~ backup lands
        # inside drive M. Gating on the endpoint itself would reject both.
        base = tmp_path / "media" / "u"
        deep = base / "G" / "Documents" / "Programming" / "python"
        assert mount_gate_root(deep, base) == base / "G"

    def test_path_outside_base_dir_is_exempt(self, tmp_path):
        base = tmp_path / "media" / "u"
        assert mount_gate_root(tmp_path / "home" / "u", base) is None

    def test_base_dir_itself_is_exempt(self, tmp_path):
        # /media/u is not a drive; there is no component below it to check.
        base = tmp_path / "media" / "u"
        assert mount_gate_root(base, base) is None


class TestCheckMounted:
    def test_remote_endpoint_is_exempt(self, tmp_path):
        # A remote endpoint is a str, not a Path, and has no local mount.
        check_mounted("host:/srv/data", tmp_path, gate_outside_base=True)

    def test_unmounted_drive_raises(self, tmp_path, monkeypatch):
        base = tmp_path / "media" / "u"
        drive = base / "G"
        drive.mkdir(parents=True)
        monkeypatch.setattr("os.path.ismount", lambda p: False)

        with pytest.raises(EndpointNotMounted) as excinfo:
            check_mounted(drive, base)

        assert str(drive) in str(excinfo.value)

    def test_mounted_drive_passes(self, tmp_path, monkeypatch):
        base = tmp_path / "media" / "u"
        drive = base / "G"
        drive.mkdir(parents=True)
        monkeypatch.setattr("os.path.ismount", lambda p: str(p) == str(drive))

        check_mounted(drive, base)

    def test_outside_base_is_exempt_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr("os.path.ismount", lambda p: False)
        check_mounted(tmp_path / "mnt" / "data", tmp_path / "media" / "u")

    def test_outside_base_checked_when_opted_in(self, tmp_path, monkeypatch):
        monkeypatch.setattr("os.path.ismount", lambda p: False)
        with pytest.raises(EndpointNotMounted):
            check_mounted(
                tmp_path / "mnt" / "data",
                tmp_path / "media" / "u",
                gate_outside_base=True,
            )

    def test_base_dir_is_exempt_even_when_gate_outside_base(
        self, tmp_path, monkeypatch
    ):
        # base_dir itself is the ordinary directory containing mountpoints,
        # never a mountpoint, so it must be exempt even with gate_outside_base=True.
        base = tmp_path / "media" / "u"
        base.mkdir(parents=True)
        monkeypatch.setattr("os.path.ismount", lambda p: False)

        check_mounted(base, base, gate_outside_base=True)

    def test_default_role_raises_source_not_mounted(self, tmp_path, monkeypatch):
        base = tmp_path / "media" / "u"
        drive = base / "G"
        drive.mkdir(parents=True)
        monkeypatch.setattr("os.path.ismount", lambda p: False)

        with pytest.raises(SourceNotMounted):
            check_mounted(drive, base)

    def test_role_source_raises_source_not_mounted(self, tmp_path, monkeypatch):
        base = tmp_path / "media" / "u"
        drive = base / "G"
        drive.mkdir(parents=True)
        monkeypatch.setattr("os.path.ismount", lambda p: False)

        with pytest.raises(SourceNotMounted):
            check_mounted(drive, base, role="source")

    def test_role_destination_raises_destination_not_mounted(
        self, tmp_path, monkeypatch
    ):
        base = tmp_path / "media" / "u"
        drive = base / "G_backup"
        drive.mkdir(parents=True)
        monkeypatch.setattr("os.path.ismount", lambda p: False)

        with pytest.raises(DestinationNotMounted):
            check_mounted(drive, base, role="destination")

    def test_source_not_mounted_is_also_endpoint_not_mounted(
        self, tmp_path, monkeypatch
    ):
        # Existing `except EndpointNotMounted` call sites (cli.main,
        # run_backup's --allow-unmounted handler) must keep working.
        base = tmp_path / "media" / "u"
        drive = base / "G"
        drive.mkdir(parents=True)
        monkeypatch.setattr("os.path.ismount", lambda p: False)

        with pytest.raises(EndpointNotMounted):
            check_mounted(drive, base, role="source")

    def test_destination_not_mounted_is_also_endpoint_not_mounted(
        self, tmp_path, monkeypatch
    ):
        base = tmp_path / "media" / "u"
        drive = base / "G_backup"
        drive.mkdir(parents=True)
        monkeypatch.setattr("os.path.ismount", lambda p: False)

        with pytest.raises(EndpointNotMounted):
            check_mounted(drive, base, role="destination")

    def test_destination_not_mounted_is_not_a_source_not_mounted(
        self, tmp_path, monkeypatch
    ):
        # The two roles are siblings, not parent/child of each other: a
        # handler that specifically wants SourceNotMounted must not
        # accidentally also catch a DestinationNotMounted.
        base = tmp_path / "media" / "u"
        drive = base / "G_backup"
        drive.mkdir(parents=True)
        monkeypatch.setattr("os.path.ismount", lambda p: False)

        with pytest.raises(DestinationNotMounted):
            try:
                check_mounted(drive, base, role="destination")
            except SourceNotMounted:
                pytest.fail("DestinationNotMounted must not be a SourceNotMounted")


class TestForeignDestEntries:
    def test_empty_destination_returns_nothing(self, tmp_path):
        assert foreign_dest_entries(tmp_path) == []

    def test_irsync_reserved_files_do_not_count(self, tmp_path):
        # An interrupted first backup leaves a snapshot behind. Retrying must
        # not require an override.
        (tmp_path / SNAPSHOT_FILENAME).write_text("{}\n")
        (tmp_path / LOCKFILE_NAME).write_text("")
        (tmp_path / ".irsync-snap-abc123").write_text("")
        assert foreign_dest_entries(tmp_path) == []

    def test_user_data_counts(self, tmp_path):
        (tmp_path / "photos").mkdir()
        (tmp_path / "notes.txt").write_text("hi")
        assert foreign_dest_entries(tmp_path) == ["notes.txt", "photos"]

    def test_missing_destination_is_empty(self, tmp_path):
        assert foreign_dest_entries(tmp_path / "not_created_yet") == []

    def test_unreadable_destination_fails_closed(self, tmp_path):
        dest = tmp_path / "locked"
        dest.mkdir()
        dest.chmod(0o000)
        try:
            with pytest.raises(UnsafeDestination):
                foreign_dest_entries(dest)
        finally:
            dest.chmod(0o755)


class TestSourceRootIsEmpty:
    def test_empty_source_is_empty(self, tmp_path):
        assert source_root_is_empty(tmp_path) is True

    def test_reserved_files_only_still_counts_as_empty(self, tmp_path):
        # A source containing only a leftover lockfile from an earlier run
        # is still empty: the reserved namespace is not user content.
        (tmp_path / SNAPSHOT_FILENAME).write_text("{}\n")
        (tmp_path / LOCKFILE_NAME).write_text("")
        (tmp_path / ".irsync-snap-abc123").write_text("")
        assert source_root_is_empty(tmp_path) is True

    def test_user_content_is_not_empty(self, tmp_path):
        (tmp_path / "photo.jpg").write_text("data")
        assert source_root_is_empty(tmp_path) is False

    def test_missing_source_counts_as_empty(self, tmp_path):
        assert source_root_is_empty(tmp_path / "not_created_yet") is True

    def test_unreadable_source_fails_closed(self, tmp_path):
        # M1: a PermissionError from _non_reserved_names used to be the only
        # one of its siblings not caught here, escaping uncaught instead of
        # failing closed like foreign_dest_entries does on the destination
        # side. An unreadable source is not a proven-non-empty one, so this
        # must raise rather than silently guess either way.
        src = tmp_path / "locked"
        src.mkdir()
        (src / "photo.jpg").write_text("data")
        src.chmod(0o000)
        try:
            with pytest.raises(PermissionError):
                source_root_is_empty(src)
        finally:
            src.chmod(0o755)


class TestFreshSnapshotIsEmpty:
    def test_root_only_row_is_empty(self):
        # snapshot_tree always emits the root itself as path == ".", so an
        # empty source yields exactly one row, never zero.
        rows = [{"path": "."}]
        assert fresh_snapshot_is_empty(rows) is True

    def test_root_plus_content_is_not_empty(self):
        rows = [{"path": "."}, {"path": "file.txt"}]
        assert fresh_snapshot_is_empty(rows) is False

    def test_empty_list_is_not_mistaken_for_a_real_empty_source(self):
        # An empty list never actually comes out of snapshot_tree (the root
        # row is unconditional), but the check must not silently treat a
        # malformed/empty list as the same case as a genuine single-root
        # snapshot — pin the exact condition rather than a laxer len()<=1.
        assert fresh_snapshot_is_empty([]) is False


class TestRsyncAvailable:
    def test_missing_rsync_raises(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        with pytest.raises(RsyncUnavailable) as excinfo:
            check_rsync_available()
        assert "rsync" in str(excinfo.value)

    def test_present_rsync_passes(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/rsync")
        check_rsync_available()
