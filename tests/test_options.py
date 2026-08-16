"""Config-backed endpoint resolution.

Every fixture here is rooted at tmp_path and uses invented drive ids (see
tests/conftest.py) — the real layout lives in the user's drivecfg config,
never in this repo.
"""

from pathlib import Path

import pytest

from irsync.options import (
    Endpoints,
    Options,
    needs_drive_config,
    resolve_endpoints,
    resolve_source,
)

from .conftest import DRIVE_IDS


class TestResolveEndpoints:
    def test_drive_id_uses_configured_paths(self, basic_options):
        # Catches the drive shorthand losing its config backing and falling
        # back to the old "<letter>_backup under /media/$USER" assumption.
        cfg = basic_options.drive_config
        (cfg.base_dir / "B").mkdir()
        ep = resolve_endpoints("B", None, basic_options)
        assert isinstance(ep, Endpoints)
        assert ep.source == cfg.base_dir / "B"
        assert ep.dest == cfg.base_dir / "B_backup"
        assert ep.source == cfg.drive("B").path
        assert ep.dest == cfg.drive("B").backup_path
        assert ep.source_is_remote is False
        assert ep.dest_is_remote is False

    def test_drive_id_lookup_is_case_insensitive(self, basic_options):
        # Catches the lower-case shorthand ("irsync b") being passed through
        # verbatim and resolving to a different, non-existent directory.
        cfg = basic_options.drive_config
        (cfg.base_dir / "B").mkdir()
        ep = resolve_endpoints("b", None, basic_options)
        assert ep.source == cfg.drive("B").path

    def test_drive_dir_and_backup_dir_overrides_are_honoured(self, tmp_path):
        # Catches paths being rebuilt as base_dir/<id> and base_dir/<id>_backup
        # instead of being read from the config, which would silently ignore a
        # drive whose directory name differs from its id.
        from drivecfg import load_config

        base = tmp_path / "media"
        (base / "spinner").mkdir(parents=True)
        (base / "vault").mkdir()
        config = tmp_path / "drives.toml"
        config.write_text(
            f'schema_version = 1\nbase_dir = "{base}"\n'
            'drives = [{ id = "B", dir = "spinner", backup_dir = "vault" }]\n',
            encoding="utf-8",
        )
        options = Options.from_drive_config(load_config(config))
        ep = resolve_endpoints("B", None, options)
        assert ep.source == base / "spinner"
        assert ep.dest == base / "vault"

    def test_unconfigured_letter_is_refused_naming_the_drives(self, basic_options):
        # Catches the old "any single letter is a drive" assumption surviving
        # migration, which would resolve Z to a path that does not exist and
        # may not be yours.
        with pytest.raises(
            ValueError, match="Configured drives: A, B, C, D"
        ) as excinfo:
            resolve_endpoints("Z", None, basic_options)
        assert "'Z'" in str(excinfo.value)

    def test_named_endpoint_comes_from_config(self, basic_options):
        # Catches the mypython source/destination being hardcoded again.
        cfg = basic_options.drive_config
        ep = resolve_endpoints("mypython", None, basic_options)
        assert ep.source == cfg.drive(DRIVE_IDS[0]).path / "code"
        assert ep.dest == cfg.drive(DRIVE_IDS[-1]).path / "python_backup"

    def test_home_endpoint_comes_from_config(self, basic_options, tmp_path):
        # Catches '~' being resolved from Path.home()/a hardcoded backup path
        # rather than from the configured endpoint.
        cfg = basic_options.drive_config
        ep = resolve_endpoints("~", None, basic_options)
        assert ep.source == (tmp_path / "home").resolve()
        assert ep.dest == cfg.drive(DRIVE_IDS[-1]).path / "home_backup"

    def test_endpoint_source_path_is_an_alias_for_its_name(
        self, basic_options, tmp_path
    ):
        # Pre-migration, naming the home directory itself (not '~') resolved
        # to the home backup. Catches that alias being dropped, which would
        # turn a working invocation into "destination folder is required".
        cfg = basic_options.drive_config
        ep = resolve_endpoints(str(tmp_path / "home"), None, basic_options)
        assert ep.source == (tmp_path / "home").resolve()
        assert ep.dest == cfg.drive(DRIVE_IDS[-1]).path / "home_backup"

    def test_unknown_name_without_destination_names_drives_and_endpoints(
        self, basic_options
    ):
        # Catches a typo'd shorthand producing a bare "destination required"
        # message that never tells the user what IS configured.
        with pytest.raises(ValueError) as excinfo:
            resolve_endpoints("mypthon", None, basic_options)
        message = str(excinfo.value)
        assert "Configured drives: A, B, C, D" in message
        assert "Configured endpoints: ~, mypython" in message

    def test_drive_id_with_explicit_destination_uses_configured_source(
        self, basic_options, tmp_path
    ):
        # Catches "irsync B /somewhere" reverting to base_dir/<letter> instead
        # of the configured drive directory.
        cfg = basic_options.drive_config
        (cfg.base_dir / "B").mkdir()
        dest = tmp_path / "dst"
        dest.mkdir()
        ep = resolve_endpoints("B", str(dest), basic_options)
        assert ep.source == cfg.drive("B").path
        assert ep.dest == dest

    def test_explicit_paths(self, basic_options, tmp_path):
        s = tmp_path / "src"
        s.mkdir()
        d = tmp_path / "dst"
        d.mkdir()
        ep = resolve_endpoints(str(s), str(d), basic_options)
        assert ep.source == s
        assert ep.dest == d

    def test_remote_source(self, basic_options, tmp_path):
        d = tmp_path / "dst"
        d.mkdir()
        ep = resolve_endpoints("user@host:/data", str(d), basic_options)
        assert ep.source_is_remote is True
        assert ep.source == "user@host:/data"

    def test_missing_dest_for_arbitrary_path_raises(self, basic_options):
        with pytest.raises(ValueError, match="Destination"):
            resolve_endpoints("/some/path", None, basic_options)

    def test_multi_character_drive_id_resolves(self, tmp_path):
        # Drive ids are whatever the config says they are, not "one letter".
        # Catches the resolver only ever consulting the config for tokens
        # that happen to be a single character.
        from drivecfg import load_config

        base = tmp_path / "media"
        (base / "NAS").mkdir(parents=True)
        config = tmp_path / "drives.toml"
        config.write_text(
            f'schema_version = 1\nbase_dir = "{base}"\ndrives = [{{ id = "NAS" }}]\n',
            encoding="utf-8",
        )
        options = Options.from_drive_config(load_config(config))
        ep = resolve_endpoints("nas", None, options)
        assert ep.source == base / "NAS"
        assert ep.dest == base / "NAS_backup"

    def test_empty_source_is_rejected(self, basic_options):
        with pytest.raises(ValueError, match="Source argument is required"):
            resolve_endpoints("   ", None, basic_options)

    def test_remote_to_remote_rejected(self, basic_options):
        with pytest.raises(ValueError, match="emote-to-remote"):
            resolve_endpoints("u@h:/a", "u@h:/b", basic_options)

    def test_source_inside_dest_rejected(self, basic_options, tmp_path):
        outer = tmp_path / "outer"
        outer.mkdir()
        inner = outer / "inner"
        inner.mkdir()
        with pytest.raises(ValueError, match="inside"):
            resolve_endpoints(str(inner), str(outer), basic_options)

    def test_source_equals_dest_rejected(self, basic_options, tmp_path):
        s = tmp_path / "x"
        s.mkdir()
        with pytest.raises(ValueError, match="same path"):
            resolve_endpoints(str(s), str(s), basic_options)


