from irsync.diff import (
    Changes,
    compute_changes,
    compute_moves,
    index_by_inode,
    make_parent_substituter,
    plan_directory_moves,
    prune_redundant_dir_moves,
)


def _row(*, dev=1, ino, type="f", nlink=1, size=0, path, mtime_ns=1_000_000_000):
    return {
        "dev": dev,
        "ino": ino,
        "type": type,
        "nlink": nlink,
        "size": size,
        "path": path,
        "mtime_ns": mtime_ns,
    }


class TestIndexByInode:
    def test_groups_by_dev_ino(self):
        rows = [
            _row(ino=1, path="a"),
            _row(ino=1, path="a-hardlink"),
            _row(ino=2, path="b"),
        ]
        idx = index_by_inode(rows)
        assert set(idx[(1, 1)]["paths"]) == {"a", "a-hardlink"}
        assert idx[(1, 2)]["paths"] == {"b"}


class TestComputeMoves:
    def test_simple_file_rename(self):
        before = [_row(ino=10, path="old.txt")]
        after = [_row(ino=10, path="new.txt")]
        dir_moves, file_moves = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert dir_moves == []
        assert file_moves == [("old.txt", "new.txt")]

    def test_dir_rename_supersedes_file_moves(self):
        before = [
            _row(ino=20, type="d", path="oldDir"),
            _row(ino=21, path="oldDir/inside.txt"),
        ]
        after = [
            _row(ino=20, type="d", path="newDir"),
            _row(ino=21, path="newDir/inside.txt"),
        ]
        before_idx = index_by_inode(before)
        after_idx = index_by_inode(after)
        dir_moves, file_moves = compute_moves(before_idx, after_idx)
        assert dir_moves == [("oldDir", "newDir")]
        # File move IS reported by compute_moves; suppression happens later via the helper.
        assert file_moves == [("oldDir/inside.txt", "newDir/inside.txt")]

    def test_unchanged_paths_omitted(self):
        before = [_row(ino=30, path="same.txt")]
        after = [_row(ino=30, path="same.txt")]
        dir_moves, file_moves = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert dir_moves == []
        assert file_moves == []

    def test_created_inodes_not_reported_as_moves(self):
        before = []
        after = [_row(ino=40, path="new_only.txt")]
        dir_moves, file_moves = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert dir_moves == [] and file_moves == []

    def test_inode_reuse_with_different_size_not_a_move(self):
        # Inode 100 was 'old.txt' (size 50). Source FS reused inode 100 for
        # 'unrelated.bin' (size 999) after old.txt was deleted. This MUST NOT
        # be reported as a move; doing so would cause apply_moves to clobber
        # the legitimate dest file at the new path.
        before = [_row(ino=100, size=50, mtime_ns=1_000, path="old.txt")]
        after = [_row(ino=100, size=999, mtime_ns=2_000, path="unrelated.bin")]
        dir_moves, file_moves = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert file_moves == [], (
            f"inode reuse must not produce a move; got {file_moves!r}"
        )

    def test_inode_reuse_with_same_size_but_different_mtime_not_a_move(self):
        # Same size by coincidence; mtime differs → not a move.
        before = [_row(ino=100, size=42, mtime_ns=1_000, path="old.txt")]
        after = [_row(ino=100, size=42, mtime_ns=2_000, path="other.txt")]
        dir_moves, file_moves = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert file_moves == [], "same size but different mtime should not be a move"

    def test_genuine_rename_same_size_same_mtime_is_a_move(self):
        # Pure rename: same inode, same size, same mtime_ns.
        before = [_row(ino=100, size=42, mtime_ns=1_500, path="before.txt")]
        after = [_row(ino=100, size=42, mtime_ns=1_500, path="after.txt")]
        dir_moves, file_moves = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert file_moves == [("before.txt", "after.txt")]

    def test_hardlinks_skipped_by_default(self):
        before = [_row(ino=50, nlink=2, path="a"), _row(ino=50, nlink=2, path="link")]
        after = [_row(ino=50, nlink=2, path="b"), _row(ino=50, nlink=2, path="link")]
        dir_moves, file_moves = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert file_moves == []


class TestMakeParentSubstituter:
    def test_rewrites_path_under_moved_dir(self):
        subst = make_parent_substituter([("oldDir", "newDir")])
        assert subst("oldDir/inside.txt") == "newDir/inside.txt"

    def test_leaves_unrelated_path_alone(self):
        subst = make_parent_substituter([("oldDir", "newDir")])
        assert subst("other/file.txt") == "other/file.txt"


class TestPruneRedundantDirMoves:
    def test_drops_child_dir_move_covered_by_parent(self):
        moves = [("a", "z"), ("a/b", "z/b")]
        kept = prune_redundant_dir_moves(moves)
        assert kept == [("a", "z")]


