"""Unit tests for the pre-flight endpoint identity checks."""

import pytest

from irsync.preflight import (
    EndpointNotMounted,
    check_mounted,
    mount_gate_root,
)


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