class TestWithoutDriveConfig:
    def test_plain_paths_resolve_with_no_config_loaded(self, tmp_path):
        # THE compatibility case: a stranger who installed irsync from PyPI
        # and has no drives.toml at all. Catches resolution acquiring a hard
        # dependency on a loaded config.
        options = Options.without_drive_config()
        s = tmp_path / "src"
        s.mkdir()
        d = tmp_path / "dst"
        d.mkdir()
        ep = resolve_endpoints(str(s), str(d), options)
        assert (ep.source, ep.dest) == (s, d)

    def test_shorthand_without_config_says_how_to_supply_one(self):
        # Catches a missing config surfacing as an AttributeError on None, or
        # as a refusal that does not tell the user what to do next.
        options = Options.without_drive_config()
        with pytest.raises(ValueError) as excinfo:
            resolve_endpoints("G", None, options)
        message = str(excinfo.value)
        assert "--config" in message
        assert "DRIVECFG_CONFIG" in message
        assert "drives.toml" in message

    def test_plain_path_without_a_destination_asks_for_a_destination(self):
        # A path is not a shorthand: with no config loaded, the refusal must
        # say the destination is missing, not send the user off to write a
        # drives.toml they do not need.
        options = Options.without_drive_config()
        with pytest.raises(ValueError) as excinfo:
            resolve_endpoints("/some/path", None, options)
        message = str(excinfo.value)
        assert "Destination folder is required" in message
        assert "--config" not in message

    def test_base_dir_defaults_to_the_media_mount_root(self):
        # base_dir survives as the mount-gate root only. Catches it being
        # dropped (which would disable the gate) or made config-only.
        options = Options.without_drive_config()
        assert options.base_dir == Path("/media") / Path.home().resolve().name
        assert options.homedir == Path.home().resolve()
        assert options.drive_config is None

    def test_all_backups_is_empty_without_a_config(self):
        # Catches an ALL run inventing a drive list when none is configured.
        assert Options.without_drive_config().all_backups == []


