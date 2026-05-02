from irsync.diff import (
    Changes,
    compute_changes,
    compute_moves,
    index_by_inode,
    make_parent_substituter,
    plan_directory_moves,
    prune_redundant_dir_moves,
)


def _row(*, dev=1, ino, type="f", nlink=1, size=0, path):
    return {
        "dev": dev,
        "ino": ino,
        "type": type,
        "nlink": nlink,
        "size": size,
        "path": path,
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
