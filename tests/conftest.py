"""Shared fixtures: random trees, options builders, etc."""

import argparse
import os
import random
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from drivecfg import load_config

from irsync.options import Options

# Invented drive ids, deliberately unrelated to any real machine's layout:
# this repo is public, and the point of the drivecfg migration is that the
# real drive list lives in the user's config file, never in the source tree.
DRIVE_IDS = ("A", "B", "C", "D")
# Endpoints hang off the first and last ids, so B and C are free for tests
# that need a configured drive whose directory does NOT exist.
SOURCE_DRIVE = DRIVE_IDS[0]
BACKUP_DRIVE = DRIVE_IDS[-1]


@pytest.fixture
def make_tree(tmp_path):
    """Create a small directory tree with files of unique sizes for diff testing."""

    def _factory(
        root: Path, num_files: int = 8, depth: int = 2, seed: int = 42
    ) -> Path:
        rng = random.Random(seed)
        root.mkdir(parents=True, exist_ok=True)
        for i in range(num_files):
            # Choose a random subdir of depth in [0, depth]
            d = rng.randint(0, depth)
            parts = [f"d{rng.randint(0, 3)}" for _ in range(d)]
            sub = root.joinpath(*parts) if parts else root
            sub.mkdir(parents=True, exist_ok=True)
            f = sub / f"file_{i}.bin"
            # Each file gets unique size i+1 bytes so we can detect content changes
            f.write_bytes(b"\x00" * (i + 1))
        return root

    return _factory


def subprocess_pythonpath() -> str:
    """Return the PYTHONPATH a `python -m irsync` subprocess needs.

    A subprocess inherits neither pytest's `pythonpath` setting nor pixi's
    activation environment, so point it at the same source trees this test
    process imported from — for irsync that is the repo's `src/`, which is
    on sys.path only because of pytest's `pythonpath` setting. drivecfg is
    listed alongside it so the child imports whichever drivecfg this process
    did: normally the installed PyPI package (already importable, so the
    entry is a harmless duplicate), but a local editable checkout when a
    developer is testing an unreleased drivecfg against irsync.
    """
    import drivecfg

    import irsync

    return os.pathsep.join(
        str(Path(str(module.__file__)).resolve().parent.parent)
        for module in (irsync, drivecfg)
    )


def cli_args(**overrides: object) -> argparse.Namespace:
    """Return the argparse Namespace a backup call expects, with overrides applied.

    This mirrors the flag surface of :func:`irsync.cli._build_parser`, so it
    lives here rather than in each test module: a new CLI flag that
    ``backup.py`` reads has to be added in exactly one place, instead of
    leaving whichever copy was missed to fail with an AttributeError.

    Args:
        **overrides: Flag values to change from their defaults. ``yes`` is
            True by default so tests never block on the confirmation prompt.

    Returns:
        A Namespace with every attribute the orchestrator reads.
    """
    defaults: dict[str, object] = dict(
        ssh_port=None,
        ssh_key=None,
        no_exclude=False,
        yes=True,
        force=False,
        no_snapshot=False,
        snapshot_only=False,
        dry_run=False,
        debug=False,
        allow_unmounted=False,
        require_mount=False,
        allow_nonempty_dest=False,
        allow_massive_delete=False,
        allow_empty_source=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def write_drive_config(
    root: Path,
    *,
    drives: Sequence[str] = DRIVE_IDS,
    backup_order: Sequence[str] | None = None,
) -> Path:
    """Write a drives.toml under ``root`` and return its path.

    The layout is entirely tmp_path-rooted and uses invented drive ids. Only
    the directories the named endpoints resolve to are created; the drives
    themselves are left absent so a test that needs a present drive has to
    create it, and a test that needs a missing one gets a genuinely missing
    one.

    Args:
        root: Directory to write the config and the media tree under.
        drives: Drive ids to configure, in file order.
        backup_order: backup_order tokens. Defaults to the endpoints plus
            every drive.

    Returns:
        The path of the written config file.
    """
    base = root / "media"
    base.mkdir(exist_ok=True)
    (root / "home").mkdir(exist_ok=True)
    (base / SOURCE_DRIVE / "code").mkdir(parents=True, exist_ok=True)
    (base / BACKUP_DRIVE / "home_backup").mkdir(parents=True, exist_ok=True)
    (base / BACKUP_DRIVE / "python_backup").mkdir(parents=True, exist_ok=True)
    order = ["mypython", "*drives", "~"] if backup_order is None else backup_order
    drive_tables = ", ".join(f'{{ id = "{drive}" }}' for drive in drives)
    order_tokens = ", ".join(f'"{token}"' for token in order)
    config = root / "drives.toml"
    config.write_text(
        f"""schema_version = 1
base_dir = "{base}"
drives = [{drive_tables}]
backup_order = [{order_tokens}]

[endpoints."~"]
source = {{ path = "{root / "home"}" }}
dest = {{ drive = "{BACKUP_DRIVE}", path = "home_backup" }}

[endpoints.mypython]
source = {{ drive = "{SOURCE_DRIVE}", path = "code" }}
dest = {{ drive = "{BACKUP_DRIVE}", path = "python_backup" }}
""",
        encoding="utf-8",
    )
    return config


@pytest.fixture
def make_options(tmp_path):
    """Return a factory building Options from a tmp_path-rooted drive config."""

    def _factory(
        *,
        drives: Sequence[str] = DRIVE_IDS,
        backup_order: Sequence[str] | None = None,
        root: Path | None = None,
    ) -> Options:
        config = write_drive_config(
            root or tmp_path, drives=drives, backup_order=backup_order
        )
        return Options.from_drive_config(load_config(config))

    return _factory


@pytest.fixture
def basic_options(make_options):
    """Options backed by an invented, tmp_path-rooted drive config."""
    return make_options()


def tree_signature(root: Path) -> dict[str, tuple[int, bytes]]:
    """Return a {relative_path: (size, content)} dict for asserting tree equivalence."""
    out: dict[str, tuple[int, bytes]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            data = full.read_bytes()
            out[rel] = (len(data), data)
    return out


def inode_for(path: Path) -> int:
    return path.stat().st_ino


_STAND_IN_SSH = """#!/bin/sh
# Stand-in for ssh in tests: drop the options and the host name, then run
# the remote command on this machine through a shell, as sshd would.
while [ $# -gt 0 ]; do
  case "$1" in
    -p|-i|-l|-o) shift 2 ;;
    -*) shift ;;
    *) shift; break ;;
  esac
done
exec sh -c "$*"
"""


@pytest.fixture
def remote_host(tmp_path, monkeypatch):
    """Make ``<host>:/path`` reach this machine, and return the host name.

    Puts a stand-in ``ssh`` first on ``PATH``, so rsync treats the
    destination as remote while the "remote" rsync runs locally. irsync sees
    a remote destination (a string, never a ``Path``), which is the code
    path under test. Skips when this rsync cannot be driven that way, rather
    than failing for a reason that has nothing to do with irsync.
    """
    bindir = tmp_path / "stand-in-bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(_STAND_IN_SSH)
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    probe_src = tmp_path / "ssh-probe-src"
    probe_src.mkdir()
    (probe_src / "probe").write_text("probe")
    probe_dest = tmp_path / "ssh-probe-dest"
    result = subprocess.run(
        ["rsync", "-a", "-e", "ssh", f"{probe_src}/", f"standin:{probe_dest}/"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not (probe_dest / "probe").is_file():
        pytest.skip(
            "this rsync cannot be driven through the stand-in ssh: "
            f"{result.stderr.strip()}"
        )
    return "standin"