class TestAllBackups:
    def test_all_backups_comes_from_backup_order(self, make_options):
        # Catches all_backups being rebuilt from a hardcoded list, and catches
        # the order being lost (the drives are spliced between the two
        # endpoints, so any reordering shows up).
        options = make_options()
        assert options.all_backups == ["mypython", "A", "B", "C", "D", "~"]

    def test_backup_order_is_taken_verbatim_when_given(self, make_options):
        # Catches *drives being re-expanded or the configured order being
        # sorted/deduplicated on the way through.
        options = make_options(backup_order=["D", "~", "B"])
        assert options.all_backups == ["D", "~", "B"]

    def test_a_drive_marked_backup_false_is_still_addressable(self, tmp_path):
        # backup = false excludes a drive from an ALL run but must not stop
        # "irsync <that drive>" from resolving.
        from drivecfg import load_config

        base = tmp_path / "media"
        (base / "B").mkdir(parents=True)
        config = tmp_path / "drives.toml"
        config.write_text(
            f'schema_version = 1\nbase_dir = "{base}"\n'
            'drives = [{ id = "B", backup = false }, { id = "C" }]\n',
            encoding="utf-8",
        )
        options = Options.from_drive_config(load_config(config))
        assert options.all_backups == ["C"]
        assert resolve_endpoints("B", None, options).source == base / "B"


class TestNeedsDriveConfig:
    @pytest.mark.parametrize(
        ("source", "destination"),
        [
            ("ALL", None),
            ("all", None),
            ("~", None),
            ("G", None),
            ("g", None),
            ("G", "/abs/dst"),
            ("mypython", None),
            ("archive", None),
        ],
    )
    def test_shorthands_need_a_config(self, source, destination):
        # Catches the lazy-loading gate missing a shorthand, which would send
        # it into resolution with no config and fail with the wrong message.
        assert needs_drive_config(source, destination) is True

    @pytest.mark.parametrize(
        ("source", "destination"),
        [
            ("/abs/src", "/abs/dst"),
            ("/abs/src", None),
            ("./rel", "/abs/dst"),
            ("~/sub", "/abs/dst"),
            ("host:/path", None),
            ("user@host:/path", "/abs/dst"),
            ("mypython", "/abs/dst"),
            (".", None),
            ("", None),
        ],
    )
    def test_plain_paths_do_not_need_a_config(self, source, destination):
        # Catches config discovery being attempted for an ordinary rsync run,
        # which would make irsync unusable without a drives.toml.
        assert needs_drive_config(source, destination) is False


class TestResolveSource:
    def test_drive_id_resolves_the_same_way_as_a_full_backup(self, basic_options):
        # --snapshot-only used to carry its own copy of the shorthand rules.
        # Catches the two drifting apart.
        cfg = basic_options.drive_config
        (cfg.base_dir / "B").mkdir()
        assert resolve_source("b", basic_options) == cfg.drive("B").path

    def test_endpoint_name_resolves_to_its_source(self, basic_options):
        cfg = basic_options.drive_config
        assert (
            resolve_source("mypython", basic_options)
            == cfg.drive(DRIVE_IDS[0]).path / "code"
        )

    def test_plain_path_needs_no_config_and_no_destination(self, tmp_path):
        # --snapshot-only against an arbitrary directory is the whole point of
        # that flag; catches it being routed through a resolver that demands a
        # destination or a config.
        src = tmp_path / "anywhere"
        src.mkdir()
        assert resolve_source(str(src), Options.without_drive_config()) == src

    def test_unconfigured_letter_is_refused(self, basic_options):
        with pytest.raises(ValueError, match="Configured drives: A, B, C, D"):
            resolve_source("Z", basic_options)

    def test_empty_source_is_rejected(self, basic_options):
        with pytest.raises(ValueError, match="Source argument is required"):
            resolve_source("  ", basic_options)

    def test_missing_directory_still_raises_file_not_found(self, basic_options):
        cfg = basic_options.drive_config
        assert not (cfg.base_dir / "C").exists()
        with pytest.raises(FileNotFoundError):
            resolve_source("C", basic_options)