class TestPlanDirectoryMoves:
    def test_simple_chain_orders_correctly(self):
        # Renaming a -> b. No cycle.
        ordered = plan_directory_moves([("a", "b")])
        assert ordered == [("a", "b")]

    def test_swap_breaks_cycle_with_temp(self):
        # Swap two directory names: a <-> b
        ordered = plan_directory_moves([("a", "b"), ("b", "a")])
        # Must produce a sequence that doesn't clobber: e.g. b->tmp, a->b, tmp->a
        # Validate by simulating execution against a name set.
        names = {"a", "b"}
        for src, dst in ordered:
            assert src in names
            assert dst not in names
            names.remove(src)
            names.add(dst)
        assert names == {"a", "b"}

    def test_temp_name_uses_random_suffix(self):
        # H5 regression: the cycle-breaking temp name must be unpredictable so it
        # can't collide with a real file on the destination tree (e.g. a backup
        # of in-progress work that happens to contain '<dir>.__mvtmp__'). Two
        # planner runs over the same swap should produce different temp names.
        import re

        runs = [plan_directory_moves([("a", "b"), ("b", "a")]) for _ in range(20)]
        # Extract the temp-suffixed name from each run
        temp_pat = re.compile(r"\.__mvtmp__([0-9a-f]{8,})")
        suffixes = set()
        for ordered in runs:
            for src, dst in ordered:
                m = temp_pat.search(src) or temp_pat.search(dst)
                if m:
                    suffixes.add(m.group(1))
                    break
        assert len(suffixes) > 1, (
            f"temp suffix must be randomized to avoid dest-tree collisions; "
            f"got only {len(suffixes)} unique suffix(es) across 20 runs"
        )


class TestComputeChanges:
    def test_returns_changes_with_created_and_deleted(self):
        before = [
            _row(ino=100, path="kept.txt"),
            _row(ino=101, path="moved-from.txt"),
            _row(ino=102, path="will-be-deleted.txt"),
        ]
        after = [
            _row(ino=100, path="kept.txt"),
            _row(ino=101, path="moved-to.txt"),
            _row(ino=103, path="newly-created.txt"),
        ]
        changes = compute_changes(before, after)
        assert isinstance(changes, Changes)
        assert changes.file_moves == [("moved-from.txt", "moved-to.txt")]
        assert changes.created == ["newly-created.txt"]
        assert changes.deleted == ["will-be-deleted.txt"]
        assert changes.dir_moves == []

    def test_no_changes_returns_all_empty(self):
        before = [_row(ino=200, path="a.txt")]
        after = [_row(ino=200, path="a.txt")]
        changes = compute_changes(before, after)
        assert not changes.any_changes()

    def test_any_changes_true_when_anything_present(self):
        before = []
        after = [_row(ino=300, path="new.txt")]
        changes = compute_changes(before, after)
        assert changes.any_changes()

    def test_in_place_modification_reported_as_modified(self):
        # NEW-H1 regression: same path, same inode, different size/mtime means the
        # file was edited in place. compute_changes MUST report this so the
        # backup runs; otherwise the dest never gets the new content.
        before = [_row(ino=400, size=10, mtime_ns=1_000, path="report.txt")]
        after = [_row(ino=400, size=20, mtime_ns=2_000, path="report.txt")]
        changes = compute_changes(before, after)
        assert changes.modified == ["report.txt"]
        assert changes.any_changes(), (
            "modification-only diff must NOT trigger the no-changes short-circuit"
        )

    def test_unchanged_file_not_reported_as_modified(self):
        # Same inode, same path, same size, same mtime → nothing changed.
        before = [_row(ino=500, size=10, mtime_ns=1_000, path="stable.txt")]
        after = [_row(ino=500, size=10, mtime_ns=1_000, path="stable.txt")]
        changes = compute_changes(before, after)
        assert changes.modified == []
        assert not changes.any_changes()

    def test_directory_mtime_change_does_not_report_modified(self):
        # A dir's mtime ticks whenever entries inside change. Reporting that as
        # "modified" would just duplicate what created/deleted/file_moves already
        # show. Skip dirs in the modification check.
        before = [_row(ino=600, type="d", size=0, mtime_ns=1_000, path="d")]
        after = [_row(ino=600, type="d", size=4096, mtime_ns=2_000, path="d")]
        changes = compute_changes(before, after)
        assert changes.modified == []

    def test_unknown_mtime_treated_as_modified(self):
        # Conservative: if mtime is unknown on either side (legacy snapshot
        # sentinel -1), assume modified and let rsync sort it out.
        before = [_row(ino=700, size=10, mtime_ns=-1, path="legacy.txt")]
        after = [_row(ino=700, size=10, mtime_ns=2_000, path="legacy.txt")]
        changes = compute_changes(before, after)
        assert changes.modified == ["legacy.txt"]
