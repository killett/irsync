"""Configuration knobs and source/dest argument resolution for the irsync CLI."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from drivecfg import Drive, DriveConfig, UnknownDriveError

from irsync.paths import ensure_local_dir, is_rsync_remote

ALL_ARG = "ALL"
"""Source argument meaning "every entry in the configured backup order"."""

HOME_ARG = "~"
"""Source argument for the home directory. Resolved as a configured endpoint."""


@dataclass
class Options:
    """Path knobs and defaults for the backup workflow.

    The drive list, named endpoints, and backup order all come from a
    :class:`drivecfg.DriveConfig`; nothing about a particular machine is
    hardcoded here. ``base_dir`` keeps a default because it is only used as
    the mount-gate root (see :func:`irsync.preflight.check_mounted`), which
    can refuse a run but can never redirect one.

    Attributes:
        base_dir: Parent directory of every drive; the mount-gate root.
        homedir: The current user's home directory.
        drive_config: The loaded layout, or None for a plain-path run.
        exclude_dirs: Directory names rsync is told to skip by default.
        max_errors: How many failures an ``ALL`` run tolerates before stopping.
    """

    base_dir: Path
    homedir: Path
    drive_config: DriveConfig | None = None
    exclude_dirs: list[str] = field(
        default_factory=lambda: [".Trash-1000", ".cache", "unfinished_downloads"]
    )
    max_errors: int = 5

    @property
    def all_backups(self) -> list[str]:
        """The ordered source arguments for a whole-machine (``ALL``) backup.

        Returns:
            The configured backup order, or an empty list when no config is
            loaded (in which case ``ALL`` never gets this far — the CLI
            refuses first).
        """
        if self.drive_config is None:
            return []
        return list(self.drive_config.backup_order)

    @classmethod
    def without_drive_config(cls) -> Options:
        """Build options for a plain-path run, which needs no config file.

        Returns:
            Options whose only path knowledge is the mount-gate root.
        """
        homedir = Path.home().resolve()
        return cls(base_dir=Path("/") / "media" / homedir.name, homedir=homedir)

    @classmethod
    def from_drive_config(cls, cfg: DriveConfig) -> Options:
        """Build options from a loaded drive configuration.

        Args:
            cfg: The layout loaded by drivecfg.

        Returns:
            Options whose drives, endpoints, backup order, and mount-gate
            root all come from ``cfg``.
        """
        return cls(
            base_dir=cfg.base_dir, homedir=Path.home().resolve(), drive_config=cfg
        )


@dataclass(frozen=True)
class Endpoints:
    """Resolved source/dest pair plus remote flags for a single backup."""

    source: Path | str
    dest: Path | str
    source_is_remote: bool
    dest_is_remote: bool


def _is_letter_token(token: str) -> bool:
    """Return True for a single-letter drive shorthand such as ``g``."""
    return len(token) == 1 and token.isalpha()


def needs_drive_config(source_arg: str, destination_arg: str | None) -> bool:
    """Return True when this invocation cannot be resolved without a config.

    Deliberately decided from the argument text alone, never from what
    happens to exist in the working directory, so the same command always
    means the same thing. A plain path — anything containing a separator,
    a ``~``-prefixed path, ``.``/``..``, or an rsync remote — never needs a
    config, which is what keeps ``irsync SRC DEST`` working on a machine
    that has no ``drives.toml`` at all. A bare name with no destination has
    no other possible meaning than a configured drive or endpoint, so it
    does need one.

    Args:
        source_arg: Raw source argument from the user.
        destination_arg: Raw destination argument, or None.

    Returns:
        True if the caller must load a :class:`drivecfg.DriveConfig` first.
    """
    token = source_arg.strip()
    destination = (destination_arg or "").strip()
    if not token:
        return False
    if token.upper() == ALL_ARG or token == HOME_ARG:
        return True
    if _is_letter_token(token):
        return True
    if destination:
        return False
    if is_rsync_remote(token):
        return False
    if token in {".", ".."} or token.startswith(HOME_ARG):
        return False
    return os.sep not in token


def _require_config(options: Options, source_arg: str) -> DriveConfig:
    """Return the loaded config, or explain that this argument needs one.

    Args:
        options: The options in play.
        source_arg: The argument that triggered the requirement.

    Returns:
        The loaded :class:`drivecfg.DriveConfig`.

    Raises:
        ValueError: If no config was loaded. The message distinguishes a
            shorthand that needs a config from a plain path that is simply
            missing its destination.
    """
    if options.drive_config is not None:
        return options.drive_config
    if needs_drive_config(source_arg, None):
        raise ValueError(
            f"The source argument {source_arg!r} names a configured drive or "
            "endpoint, but no drive config was loaded. Pass --config PATH, "
            "set DRIVECFG_CONFIG, or create drives.toml under "
            "$XDG_CONFIG_HOME/drivecfg (usually ~/.config/drivecfg)."
        )
    raise ValueError(
        "Destination folder is required unless the source argument is a "
        f"configured drive or endpoint. The source argument is {source_arg!r}."
    )


def _lookup_drive(cfg: DriveConfig, token: str) -> Drive:
    """Look up a drive, converting drivecfg's error into a ValueError.

    Args:
        cfg: The loaded configuration.
        token: The drive id from the command line, in any case.

    Returns:
        The matching drive.

    Raises:
        ValueError: If the token is not a configured drive. The message
            names every configured drive id.
    """
    try:
        return cfg.drive(token)
    except UnknownDriveError as exc:
        raise ValueError(str(exc)) from exc


def _find_drive(cfg: DriveConfig, token: str) -> Drive | None:
    """Return the drive ``token`` names, or None if it names no drive."""
    try:
        return cfg.drive(token)
    except UnknownDriveError:
        return None


def _endpoint_name(cfg: DriveConfig, token: str) -> str | None:
    """Return the endpoint ``token`` names, directly or as its own source path.

    The path form preserves pre-config behaviour: passing the home directory
    itself (rather than ``~``) resolved to the home backup, and the same held
    for the Python tree. Any endpoint whose configured source resolves to the
    same directory is matched the same way.

    Args:
        cfg: The loaded configuration.
        token: The source argument.

    Returns:
        The endpoint name, or None if nothing matches.
    """
    if token in cfg.endpoints:
        return token
    candidate = Path(token).expanduser().resolve()
    for name in cfg.endpoints:
        source, _dest = cfg.endpoint(name)
        if source.expanduser().resolve() == candidate:
            return name
    return None


def _resolve_named(cfg: DriveConfig, token: str) -> tuple[Path, Path]:
    """Resolve a destination-less source argument against the config.

    Args:
        cfg: The loaded configuration.
        token: A drive id or endpoint name.

    Returns:
        The ``(source, dest)`` pair the config gives for ``token``.

    Raises:
        ValueError: If ``token`` is neither a configured drive nor a
            configured endpoint. The message names both sets.
    """
    drive = _find_drive(cfg, token)
    if drive is not None:
        return drive.path, drive.backup_path
    name = _endpoint_name(cfg, token)
    if name is None:
        drives = ", ".join(d.id for d in cfg.drives) or "(none)"
        endpoints = ", ".join(cfg.endpoints) or "(none)"
        raise ValueError(
            "Destination folder is required unless the source argument is a "
            f"configured drive or endpoint. The source argument is {token!r}. "
            f"Configured drives: {drives}. Configured endpoints: {endpoints}."
        )
    return cfg.endpoint(name)


def resolve_source(source_arg: str, options: Options) -> Path:
    """Resolve a source-only argument (no destination) to a local directory.

    Shares the shorthand rules with :func:`resolve_endpoints` so that
    ``--snapshot-only`` cannot drift into a second, divergent notion of what
    a drive id or endpoint name means. A plain path is still accepted with
    no config at all, because a snapshot needs no destination.

    Args:
        source_arg: Raw source argument from the user.
        options: Path knobs and the loaded config, if any.

    Returns:
        The resolved source directory.

    Raises:
        ValueError: If the argument is empty, or names no configured drive
            or endpoint.
        FileNotFoundError: If the resolved directory does not exist.
        NotADirectoryError: If the resolved path is not a directory.
    """
    token = source_arg.strip()
    if not token:
        raise ValueError("Source argument is required.")
    if not needs_drive_config(token, None):
        return ensure_local_dir(token)
    cfg = _require_config(options, token)
    if _is_letter_token(token):
        return ensure_local_dir(_lookup_drive(cfg, token).path)
    source, _dest = _resolve_named(cfg, token)
    return ensure_local_dir(source)


def resolve_endpoints(
    source_arg: str,
    destination_arg: str | None,
    options: Options,
) -> Endpoints:
    """Map raw CLI args into resolved endpoints, applying configured shortcuts.

    A configured drive id expands to that drive's directory and its backup
    directory, both taken from the config; an endpoint name such as ``~`` or
    ``mypython`` expands to the configured source/dest pair. Anything else is
    taken as a literal path or rsync remote (``host:/path``), which needs no
    config.

    Args:
        source_arg: Raw source argument from the user.
        destination_arg: Raw destination argument, or None.
        options: Path knobs and the loaded config, if any.

    Returns:
        An :class:`Endpoints` describing the resolved pair.

    Raises:
        ValueError: If the args are invalid (missing dest, unconfigured
            drive or endpoint, no config loaded, remote-to-remote, recursive
            paths, identical paths).
        FileNotFoundError: If a local source/dest path does not exist.
        NotADirectoryError: If a local source/dest is not a directory.
    """
    source_arg = source_arg.strip()
    if destination_arg is not None:
        destination_arg = destination_arg.strip() or None

    source: Path | str
    dest: Path | str

    if not source_arg:
        raise ValueError("Source argument is required.")

    if _is_letter_token(source_arg) and not destination_arg:
        drive = _lookup_drive(_require_config(options, source_arg), source_arg)
        source = drive.path
        dest = drive.backup_path
    elif destination_arg:
        if _is_letter_token(source_arg):
            source = _lookup_drive(
                _require_config(options, source_arg), source_arg
            ).path
        else:
            source = source_arg
        dest = destination_arg
    else:
        source, dest = _resolve_named(_require_config(options, source_arg), source_arg)

    source_is_remote = is_rsync_remote(source)
    dest_is_remote = is_rsync_remote(dest)

    if source_is_remote and dest_is_remote:
        raise ValueError(
            "Remote-to-remote rsync is not supported by this wrapper "
            "(exactly one side must be local)."
        )

    if not source_is_remote:
        source = ensure_local_dir(source)
    if not dest_is_remote:
        # Allow the destination to not yet exist on first backup if the parent does.
        try:
            dest = ensure_local_dir(dest)
        except FileNotFoundError:
            dest_path = Path(dest).expanduser().resolve()
            if not dest_path.parent.exists():
                raise
            dest = dest_path

    if not source_is_remote and not dest_is_remote:
        src_p = source if isinstance(source, Path) else Path(source)
        dst_p = dest if isinstance(dest, Path) else Path(dest)
        if src_p == dst_p:
            raise ValueError(
                "Source and destination are the same path. Refusing to run."
            )
        if dst_p.exists() and src_p.is_relative_to(dst_p):
            raise ValueError(
                "Source is inside destination. Refusing to run to avoid recursion/deletion."
            )
        if dst_p.exists() and dst_p.is_relative_to(src_p):
            raise ValueError(
                "Destination is inside source. Refusing to run to avoid recursion/deletion."
            )

    return Endpoints(
        source=source,
        dest=dest,
        source_is_remote=source_is_remote,
        dest_is_remote=dest_is_remote,
    )
