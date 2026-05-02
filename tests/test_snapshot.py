import json
import os

import pytest

from irsync.snapshot import (
    SNAPSHOT_FILENAME,
    read_jsonl,
    snapshot_tree,
    write_jsonl,
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
