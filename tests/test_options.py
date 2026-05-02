from pathlib import Path

import pytest

from irsync.options import Endpoints, Options, resolve_endpoints


def _opts(tmp_path):
    base = tmp_path / "media"
    base.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    pydir = base / "G" / "Documents" / "Programming" / "python"
    pydir.mkdir(parents=True)
    homedir_backup = base / "M" / "homedir_backup" / "u"
    homedir_backup.mkdir(parents=True)
    python_backup = base / "M" / "python_backup"
    python_backup.mkdir(parents=True)
    return Options(
        base_dir=base,
        homedir=home,
        homedir_backup=homedir_backup,
        python_dir=pydir,
        python_backup_dir=python_backup,
    )


class TestResolveEndpoints:
    def test_drive_letter_no_dest(self, tmp_path):
        opts = _opts(tmp_path)
        (opts.base_dir / "G").mkdir(exist_ok=True)
        ep = resolve_endpoints("g", None, opts)
        assert isinstance(ep, Endpoints)
        assert ep.source == opts.base_dir / "G"
        assert ep.dest == opts.base_dir / "G_backup"
        assert ep.source_is_remote is False
        assert ep.dest_is_remote is False

    def test_drive_letter_uppercases(self, tmp_path):
        opts = _opts(tmp_path)
        (opts.base_dir / "F").mkdir(exist_ok=True)
        ep = resolve_endpoints("f", None, opts)
        assert ep.source.name == "F"

    def test_tilde_uses_homedir_backup(self, tmp_path):
        opts = _opts(tmp_path)
        ep = resolve_endpoints("~", None, opts)
        assert ep.source == opts.homedir
        assert ep.dest == opts.homedir_backup

    def test_mypython_uses_python_backup_dir(self, tmp_path):
        opts = _opts(tmp_path)
        ep = resolve_endpoints("mypython", None, opts)
        assert ep.source == opts.python_dir
        assert ep.dest == opts.python_backup_dir

    def test_explicit_paths(self, tmp_path):
        opts = _opts(tmp_path)
        s = tmp_path / "src"
        s.mkdir()
        d = tmp_path / "dst"
        d.mkdir()
        ep = resolve_endpoints(str(s), str(d), opts)
        assert ep.source == s
        assert ep.dest == d

    def test_remote_source(self, tmp_path):
        opts = _opts(tmp_path)
        d = tmp_path / "dst"
        d.mkdir()
        ep = resolve_endpoints("user@host:/data", str(d), opts)
        assert ep.source_is_remote is True
        assert ep.source == "user@host:/data"

    def test_missing_dest_for_arbitrary_path_raises(self, tmp_path):
        opts = _opts(tmp_path)
        with pytest.raises(ValueError, match="Destination"):
            resolve_endpoints("/some/path", None, opts)

    def test_remote_to_remote_rejected(self, tmp_path):
        opts = _opts(tmp_path)
        with pytest.raises(ValueError, match="emote-to-remote"):
            resolve_endpoints("u@h:/a", "u@h:/b", opts)

    def test_source_inside_dest_rejected(self, tmp_path):
        opts = _opts(tmp_path)
        outer = tmp_path / "outer"
        outer.mkdir()
        inner = outer / "inner"
        inner.mkdir()
        with pytest.raises(ValueError, match="inside"):
            resolve_endpoints(str(inner), str(outer), opts)

    def test_source_equals_dest_rejected(self, tmp_path):
        opts = _opts(tmp_path)
        s = tmp_path / "x"
        s.mkdir()
        with pytest.raises(ValueError, match="same path"):
            resolve_endpoints(str(s), str(s), opts)


class TestDefaultOptions:
    def test_default_options_resolves_homedir(self):
        # Default constructor uses Path.home() and /media/<user>
        opts = Options.from_defaults()
        assert opts.homedir == Path.home().resolve()
        assert isinstance(opts.exclude_dirs, list)
