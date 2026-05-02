from pathlib import Path

import pytest

from irsync.paths import ensure_local_dir, is_rsync_remote, with_trailing_slash


class TestIsRsyncRemote:
    @pytest.mark.parametrize(
        "spec",
        [
            "host:/path",
            "host:relative/path",
            "user@host:/path",
            "user@host:path",
            "user.name@host-1.example.com:/srv/data",
        ],
    )
    def test_remote(self, spec):
        assert is_rsync_remote(spec) is True

    @pytest.mark.parametrize(
        "spec",
        [
            "/absolute/path",
            "relative/path",
            "./relative",
            "",
            "no-colon-here",
            "/path:with:colons",  # leading slash means it's a local path even with colons
        ],
    )
    def test_local(self, spec):
        assert is_rsync_remote(spec) is False

    def test_accepts_path_object(self):
        assert is_rsync_remote(Path("/local/path")) is False


class TestWithTrailingSlash:
    def test_local_already_trailing(self):
        assert with_trailing_slash("/foo/bar/", remote=False) == "/foo/bar/"

    def test_local_adds_slash(self):
        assert with_trailing_slash("/foo/bar", remote=False) == "/foo/bar/"

    def test_local_accepts_path(self):
        assert with_trailing_slash(Path("/foo/bar"), remote=False) == "/foo/bar/"

    def test_remote_adds_slash_to_path(self):
        assert with_trailing_slash("host:/foo/bar", remote=True) == "host:/foo/bar/"

    def test_remote_already_trailing(self):
        assert with_trailing_slash("user@host:/x/", remote=True) == "user@host:/x/"


class TestEnsureLocalDir:
    def test_resolves_existing_dir(self, tmp_path):
        result = ensure_local_dir(tmp_path)
        assert result == tmp_path.resolve()

    def test_missing_raises_filenotfound(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ensure_local_dir(tmp_path / "does_not_exist")

    def test_file_raises_notadirectory(self, tmp_path):
        f = tmp_path / "afile"
        f.write_text("x")
        with pytest.raises(NotADirectoryError):
            ensure_local_dir(f)

    def test_expands_user(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = ensure_local_dir("~")
        assert result == tmp_path.resolve()
