import json
import os

import pytest

from irsync.snapshot import (
    LOCKFILE_NAME,
    SNAPSHOT_FILENAME,
    SNAPSHOT_TEMPFILE_PREFIX,
    SnapshotMismatch,
    read_jsonl,
    read_snapshot,
    snapshot_tree,
    write_jsonl,
    write_snapshot,
)


def _build_tree(root):
    (root / "a").mkdir()
    (root / "a" / "file1.txt").write_text("hello")
    (root / "b").mkdir()
    (root / "b" / "c").mkdir()
    (root / "b" / "c" / "file2.txt").write_text("world")


class TestSnapshotTree:
    def test_includes_root_as_dot(self, tmp_path):
        rows = snapshot_tree(tmp_path)
        paths = [r["path"] for r in rows]
        assert "." in paths

    def test_captures_files_and_dirs(self, tmp_path):
        _build_tree(tmp_path)
        rows = snapshot_tree(tmp_path)
        paths = {r["path"] for r in rows}
        assert paths >= {".", "a", "a/file1.txt", "b", "b/c", "b/c/file2.txt"}

    def test_each_row_has_required_keys(self, tmp_path):
        _build_tree(tmp_path)
        rows = snapshot_tree(tmp_path)
        for r in rows:
            assert set(r.keys()) >= {"dev", "ino", "type", "nlink", "size", "path"}

    def test_file_type_is_f_dir_is_d(self, tmp_path):
        _build_tree(tmp_path)
        rows = snapshot_tree(tmp_path)
        by_path = {r["path"]: r for r in rows}
        assert by_path["a"]["type"] == "d"
        assert by_path["a/file1.txt"]["type"] == "f"

    def test_excludes_snapshot_file_at_root(self, tmp_path):
        (tmp_path / SNAPSHOT_FILENAME).write_text("[]")
        (tmp_path / "real.txt").write_text("x")
        rows = snapshot_tree(tmp_path)
        paths = {r["path"] for r in rows}
        assert SNAPSHOT_FILENAME not in paths
        assert "real.txt" in paths

    def test_includes_same_named_file_in_subdir(self, tmp_path):
        # Only the root-level snapshot file is excluded.
        (tmp_path / "sub").mkdir()
        nested = f"sub/{SNAPSHOT_FILENAME}"
        (tmp_path / "sub" / SNAPSHOT_FILENAME).write_text("[]")
        rows = snapshot_tree(tmp_path)
        paths = {r["path"] for r in rows}
        assert nested in paths

    def test_excludes_lockfile_at_root(self, tmp_path):
        # NEW-H2: the lockfile is created by irsync at the source root before
        # snapshot_tree runs; it must not appear in the snapshot rows or its
        # mtime ticking would defeat the no-changes short-circuit.
        (tmp_path / LOCKFILE_NAME).write_text("")
        (tmp_path / "real.txt").write_text("x")
        rows = snapshot_tree(tmp_path)
        paths = {r["path"] for r in rows}
        assert LOCKFILE_NAME not in paths
        assert "real.txt" in paths

    def test_includes_lockfile_named_file_in_subdir(self, tmp_path):
        # Subdir files with the lockfile basename are still user data.
        (tmp_path / "sub").mkdir()
        nested = f"sub/{LOCKFILE_NAME}"
        (tmp_path / "sub" / LOCKFILE_NAME).write_text("user content")
        rows = snapshot_tree(tmp_path)
        paths = {r["path"] for r in rows}
        assert nested in paths

    def test_excludes_snap_tempfile_pattern_at_root(self, tmp_path):
        # NEW-M1: orphan .irsync-snap-* tempfiles from a killed
        # _atomic_write_snapshot must not be included in subsequent snapshots.
        (tmp_path / f"{SNAPSHOT_TEMPFILE_PREFIX}abc123").write_text("")
        (tmp_path / f"{SNAPSHOT_TEMPFILE_PREFIX}def456").write_text("")
        (tmp_path / "real.txt").write_text("x")
        rows = snapshot_tree(tmp_path)
        paths = {r["path"] for r in rows}
        assert not any(p.startswith(SNAPSHOT_TEMPFILE_PREFIX) for p in paths)
        assert "real.txt" in paths

    def test_includes_snap_tempfile_pattern_in_subdir(self, tmp_path):
        # Subdir files matching the tempfile prefix are still user data.
        (tmp_path / "sub").mkdir()
        nested = f"sub/{SNAPSHOT_TEMPFILE_PREFIX}xyz"
        (tmp_path / "sub" / f"{SNAPSHOT_TEMPFILE_PREFIX}xyz").write_text("user")
        rows = snapshot_tree(tmp_path)
        paths = {r["path"] for r in rows}
        assert nested in paths

    def test_does_not_follow_symlinks(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        (target / "inner.txt").write_text("x")
        link = tmp_path / "link"
        os.symlink(target, link)
        rows = snapshot_tree(tmp_path)
        paths = {r["path"] for r in rows}
        # Symlink itself appears, but its target is not walked through it.
        assert "link" in paths
        assert "link/inner.txt" not in paths

    def test_missing_root_raises(self, tmp_path):
        with pytest.raises(SystemExit):
            snapshot_tree(tmp_path / "nope")


class TestWriteReadRoundtrip:
    def test_roundtrip(self, tmp_path):
        _build_tree(tmp_path)
        rows = snapshot_tree(tmp_path)
        out = tmp_path.parent / "snap.jsonl"
        write_jsonl(rows, out)
        loaded = read_jsonl(out)
        assert loaded == rows

    def test_jsonl_format_one_per_line(self, tmp_path):
        _build_tree(tmp_path)
        rows = snapshot_tree(tmp_path)
        out = tmp_path.parent / "snap.jsonl"
        write_jsonl(rows, out)
        lines = out.read_text(encoding="utf-8").splitlines()
        assert len(lines) == len(rows)
        for line in lines:
            obj = json.loads(line)
            assert "ino" in obj

    def test_read_rejects_missing_keys(self, tmp_path):
        out = tmp_path / "bad.jsonl"
        out.write_text('{"dev":1,"ino":2}\n', encoding="utf-8")
        with pytest.raises(SystemExit):
            read_jsonl(out)

    def test_7th_m2_read_jsonl_rejects_invalid_type_value(self, tmp_path):
        # 7th-M2: a corrupted snapshot row with an unknown type ("x", "")
        # was silently appended; later compute_changes filtered it out via
        # the type whitelist, hiding the corruption. read_jsonl must reject
        # it up front so a corrupted snapshot file can't cause silent
        # invisibility of inodes during the diff.
        out = tmp_path / "bad_type.jsonl"
        out.write_text(
            '{"dev":1,"ino":2,"type":"x","nlink":1,"size":0,"path":"a"}\n',
            encoding="utf-8",
        )
        with pytest.raises(SystemExit):
            read_jsonl(out)

    def test_7th_m2_read_snapshot_rejects_invalid_type_value(self, tmp_path):
        # Same defense as the read_jsonl variant, but for the headered
        # read_snapshot path that the orchestrator actually uses.
        out = tmp_path / "bad_type.jsonl"
        header_line = (
            '{"_meta":{"source_root":"' + str(tmp_path.resolve()) + '",'
            '"irsync_version":"x","created_at_utc":"x"}}\n'
        )
        bad_row = '{"dev":1,"ino":2,"type":"q","nlink":1,"size":0,"path":"a","mtime_ns":0,"btime_ns":-1}\n'
        out.write_text(header_line + bad_row, encoding="utf-8")
        with pytest.raises(SystemExit):
            read_snapshot(out, expected_source_root=tmp_path)


class TestBtimeField:
    def test_5th_h1_snapshot_records_btime_ns_per_row(self, tmp_path):
        # NEW-H1 (5th-pass, take 2): btime_ns is the inode-reuse tiebreaker
        # — set when the inode is allocated, never updated by rename. The
        # snapshot must populate it via statx so compute_moves can require
        # it to match before declaring a move. On filesystems that don't
        # report btime, snapshot_tree records -1; that disables the
        # tiebreaker without breaking the rest of the diff.
        _build_tree(tmp_path)
        rows = snapshot_tree(tmp_path)
        for r in rows:
            assert "btime_ns" in r, f"row missing btime_ns: {r}"
            assert isinstance(r["btime_ns"], int)
            # Either FS supports btime (positive ns since epoch) or doesn't (-1).
            assert r["btime_ns"] > 0 or r["btime_ns"] == -1

    def test_5th_h1_legacy_snapshot_without_btime_reads_back_with_sentinel(
        self, tmp_path
    ):
        # Snapshots written before this change have no btime_ns. read_jsonl
        # must default missing field to -1 so compute_moves falls back to
        # the size+mtime-only gate (no NFS defense for these legacy
        # snapshots, but no rename-optimization regression either).
        out = tmp_path / "legacy.jsonl"
        # Legacy row: dev/ino/type/nlink/size/mtime_ns/path, NO btime_ns.
        legacy_rows = (
            '{"dev":1,"ino":10,"type":"f","nlink":1,"size":5,'
            '"mtime_ns":1000,"path":"x.txt"}\n'
        )
        out.write_text(legacy_rows, encoding="utf-8")
        rows = read_jsonl(out)
        assert rows[0]["btime_ns"] == -1


class TestF5DeepTreeWalk:
    """10th-pass F5: snapshot_tree was recursive — one Python frame per
    directory level — so trees deeper than sys.getrecursionlimit() raised
    RecursionError mid-walk, leaving the user with no snapshot. Switch to
    an explicit-stack iterative walk."""

    def test_10th_f5_snapshot_tree_handles_depth_1500(self, tmp_path):
        # 1500 levels exceeds Python's default recursion limit of 1000.
        # The recursive implementation crashes with RecursionError; the
        # iterative one returns the full snapshot. Single-char dir names
        # keep the total path length well under PATH_MAX (~4096): tmp_path
        # prefix (~50) + 1500 × "a/" (3000) = ~3050.
        depth = 1500
        # Deep-tree survival is only reachable where the OS lets us *build*
        # such a path. macOS/BSD PATH_MAX is 1024, far below the ~3 KB a
        # 1500-deep path needs, so tree creation (not irsync) would fail
        # there; skip rather than assert an OS limit we don't control.
        needed = len(str(tmp_path)) + depth * 2  # each level adds "/a"
        try:
            path_max = os.pathconf(tmp_path, "PC_PATH_MAX")
        except (OSError, ValueError):
            path_max = 4096
        if needed >= path_max:
            pytest.skip(f"OS PATH_MAX ({path_max}) can't hold a {depth}-deep path")
        cur = tmp_path
        for _ in range(depth):
            cur = cur / "a"
            cur.mkdir()
        rows = snapshot_tree(tmp_path)
        dir_count = sum(1 for r in rows if r["type"] == "d")
        assert dir_count == depth + 1  # all subdirs + root "."
        depths = sorted({r["path"].count("/") for r in rows if r["type"] == "d"})
        assert depths[0] == 0  # root "."
        assert depths[-1] == depth - 1  # deepest "a/a/.../a"


class TestF4StreamingRead:
    """10th-pass F4: read_snapshot must stream line by line so very large
    snapshots don't OOM via text.split('\\n'). Behavior must remain identical
    to a reference parser."""

    def test_10th_f4_read_snapshot_does_not_call_read_text(self, tmp_path, monkeypatch):
        # If read_snapshot still calls Path.read_text, it materializes the
        # whole file at once and the streaming refactor regressed.
        from irsync import snapshot as snapshot_mod

        _build_tree(tmp_path)
        rows = snapshot_tree(tmp_path)
        out = tmp_path.parent / "snap.jsonl"
        write_snapshot(rows, source_root=tmp_path, out_file=out)

        called: list[None] = []
        original_read_text = snapshot_mod.Path.read_text

        def fail_read_text(self, *a, **kw):
            called.append(None)
            return original_read_text(self, *a, **kw)

        monkeypatch.setattr(snapshot_mod.Path, "read_text", fail_read_text)
        _header, _ = read_snapshot(out, expected_source_root=tmp_path)
        assert called == [], (
            "read_snapshot must stream — Path.read_text materializes the "
            "whole file and breaks for multi-GB snapshots"
        )

    def test_10th_f4_streaming_read_matches_reference(self, tmp_path):
        # Behavioral parity: streaming read_snapshot returns the same
        # (header, rows) as a naive whole-file parse.
        _build_tree(tmp_path)
        rows = snapshot_tree(tmp_path)
        out = tmp_path.parent / "snap.jsonl"
        write_snapshot(rows, source_root=tmp_path, out_file=out)

        # Reference parse: the old whole-file approach.
        text = out.read_text(encoding="utf-8", errors="surrogateescape")
        ref_lines = [ln for ln in text.split("\n") if ln.strip()]
        ref_header = json.loads(ref_lines[0])
        ref_rows = [json.loads(ln) for ln in ref_lines[1:]]
        for r in ref_rows:
            r.setdefault("mtime_ns", -1)
            r.setdefault("btime_ns", -1)

        header, loaded = read_snapshot(out, expected_source_root=tmp_path)
        assert header is not None
        assert header["source_root"] == ref_header["_meta"]["source_root"]
        assert loaded == ref_rows

    def test_10th_f4_corrupted_row_reports_actual_file_line_number(self, tmp_path):
        # Streaming must surface the actual file line number (not a header-
        # relative offset) so the user can sed -n <N>p the file directly.
        # The corrupted row is at file line 3 (header=1, good_row=2, bad=3).
        out = tmp_path / "bad.jsonl"
        header_line = (
            '{"_meta":{"source_root":"' + str(tmp_path.resolve()) + '",'
            '"irsync_version":"x","created_at_utc":"x"}}\n'
        )
        good_row = (
            '{"dev":1,"ino":2,"type":"f","nlink":1,"size":0,"path":"a",'
            '"mtime_ns":0,"btime_ns":-1}\n'
        )
        bad_row = "not json at all\n"
        out.write_text(header_line + good_row + bad_row, encoding="utf-8")
        with pytest.raises(SystemExit, match=":3:"):
            read_snapshot(out, expected_source_root=tmp_path)


class TestF1NonUtf8Filenames:
    """10th-pass F1: filenames with arbitrary (non-UTF-8) bytes must round-trip
    through write/read instead of crashing snapshot serialization."""

    def _make_byte_named_file(self, root, name_bytes):
        """Create a file with a non-UTF-8 byte filename. Skip if FS rejects it."""
        try:
            with open(os.path.join(os.fsencode(root), name_bytes), "wb") as f:
                f.write(b"x")
        except OSError as e:
            pytest.skip(f"filesystem rejected byte-named file: {e}")

    def test_10th_f1_snapshot_tree_handles_non_utf8_filename(self, tmp_path):
        # Lone 0x80-0xFF bytes are valid in Linux filenames but invalid UTF-8.
        # snapshot_tree must capture them; the surrogate-escape decoded path
        # is the canonical Python representation.
        self._make_byte_named_file(tmp_path, b"\xff\xfe.bin")
        rows = snapshot_tree(tmp_path)
        paths = {r["path"] for r in rows}
        # The surrogate-escaped decoding of \xff\xfe is \udcff\udcfe.
        assert "\udcff\udcfe.bin" in paths

    def test_10th_f1_write_snapshot_does_not_crash_on_surrogate_path(self, tmp_path):
        self._make_byte_named_file(tmp_path, b"\xff\xfe.bin")
        rows = snapshot_tree(tmp_path)
        out = tmp_path.parent / "snap.jsonl"
        # Without errors='surrogateescape' on the file open, this raises
        # UnicodeEncodeError on the lone surrogate.
        write_snapshot(rows, source_root=tmp_path, out_file=out)
        assert out.exists() and out.stat().st_size > 0

    def test_10th_f1_read_snapshot_round_trips_surrogate_path(self, tmp_path):
        self._make_byte_named_file(tmp_path, b"\xff\xfe.bin")
        rows = snapshot_tree(tmp_path)
        out = tmp_path.parent / "snap.jsonl"
        write_snapshot(rows, source_root=tmp_path, out_file=out)
        _header, loaded = read_snapshot(out, expected_source_root=tmp_path)
        loaded_paths = {r["path"] for r in loaded}
        original_paths = {r["path"] for r in rows}
        assert loaded_paths == original_paths
        assert "\udcff\udcfe.bin" in loaded_paths

    def test_10th_f1_write_jsonl_round_trips_surrogate_path(self, tmp_path):
        # Legacy headerless writer must also handle the encoding cleanly.
        self._make_byte_named_file(tmp_path, b"\xff\xfe.bin")
        rows = snapshot_tree(tmp_path)
        out = tmp_path.parent / "legacy.jsonl"
        write_jsonl(rows, out)
        loaded = read_jsonl(out)
        assert loaded == rows


class TestProvenanceHeader:
    def test_write_snapshot_adds_header_first_line(self, tmp_path):
        _build_tree(tmp_path)
        rows = snapshot_tree(tmp_path)
        out = tmp_path.parent / "snap.jsonl"
        write_snapshot(rows, source_root=tmp_path, out_file=out)
        first = out.read_text(encoding="utf-8").splitlines()[0]
        header = json.loads(first)
        assert "_meta" in header
        assert header["_meta"]["source_root"] == str(tmp_path.resolve())
        assert header["_meta"]["irsync_version"]
        assert header["_meta"]["created_at_utc"]

    def test_read_snapshot_returns_header_and_rows(self, tmp_path):
        _build_tree(tmp_path)
        rows = snapshot_tree(tmp_path)
        out = tmp_path.parent / "snap.jsonl"
        write_snapshot(rows, source_root=tmp_path, out_file=out)
        header, loaded = read_snapshot(out, expected_source_root=tmp_path)
        assert header is not None
        assert header["source_root"] == str(tmp_path.resolve())
        assert loaded == rows

    def test_read_snapshot_rejects_mismatched_source_root(self, tmp_path):
        _build_tree(tmp_path)
        rows = snapshot_tree(tmp_path)
        out = tmp_path.parent / "snap.jsonl"
        write_snapshot(rows, source_root=tmp_path, out_file=out)
        wrong_root = tmp_path / "a"
        with pytest.raises(SnapshotMismatch):
            read_snapshot(out, expected_source_root=wrong_root)

    def test_read_snapshot_on_legacy_headerless_file_returns_none_header(
        self, tmp_path
    ):
        # Snapshots written before H3 had no header — they're a sequence of row objects.
        _build_tree(tmp_path)
        rows = snapshot_tree(tmp_path)
        out = tmp_path.parent / "legacy.jsonl"
        write_jsonl(rows, out)  # no header
        header, loaded = read_snapshot(out, expected_source_root=tmp_path)
        assert header is None
        assert loaded == rows
