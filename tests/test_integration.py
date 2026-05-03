"""End-to-end integration test exercising every reproducible bug from passes 1-5.

Builds a deterministic randomly-nested tree (logic adapted from
``make_random_tree.py``), seeds the special structures every targeted bug
needs (subdir files with reserved-namespace basenames, two dirs that will
later swap, a symlink, a hardlink), runs irsync once to establish the
baseline, then mutates the source tree with one labeled operation per bug
class plus a layer of random fuzz moves (logic adapted from
``make_random_moves.py``), runs irsync a second time, and asserts both the
global property (src tree == dest tree, modulo internal files) and each
bug's specific property.

The two scripts at /workspace/{make_random_tree,make_random_moves}.py stay
as standalone CLI tools; this test inlines their *logic* with a fixed seed
so failures are reproducible.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import shutil
from collections.abc import Iterable
from pathlib import Path

import pytest

from irsync.backup import run_backup
from irsync.options import Options
from irsync.snapshot import (
    LOCKFILE_NAME,
    SNAPSHOT_FILENAME,
    SNAPSHOT_TEMPFILE_PREFIX,
)

from .conftest import inode_for

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Random-tree builder (adapted from make_random_tree.py)
# ---------------------------------------------------------------------------


def _probe_min_size_unit(base_dir: Path) -> int:
    """Return the smallest distinguishable file-size increment on this FS.

    Adapted from make_random_tree.py:_probe_min_size_unit. Almost always 1
    on Linux ext4/btrfs; included for parity with the reference script.
    """
    probe_dir = base_dir / ".probe_size_unit"
    probe_dir.mkdir(parents=True, exist_ok=True)
    observed: list[int] = []
    try:
        for size in range(1, 17):  # 16 probes is plenty to detect granularity
            p = probe_dir / f"probe_{size}.bin"
            p.write_bytes(b"\x00" * size)
            observed.append(int(p.stat().st_size))
        observed = sorted(set(observed))
        diffs = [
            b - a for a, b in zip(observed, observed[1:], strict=False) if (b - a) > 0
        ]
        return max(1, min(diffs) if diffs else 1)
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def _build_random_tree(
    root: Path, *, num_files: int, max_depth: int, seed: int
) -> list[Path]:
    """Create a randomly-nested tree of files with unique sizes under ``root``.

    Adapted from make_random_tree.py. Deterministic given ``seed``. Returns
    the list of created file paths.
    """
    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    unit = _probe_min_size_unit(root)
    multiples = list(range(1, num_files + 1))
    rng.shuffle(multiples)

    created: list[Path] = []
    for i in range(num_files):
        depth = rng.randint(0, max_depth)
        parts = [f"d{rng.randint(0, 4)}_{rng.randint(0, 3)}" for _ in range(depth)]
        sub = root.joinpath(*parts) if parts else root
        sub.mkdir(parents=True, exist_ok=True)
        # Use a stable token so the test is fully reproducible (no secrets.token_hex).
        name = f"f{i:03d}_{rng.randint(10**6, 10**7 - 1):07d}.bin"
        path = sub / name
        path.write_bytes(b"\x00" * (multiples[i] * unit))
        created.append(path)
    return created


# ---------------------------------------------------------------------------
# Random fuzz-move generator (adapted from make_random_moves.py)
# ---------------------------------------------------------------------------


def _scan_tree(base: Path) -> tuple[set[Path], set[Path]]:
    """Return (files, dirs) under ``base`` as absolute paths; dirs includes base."""
    files: set[Path] = set()
    dirs: set[Path] = {base.resolve()}
    for dirpath, dnames, fnames in os.walk(base, followlinks=False):
        r = Path(dirpath).resolve()
        for d in dnames:
            dirs.add((r / d).resolve())
        for f in fnames:
            files.add((r / f).resolve())
    return files, dirs


def _is_descendant(a: Path, b: Path) -> bool:
    try:
        b.relative_to(a)
        return a != b
    except ValueError:
        return False


def _unique_target(
    desired: Path,
    existing_files: set[Path],
    existing_dirs: set[Path],
    rng: random.Random,
) -> Path:
    if desired not in existing_files and desired not in existing_dirs:
        return desired
    parent = desired.parent
    stem = desired.stem
    suffix = desired.suffix
    while True:
        tag = f"_mv{rng.randint(1000, 9999)}"
        cand = parent / f"{stem}{tag}{suffix}"
        if cand not in existing_files and cand not in existing_dirs:
            return cand


def _random_move_plan(
    base: Path,
    *,
    num_moves: int,
    seed: int,
    exclude: Iterable[Path] = (),
) -> list[tuple[Path, Path]]:
    """Generate a deterministic random plan of file/dir moves under ``base``.

    Adapted from make_random_moves.py:_generate_moves. ``exclude`` is the set
    of source paths the fuzz layer must NOT touch (they belong to targeted
    bug-coverage moves whose outcome the test asserts on).
    """
    rng = random.Random(seed)
    files, dirs = _scan_tree(base)
    excluded = {p.resolve() for p in exclude}

    moves: list[tuple[Path, Path]] = []
    attempts = 0
    max_attempts = num_moves * 20 + 50

    def _file_newname(src: Path) -> str:
        stem = src.stem or src.name
        suffix = src.suffix
        return f"{stem}_mv{rng.randint(1000, 9999)}{suffix}"

    def _pick_file_move() -> tuple[Path, Path] | None:
        # Sort to make rng.choice deterministic — set iteration order varies
        # by hash, and tmp_path's random suffix means hashes differ each run.
        candidates = sorted(f for f in files if f not in excluded)
        dest_candidates = sorted(d for d in dirs if d not in excluded)
        if not candidates or not dest_candidates:
            return None
        src = rng.choice(candidates)
        dest_dir = rng.choice(dest_candidates)
        rename = rng.choice([True, False])
        newname = _file_newname(src) if rename else src.name
        # 9th-pass: match make_random_moves.py:135-148 — when the picked
        # combination would be a no-op (same parent dir, same name),
        # re-sample dest_dir + rename up to 10 times before forcing a
        # rename. Increases fuzz density so the seed-99 trajectory
        # exercises more of the move-planning state space.
        if dest_dir == src.parent and newname == src.name:
            tries = 0
            while tries < 10 and dest_dir == src.parent and newname == src.name:
                dest_dir = rng.choice(dest_candidates)
                rename = rng.choice([True, False])
                if rename:
                    newname = _file_newname(src)
                tries += 1
            if dest_dir == src.parent and newname == src.name:
                newname = _file_newname(src)
        desired = (dest_dir / newname).resolve()
        return src, _unique_target(desired, files, dirs, rng)

    def _pick_dir_move() -> tuple[Path, Path] | None:
        candidates = sorted(
            d for d in dirs if d != base.resolve() and d not in excluded
        )
        if not candidates:
            return None
        src = rng.choice(candidates)
        opts = sorted(
            d
            for d in dirs
            if d != src and not _is_descendant(src, d) and d not in excluded
        )
        if not opts:
            return None
        dest_dir = rng.choice(opts)
        rename = rng.choice([True, False])
        new_name = f"{src.name}_mv{rng.randint(1000, 9999)}" if rename else src.name
        # 9th-pass: match make_random_moves.py:169-178 — when the picked
        # combination would be a no-op (same parent, no rename),
        # re-sample dest_dir + rename up to 10 times before forcing a
        # rename. Same reasoning as _pick_file_move.
        if dest_dir == src.parent and not rename:
            tries = 0
            while tries < 10 and dest_dir == src.parent and not rename:
                dest_dir = rng.choice(opts)
                rename = rng.choice([True, False])
                tries += 1
            new_name = f"{src.name}_mv{rng.randint(1000, 9999)}" if rename else src.name
            if dest_dir == src.parent and not rename:
                new_name = f"{src.name}_mv{rng.randint(1000, 9999)}"
        desired = (dest_dir / new_name).resolve()
        return src, _unique_target(desired, files, dirs, rng)

    while len(moves) < num_moves and attempts < max_attempts:
        attempts += 1
        do_file = bool(files) and (not dirs or rng.choice([True, False]))
        choice = _pick_file_move() if do_file else _pick_dir_move()
        if choice is None:
            choice = _pick_dir_move() if do_file else _pick_file_move()
        if choice is None:
            continue
        src, dst = choice
        if src == dst or _is_descendant(src, dst):
            continue
        moves.append((src, dst))
        # Update in-memory sets so subsequent picks are valid.
        if src in files:
            files.remove(src)
            files.add(dst)
        elif src in dirs:
            affected_dirs = {d for d in dirs if d == src or _is_descendant(src, d)}
            affected_files = {f for f in files if _is_descendant(src, f)}
            for d in affected_dirs:
                dirs.discard(d)
                dirs.add((dst / d.relative_to(src)).resolve())
            for f in affected_files:
                files.discard(f)
                files.add((dst / f.relative_to(src)).resolve())
            dirs.add(dst.resolve())
    return moves


def _apply_moves_to_disk(moves: list[tuple[Path, Path]]) -> None:
    """Execute ``moves`` in order, creating parent dirs for the destination."""
    for src, dst in moves:
        if not src.exists() and not src.is_symlink():
            # A previous move may have already relocated this path (e.g. the
            # parent dir was moved as part of an earlier dir move). Skip.
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


_INTERNAL_FILES = {SNAPSHOT_FILENAME, LOCKFILE_NAME}


def _is_internal(rel: str) -> bool:
    name = Path(rel).name
    return name in _INTERNAL_FILES or name.startswith(SNAPSHOT_TEMPFILE_PREFIX)


def _signature_excluding_internal(
    root: Path,
) -> dict[str, tuple[str, int, bytes | None]]:
    """Tree signature that's symlink-safe and skips irsync's reserved files.

    Returns ``{rel: (kind, size, content_or_None)}`` where ``kind`` is one
    of ``"file"`` / ``"symlink"``. Symlinks are recorded by the path string
    they point to (NOT by following them), so a dangling symlink doesn't
    crash the comparison and so genuine symlink-rename behaviour is checked.
    """
    sig: dict[str, tuple[str, int, bytes | None]] = {}
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if _is_internal(rel):
                continue
            if full.is_symlink():
                target = os.readlink(full)
                sig[rel] = ("symlink", len(target), target.encode("utf-8"))
            else:
                data = full.read_bytes()
                sig[rel] = ("file", len(data), data)
    return sig


def _args(**overrides: object) -> argparse.Namespace:
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
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestEndToEndIntegration:
    """One big test exercising every reproducible bug from passes 1-5."""

    def test_random_tree_targeted_moves_then_fuzz_converges_on_dest(
        self,
        tmp_path: Path,
        basic_options: Options,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = tmp_path / "src"
        dest = tmp_path / "dest"

        # 1. Build deterministic random tree (30 files, depth up to 3, seed 42).
        initial_files = _build_random_tree(src, num_files=30, max_depth=3, seed=42)

        # Seed structures the targeted moves will mutate AFTER the first
        # backup. Putting them in the initial tree means the "before"
        # snapshot includes them, so the second-backup diff sees real
        # rename / drop / swap operations rather than create/delete pairs.

        # H5 cycle-break: two dirs that will be swapped between backups.
        (src / "dirA").mkdir()
        (src / "dirA" / "marker_a.txt").write_bytes(b"contents of dirA")
        (src / "dirB").mkdir()
        (src / "dirB" / "marker_b.txt").write_bytes(b"contents of dirB")

        # NEW-H4 symlink rename: symlink lives in baseline, gets renamed later.
        symlink_target = initial_files[0]  # any file in the tree
        (src / "old.lnk").symlink_to(symlink_target)

        # NEW-H4 hardlink path drop: two paths to one inode in baseline; we
        # remove one path between backups.
        hardlink_anchor = initial_files[1]
        (src / "hardlink_b.bin").hardlink_to(hardlink_anchor)

        # 7th-NEW-H1 deleted-symlink: lives in baseline, gets unlinked
        # between backups. Without the type-"l" inclusion in
        # compute_changes' deleted/created comprehensions, an isolated
        # symlink delete would silently short-circuit any_changes() and
        # the dest would keep the stale symlink forever.
        delete_link_target = initial_files[3]
        (src / "to_be_deleted.lnk").symlink_to(delete_link_target)

        # 2. First backup → baseline snapshot + dest tree.
        dest.mkdir()
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        # Sanity: dest mirrors src after the first backup.
        assert _signature_excluding_internal(src) == _signature_excluding_internal(dest)

        # Capture inodes we'll later assert weren't disturbed by the rename
        # optimization.
        rename_only_src_orig = initial_files[5]  # arbitrary file
        rename_only_old_rel = rename_only_src_orig.relative_to(src).as_posix()
        rename_only_dest_inode_before = inode_for(dest / rename_only_old_rel)

        hardlink_anchor_rel = hardlink_anchor.relative_to(src).as_posix()
        # The hardlink anchor should still exist on dest after the b-path drop.

        # 3. Targeted mutations — one labeled per bug class.

        # H1 (anchored excludes): subdir files with reserved-namespace basenames.
        sub_h1 = src / "sub_for_h1"
        sub_h1.mkdir()
        (sub_h1 / SNAPSHOT_FILENAME).write_text("user data 1 (snapshot basename)")
        (sub_h1 / LOCKFILE_NAME).write_text("user data 2 (lockfile basename)")
        (sub_h1 / f"{SNAPSHOT_TEMPFILE_PREFIX}foo").write_text(
            "user data 3 (snap-tempfile basename)"
        )

        # H5 cycle-break: swap dirA <-> dirB (via a temp).
        tmpswap = src / "__swap_tmp__"
        (src / "dirA").rename(tmpswap)
        (src / "dirB").rename(src / "dirA")
        tmpswap.rename(src / "dirB")
        # Note: on a real filesystem the inodes of dirA and dirB are now
        # swapped from the OS perspective; on the next snapshot, irsync's
        # diff sees (dirA's old inode at path dirB, dirB's old inode at
        # path dirA) — a classic cycle that plan_directory_moves must
        # handle via a temp suffix.

        # NEW-H1 (3rd-pass) in-place edit: rewrite content, keep path & inode.
        edit_target = initial_files[10]
        edit_new_content = b"EDITED IN PLACE FOR NEW-H1 (3rd pass)" * 3
        with edit_target.open("wb") as f:
            f.write(edit_new_content)
        edit_rel = edit_target.relative_to(src).as_posix()

        # NEW-M1 (3rd-pass) orphan tempfile cleanup: plant before backup.
        orphan_name = f"{SNAPSHOT_TEMPFILE_PREFIX}orphan_from_killed_run"
        (src / orphan_name).write_text("garbage from a crashed prior _atomic_write")

        # NEW-H3 (4th-pass) rename + edit: rename AND change content.
        re_target = initial_files[15]
        re_old_rel = re_target.relative_to(src).as_posix()
        re_new_rel = "renamed_and_edited_for_new_h3.bin"
        re_target.rename(src / re_new_rel)
        re_new_content = b"NEW CONTENT AT NEW PATH FOR NEW-H3" * 3
        (src / re_new_rel).write_bytes(re_new_content)

        # NEW-H4 (4th-pass) symlink rename.
        (src / "old.lnk").rename(src / "new.lnk")

        # NEW-H4 (4th-pass) hardlink path drop.
        (src / "hardlink_b.bin").unlink()

        # 7th-NEW-H1 added symlink: brand new symlink, no inode in baseline.
        # Without the H1 fix it would be filtered out of created_paths
        # (type "l") and any_changes() would miss it in isolation.
        added_link_target = initial_files[7]
        (src / "freshly_added.lnk").symlink_to(added_link_target)

        # 7th-NEW-H1 deleted symlink: was in baseline, gone now.
        (src / "to_be_deleted.lnk").unlink()

        # NEW-H1 (5th-pass) genuine rename — must keep the optimization.
        # ctime gate requires size + mtime + ctime to ALL match. A pure
        # os.rename preserves all three on local FS, so the optimization
        # MUST fire and the dest inode MUST be preserved.
        rename_only_new_rel = "renamed_for_5th_h1_optimization.bin"
        rename_only_src_orig.rename(src / rename_only_new_rel)

        # Created path.
        created_rel = "freshly_created_for_h4.bin"
        (src / created_rel).write_bytes(b"newly created file")

        # Deleted path. Use a known-name target so we can assert it appears
        # in the H4 preview.
        delete_target = initial_files[20]
        delete_rel = delete_target.relative_to(src).as_posix()
        delete_target.unlink()

        # 4. Random fuzz moves on top — must not touch any targeted path,
        # the symlink's target file, or irsync's reserved root files.
        # `Path.resolve()` follows symlinks, so we use `.absolute()` here
        # for symlinks (we want to refer to the symlink itself, not its
        # target). For everything else `.resolve()` and `.absolute()`
        # agree because no parent is a symlink.
        targeted_paths: set[Path] = {
            # H1: subdir + its three files.
            (src / "sub_for_h1").resolve(),
            *(p.resolve() for p in (src / "sub_for_h1").rglob("*")),
            # H5: cycle-swap dirs and their contents.
            (src / "dirA").resolve(),
            (src / "dirB").resolve(),
            *(p.resolve() for p in (src / "dirA").rglob("*")),
            *(p.resolve() for p in (src / "dirB").rglob("*")),
            # NEW-H1 (3rd): edited file.
            edit_target.resolve(),
            # NEW-M1 (3rd): orphan tempfile (irsync should clean it, fuzz
            # mustn't move it elsewhere first).
            (src / orphan_name).resolve(),
            # NEW-H3: rename-and-edit destination.
            (src / re_new_rel).resolve(),
            # NEW-H4: symlink itself (use .absolute() so we don't follow
            # it) and its target (so the symlink doesn't dangle).
            (src / "new.lnk").absolute(),
            symlink_target.resolve(),
            # NEW-H4: hardlink anchor.
            hardlink_anchor.resolve(),
            # 5th NEW-H1: pure-rename destination.
            (src / rename_only_new_rel).resolve(),
            # 7th-NEW-H1: added symlink itself + its target (mustn't dangle).
            (src / "freshly_added.lnk").absolute(),
            added_link_target.resolve(),
            # 7th-NEW-H1: the now-unlinked deleted-symlink target. The
            # symlink path itself no longer exists, so we exclude its
            # target instead so fuzz can't relocate the file the symlink
            # used to reference (which would change behaviour at the
            # baseline-comparison level).
            delete_link_target.resolve(),
            # Created path.
            (src / created_rel).resolve(),
            # Reserved root files: snapshot and lockfile must stay where
            # they are; moving the snapshot would force a "first backup"
            # fallback that suppresses the H4 preview output the test
            # asserts on.
            (src / SNAPSHOT_FILENAME).resolve(),
            (src / LOCKFILE_NAME).absolute(),  # may not exist between runs
        }
        fuzz_moves = _random_move_plan(
            src, num_moves=8, seed=99, exclude=targeted_paths
        )
        _apply_moves_to_disk(fuzz_moves)

        # 5. Second backup. Capture stdout for the H4 preview-paths assertion.
        capsys.readouterr()  # discard any earlier output
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        captured_out = capsys.readouterr().out

        # 6. Assertions.

        # Global property: src tree (excluding internal files) == dest tree.
        src_sig = _signature_excluding_internal(src)
        dest_sig = _signature_excluding_internal(dest)
        assert src_sig == dest_sig, (
            "src and dest must converge after 2nd backup; "
            f"diff: only-in-src={set(src_sig) - set(dest_sig)!r}, "
            f"only-in-dest={set(dest_sig) - set(src_sig)!r}"
        )

        # H1 (anchored excludes): subdir files with reserved-namespace basenames
        # MUST be on dest with their user content.
        assert (dest / "sub_for_h1" / SNAPSHOT_FILENAME).read_text() == (
            "user data 1 (snapshot basename)"
        )
        assert (dest / "sub_for_h1" / LOCKFILE_NAME).read_text() == (
            "user data 2 (lockfile basename)"
        )
        assert (dest / "sub_for_h1" / f"{SNAPSHOT_TEMPFILE_PREFIX}foo").read_text() == (
            "user data 3 (snap-tempfile basename)"
        )

        # H4 (preview lists paths): created + deleted paths in stdout under --yes.
        assert created_rel in captured_out, (
            f"created path {created_rel!r} must appear in --yes preview "
            "(AD-7 audit trail / H4)"
        )
        assert delete_rel in captured_out, (
            f"deleted path {delete_rel!r} must appear in --yes preview "
            "(H4 deletion-list visibility)"
        )

        # H5 (cycle-break): contents at swapped paths on dest.
        assert (dest / "dirA" / "marker_b.txt").read_bytes() == b"contents of dirB"
        assert (dest / "dirB" / "marker_a.txt").read_bytes() == b"contents of dirA"

        # NEW-H1 (3rd) in-place edit reflected on dest.
        assert (dest / edit_rel).read_bytes() == edit_new_content

        # NEW-H2 (3rd) lockfile NOT transferred to dest root.
        assert not (dest / LOCKFILE_NAME).exists(), (
            "<dest>/.irsync.lock must not exist — lockfile is excluded by "
            "anchored rsync exclude"
        )

        # NEW-M1 (3rd) orphan tempfile cleaned up from src; never on dest.
        assert not (src / orphan_name).exists(), (
            "orphan .irsync-snap-* tempfile must be cleaned at startup"
        )
        assert not (dest / orphan_name).exists()

        # NEW-H3 (4th) rename + edit converged at new path with new content.
        assert (dest / re_new_rel).read_bytes() == re_new_content
        assert not (dest / re_old_rel).exists(), (
            f"old path {re_old_rel!r} must be removed from dest by --delete-before"
        )

        # NEW-H4 (4th) symlink rename: new path exists as symlink, old gone.
        assert (dest / "new.lnk").is_symlink()
        assert not (dest / "old.lnk").exists()

        # NEW-H4 (4th) hardlink path drop: dropped path gone, anchor stays.
        assert not (dest / "hardlink_b.bin").exists()
        assert (dest / hardlink_anchor_rel).exists()

        # NEW-H1 (5th) genuine rename: dest inode preserved (rename
        # optimization survived the new ctime gate).
        assert (dest / rename_only_new_rel).exists()
        assert inode_for(dest / rename_only_new_rel) == rename_only_dest_inode_before, (
            "genuine rename must keep dest inode — rename optimization should "
            "survive the 5th-pass ctime tiebreaker for unmodified files"
        )

        # Created + deleted: present / absent on dest as expected.
        assert (dest / created_rel).read_bytes() == b"newly created file"
        assert not (dest / delete_rel).exists()

        # 7th-NEW-H1: brand-new symlink reached dest, deleted symlink is
        # gone from dest. Without the type-"l" inclusion in
        # compute_changes' deleted/created comprehensions, an isolated
        # symlink change would silently short-circuit any_changes(); here
        # the symlink change rides alongside other mutations so the gate
        # fires regardless, but the symmetric add/remove behaviour is
        # still worth pinning in the umbrella.
        assert (dest / "freshly_added.lnk").is_symlink(), (
            "added symlink must be transferred to dest"
        )
        assert not (dest / "to_be_deleted.lnk").exists(), (
            "deleted symlink must be removed from dest by --delete-before"
        )


class TestSeventhPassIntegrationCoverage:
    """Standalone end-to-end tests for previously-fixed bugs that the umbrella
    test doesn't exercise: cross-device replay refusal (5th NEW-H2) and the
    ``--no-snapshot --yes`` audit-trail invariant (5th NEW-M1)."""

    def test_7th_xdev_refused_via_real_preflight_no_snapshot_persisted(
        self,
        tmp_path: Path,
        basic_options: Options,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The unit test at test_backup.py monkeypatches apply_moves directly,
        # which bypasses the actual _preflight_check_xdev code. Here we
        # patch one level deeper — _dev_of returns a different st_dev for
        # one of the planned paths — so the real pre-flight runs and
        # raises CrossDeviceMoveError. Confirms the orchestrator catches
        # it, returns non-zero, doesn't run rsync, and leaves the source
        # snapshot at its old baseline so a corrected re-run still has a
        # clean diff.
        from pathlib import Path as _Path

        from irsync import replay as replay_mod

        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        (src / "to_rename.bin").write_bytes(b"will be renamed")
        (src / "stable.bin").write_bytes(b"unchanged content")
        dest.mkdir()

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        snap_path = src / SNAPSHOT_FILENAME
        baseline_bytes = snap_path.read_bytes()

        # Rename so the next run plans a file move and apply_moves runs.
        (src / "to_rename.bin").rename(src / "renamed.bin")

        real_dev_of = replay_mod._dev_of
        dest_root_dev = dest.stat().st_dev

        def fake_dev_of(p: _Path) -> int:
            real = real_dev_of(p)
            # Simulate that the renamed file's dest path is on another mount.
            if "renamed.bin" in str(p):
                return real + 9999  # different from dest_root_dev
            return dest_root_dev if real == dest_root_dev else real

        monkeypatch.setattr(replay_mod, "_dev_of", fake_dev_of)

        rsync_calls: list[list[str]] = []

        def _record_rsync(cmd: list[str]) -> int:
            rsync_calls.append(cmd)
            return 0

        monkeypatch.setattr("irsync.backup.run_real_sync", _record_rsync)

        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc != 0, "xdev refusal must surface as non-zero"
        assert rsync_calls == [], (
            "rsync must not run after the pre-flight refused the replay"
        )
        assert snap_path.read_bytes() == baseline_bytes, (
            "snapshot must NOT be persisted after a refused replay so the "
            "corrected re-run has the right baseline"
        )

    def test_7th_h1_isolated_symlink_add_then_delete_reaches_dest(
        self,
        tmp_path: Path,
        basic_options: Options,
    ) -> None:
        # 7th-NEW-H1 end-to-end isolation: with NO other mutations, only
        # symlink changes between backups. Before the type-"l" fix to
        # compute_changes, an isolated symlink add would leave
        # any_changes()=False and the backup would short-circuit, leaving
        # dest stale. This test fails closed: any regression that drops
        # "l" from the comprehension breaks the second assertion.
        src = tmp_path / "src_h1"
        dest = tmp_path / "dest_h1"
        src.mkdir()
        (src / "real_file.bin").write_bytes(b"target content")
        dest.mkdir()

        # Baseline.
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        assert _signature_excluding_internal(src) == _signature_excluding_internal(dest)

        # Mutation: ONLY a brand-new symlink. Nothing else changes. If
        # any_changes() spuriously returns False here, the backup
        # short-circuits and the symlink never reaches dest.
        (src / "isolated_link.lnk").symlink_to(src / "real_file.bin")
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        assert (dest / "isolated_link.lnk").is_symlink(), (
            "isolated symlink add must reach dest — H1 regression check"
        )

        # Mutation: ONLY delete the symlink. Same isolation as above.
        (src / "isolated_link.lnk").unlink()
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(),
        )
        assert rc == 0
        assert not (dest / "isolated_link.lnk").exists(), (
            "isolated symlink delete must propagate to dest — H1 regression check"
        )

    def test_7th_no_snapshot_yes_prints_dry_run_preview_to_stdout(
        self,
        tmp_path: Path,
        basic_options: Options,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # 5th-NEW-M1 end-to-end: --no-snapshot --yes (the cron mode that
        # skips the inode optimization entirely) must still print the
        # rsync dry-run preview to stdout so a cron job's log captures
        # what was about to be transferred. The unit test at
        # test_backup.py:test_5th_m1_no_snapshot_yes_prints_dry_run_preview_to_stdout
        # covers this with mocked rsync; here we want the same invariant
        # with a real source tree but a stub rsync (we don't actually want
        # to install rsync as a CI dep), to confirm the integration-level
        # flow.
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        (src / "alpha.bin").write_bytes(b"alpha")
        (src / "beta.bin").write_bytes(b"beta")
        dest.mkdir()

        # Stub the dry-run output so we don't depend on a real rsync; the
        # invariant we care about is that this output reaches stdout under
        # --yes (cron path), not the specific text rsync would have
        # produced.
        sentinel = "RSYNC_DRY_RUN_SENTINEL_alpha.bin\n"

        def fake_dry_run(cmd: list[str]) -> str:
            return sentinel

        def fake_real_sync(cmd: list[str]) -> int:
            return 0

        monkeypatch.setattr("irsync.backup.run_dry_run", fake_dry_run)
        monkeypatch.setattr("irsync.backup.run_real_sync", fake_real_sync)

        capsys.readouterr()  # discard any earlier output
        rc = run_backup(
            source_arg=str(src),
            destination_arg=str(dest),
            options=basic_options,
            args=_args(no_snapshot=True),
        )
        assert rc == 0
        captured = capsys.readouterr().out
        assert sentinel in captured, (
            "--no-snapshot --yes must print the rsync dry-run preview to "
            "stdout so cron logs capture the audit trail (AD-7)"
        )
