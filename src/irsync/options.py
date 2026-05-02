"""Configuration knobs and source/dest argument resolution for the irsync CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from irsync.paths import ensure_local_dir, is_rsync_remote


@dataclass
class Options:
    """Path knobs and defaults for the backup workflow.

    Mirrors the configuration block at the top of the original ``srsync`` script.
    Keep this purely declarative — runtime decisions live in :mod:`irsync.backup`.
    """

    base_dir: Path
    homedir: Path
    homedir_backup: Path
    python_dir: Path
    python_backup_dir: Path
    exclude_dirs: list[str] = field(
        default_factory=lambda: [".Trash-1000", ".cache", "unfinished_downloads"]
    )
    all_backups: list[str] = field(
        default_factory=lambda: [
            "mypython",
            "F",
            "G",
            "H",
            "J",
            "K",
            "L",
            "M",
            "P",
            "Q",
            "R",
            "S",
            "T",
            "U",
            "V",
            "~",
        ]
    )
    max_errors: int = 5

    @classmethod
    def from_defaults(cls) -> Options:
        """Build an :class:`Options` from the same defaults srsync uses on this machine."""
        homedir = Path.home().resolve()
        username = homedir.name
        base_dir = Path("/") / "media" / username
        return cls(
            base_dir=base_dir,
            homedir=homedir,
            homedir_backup=base_dir / "M" / "homedir_backup" / username,
            python_dir=base_dir / "G" / "Documents" / "Programming" / "python",
            python_backup_dir=base_dir / "M" / "python_backup",
        )


@dataclass(frozen=True)
class Endpoints:
    """Resolved source/dest pair plus remote flags for a single backup."""

    source: Path | str
    dest: Path | str
    source_is_remote: bool
    dest_is_remote: bool


def resolve_endpoints(
    source_arg: str,
    destination_arg: str | None,
    options: Options,
) -> Endpoints:
    """Map raw CLI args into resolved endpoints, applying drive-letter shortcuts.

    Mirrors the routing logic from ``srsync.check_args_and_rsync_once`` (lines 340-413
    of the original script).

    Args:
        source_arg: Raw source argument from the user.
        destination_arg: Raw destination argument, or None.
        options: Path defaults (drive base, homedir, mypython, etc.).

    Returns:
        An :class:`Endpoints` describing the resolved pair.

    Raises:
        ValueError: If the args are invalid (missing dest, remote-to-remote,
            recursive paths, identical paths).
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

    if len(source_arg) == 1 and source_arg.isalpha() and not destination_arg:
        letter = source_arg.upper()
        source = options.base_dir / letter
        dest = options.base_dir / (letter + "_backup")
    elif destination_arg:
        if len(source_arg) == 1 and source_arg.isalpha():
            letter = source_arg.upper()
            source = options.base_dir / letter
        else:
            source = source_arg
        dest = destination_arg
    else:
        if (
            source_arg == "~"
            or Path(source_arg).expanduser().resolve() == options.homedir
        ):
            source = options.homedir
            dest = options.homedir_backup
        elif (
            source_arg == "mypython" or Path(source_arg).resolve() == options.python_dir
        ):
            source = options.python_dir
            dest = options.python_backup_dir
        else:
            raise ValueError(
                "Destination folder is required unless the source argument is a drive letter, "
                f"'~', or 'mypython'. The source argument is {source_arg!r}."
            )

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
