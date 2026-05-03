from irsync.diff import (
    Changes,
    compute_changes,
    compute_moves,
    index_by_inode,
    make_parent_substituter,
    plan_directory_moves,
    prune_redundant_dir_moves,
)


def _row(
    *,
    dev=1,
    ino,
    type="f",
    nlink=1,
    size=0,
    path,
    mtime_ns=1_000_000_000,
    btime_ns=1_000_000_000,
):
    return {
        "dev": dev,
        "ino": ino,
        "type": type,
        "nlink": nlink,
        "size": size,
        "path": path,
        "mtime_ns": mtime_ns,
        "btime_ns": btime_ns,
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
        dir_moves, file_moves, _ = compute_moves(
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
        dir_moves, file_moves, _ = compute_moves(before_idx, after_idx)
        assert dir_moves == [("oldDir", "newDir")]
        # File move IS reported by compute_moves; suppression happens later via the helper.
        assert file_moves == [("oldDir/inside.txt", "newDir/inside.txt")]

    def test_unchanged_paths_omitted(self):
        before = [_row(ino=30, path="same.txt")]
        after = [_row(ino=30, path="same.txt")]
        dir_moves, file_moves, _ = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert dir_moves == []
        assert file_moves == []

    def test_created_inodes_not_reported_as_moves(self):
        before = []
        after = [_row(ino=40, path="new_only.txt")]
        dir_moves, file_moves, _ = compute_moves(
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
        dir_moves, file_moves, _ = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert file_moves == [], (
            f"inode reuse must not produce a move; got {file_moves!r}"
        )

    def test_inode_reuse_with_same_size_but_different_mtime_not_a_move(self):
        # Same size by coincidence; mtime differs → not a move.
        before = [_row(ino=100, size=42, mtime_ns=1_000, path="old.txt")]
        after = [_row(ino=100, size=42, mtime_ns=2_000, path="other.txt")]
        dir_moves, file_moves, _ = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert file_moves == [], "same size but different mtime should not be a move"

    def test_genuine_rename_same_size_same_mtime_is_a_move(self):
        # Pure rename: same inode, same size, same mtime_ns.
        before = [_row(ino=100, size=42, mtime_ns=1_500, path="before.txt")]
        after = [_row(ino=100, size=42, mtime_ns=1_500, path="after.txt")]
        dir_moves, file_moves, _ = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert file_moves == [("before.txt", "after.txt")]

    def test_5th_h1_inode_reuse_with_matching_size_and_mtime_blocked_by_btime(self):
        # NEW-H1 (5th-pass, take 2): the NFS data-loss scenario. Source on
        # a network FS with second-granular mtime. Old file deleted; kernel
        # reuses inode; new file at a different path with the SAME size and
        # the SAME (rounded) mtime_ns. The btime tiebreaker catches it
        # because a freshly-allocated inode always has a fresh btime.
        before = [
            _row(
                ino=100,
                size=1000,
                mtime_ns=1_234_567_890_000_000_000,  # second-aligned
                btime_ns=1_234_567_800_000_000_000,  # original allocation time
                path="A.txt",
            )
        ]
        after = [
            _row(
                ino=100,
                size=1000,
                mtime_ns=1_234_567_890_000_000_000,  # same second
                btime_ns=1_234_567_890_500_000_000,  # newly allocated
                path="B.txt",
            )
        ]
        dir_moves, file_moves, _ = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert file_moves == [], (
            "btime mismatch must block the false move (NFS inode-reuse defense)"
        )

    def test_5th_h1_legacy_snapshot_without_btime_falls_back_to_size_mtime_gate(self):
        # When EITHER snapshot lacks btime (legacy snapshots, or filesystems
        # like NFSv3/FAT/older-ext4 that don't expose statx btime), the gate
        # falls back to size + mtime alone. A genuine rename with matching
        # size + mtime is still optimized; the NFS inode-reuse hole is
        # documented in the README for these filesystems.
        before = [_row(ino=110, size=42, mtime_ns=1_500, btime_ns=-1, path="old.txt")]
        after = [_row(ino=110, size=42, mtime_ns=1_500, btime_ns=2_000, path="new.txt")]
        dir_moves, file_moves, _ = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert file_moves == [("old.txt", "new.txt")], (
            "missing btime on either side must not block a size+mtime-clean move"
        )

    def test_5th_h1_genuine_rename_with_matching_btime_still_a_move(self):
        # Sanity: a real rename preserves size, mtime, AND btime. The
        # optimization survives.
        before = [
            _row(ino=120, size=42, mtime_ns=1_500, btime_ns=1_000, path="before.txt")
        ]
        after = [
            _row(ino=120, size=42, mtime_ns=1_500, btime_ns=1_000, path="after.txt")
        ]
        dir_moves, file_moves, _ = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert file_moves == [("before.txt", "after.txt")]

    def test_5th_h1_cross_clock_tick_rename_still_optimized(self):
        # Regression test for the bug that the broken ctime gate would have
        # caused: a real rename advances ctime even though btime, size, and
        # mtime are all preserved. The new gate only requires btime to match
        # (in addition to size + mtime), so cross-clock-tick renames must
        # still optimize. If a future change reintroduces a ctime-style
        # always-ticking field into the gate, this test will fail.
        before = [_row(ino=130, size=99, mtime_ns=42, btime_ns=10, path="docs/old.md")]
        after = [
            _row(ino=130, size=99, mtime_ns=42, btime_ns=10, path="reports/new.md")
        ]
        dir_moves, file_moves, _ = compute_moves(
            index_by_inode(before), index_by_inode(after)
        )
        assert file_moves == [("docs/old.md", "reports/new.md")]

    def test_hardlinks_skipped_by_default(self):
        before = [_row(ino=50, nlink=2, path="a"), _row(ino=50, nlink=2, path="link")]
        after = [_row(ino=50, nlink=2, path="b"), _row(ino=50, nlink=2, path="link")]
        dir_moves, file_moves, _ = compute_moves(
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

    def test_rename_and_edit_falls_back_to_create_plus_delete(self):
        # NEW-H3 regression: same inode, path differs, AND size/mtime differ.
        # compute_moves correctly refuses the move (could be inode reuse),
        # but the path change MUST still appear in deleted/created so rsync
        # actually removes the old path on dest and creates the new one.
        # Otherwise the dest is left with the old content at the old path.
        before = [_row(ino=800, size=50, mtime_ns=1_000, path="foo.txt")]
        after = [_row(ino=800, size=75, mtime_ns=2_000, path="bar.txt")]
        changes = compute_changes(before, after)
        assert changes.file_moves == [], "rename refused due to size/mtime mismatch"
        assert "foo.txt" in changes.deleted, (
            "old path must appear in deleted so rsync removes it from dest"
        )
        assert "bar.txt" in changes.created, (
            "new path must appear in created so rsync transfers it"
        )
        assert changes.any_changes(), "rename+edit must NOT short-circuit the backup"

    def test_pure_rename_still_optimizes_as_a_move(self):
        # Sanity: with the H3 fallback in place, the genuine-rename optimization
        # must still work — same inode, paths differ, size and mtime match.
        before = [_row(ino=801, size=50, mtime_ns=1_500, path="old.txt")]
        after = [_row(ino=801, size=50, mtime_ns=1_500, path="new.txt")]
        changes = compute_changes(before, after)
        assert changes.file_moves == [("old.txt", "new.txt")]
        assert "old.txt" not in changes.deleted
        assert "new.txt" not in changes.created

    def test_symlink_rename_appears_in_deleted_and_created(self):
        # NEW-H4: symlinks are skipped by compute_moves (include_symlinks=False),
        # but a renamed symlink still represents a real path change. The old
        # path must end up in deleted and the new in created.
        before = [_row(ino=802, type="l", size=5, mtime_ns=1_000, path="old.lnk")]
        after = [_row(ino=802, type="l", size=5, mtime_ns=1_000, path="new.lnk")]
        changes = compute_changes(before, after)
        assert "old.lnk" in changes.deleted
        assert "new.lnk" in changes.created

    def test_hardlink_path_drop_appears_in_deleted(self):
        # NEW-H4: hardlinked file with one of its paths removed. Inode stays
        # in both indices; compute_moves skips hardlinks; but the dropped
        # path must show up in deleted so rsync removes it from dest.
        before = [
            _row(ino=803, nlink=2, path="a"),
            _row(ino=803, nlink=2, path="b"),
        ]
        after = [_row(ino=803, nlink=1, path="a")]
        changes = compute_changes(before, after)
        assert "b" in changes.deleted, "removed hardlink path must appear in deleted"
        assert "a" not in changes.deleted, (
            "kept hardlink path must NOT appear in deleted"
        )
