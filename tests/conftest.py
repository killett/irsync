"""Shared fixtures: random trees, options builders, etc."""

import os
import random
from pathlib import Path

import pytest

from irsync.options import Options


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


@pytest.fixture
def basic_options(tmp_path):
    """Build an Options object rooted at tmp_path so tests don't touch the real filesystem layout."""
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
