import os

import pytest

from cloudy.editor import EditError, Editor, validate


def test_propose_does_not_write_and_overlays(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    editor = Editor(tmp_path)
    edit = editor.propose("a.txt", "two\n", rule="r", reason="why")
    assert (tmp_path / "a.txt").read_text() == "one\n"
    assert editor.read("a.txt") == "two\n"
    assert "-one" in edit.diff and "+two" in edit.diff


def test_apply_backup_and_revert(tmp_path):
    target = tmp_path / "a.sh"
    target.write_text("echo a\n")
    target.chmod(0o755)
    editor = Editor(tmp_path)
    edit = editor.propose("a.sh", "echo b\n", rule="r", reason="why")
    editor.apply(edit, tmp_path / "backup")
    assert target.read_text() == "echo b\n" and os.access(target, os.X_OK)
    assert (tmp_path / "backup" / "a.sh").read_text() == "echo a\n"
    editor.revert(edit)
    assert target.read_text() == "echo a\n" and not edit.applied


def test_stale_edit_is_refused(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    editor = Editor(tmp_path)
    edit = editor.propose("a.txt", "two\n", rule="r", reason="why")
    (tmp_path / "a.txt").write_text("changed by someone else\n")
    with pytest.raises(EditError, match="changed since"):
        editor.apply(edit)


def test_edit_that_breaks_syntax_is_rejected(tmp_path):
    (tmp_path / "c.yml").write_text("a: 1\n")
    with pytest.raises(EditError, match="break syntax"):
        Editor(tmp_path).propose("c.yml", "a: [1\n", rule="r", reason="why")


def test_crlf_is_preserved(tmp_path):
    (tmp_path / "w.txt").write_bytes(b"a\r\nb\r\n")
    editor = Editor(tmp_path)
    editor.apply(editor.propose("w.txt", editor.read("w.txt").replace("b", "c"), rule="r", reason="why"))
    assert (tmp_path / "w.txt").read_bytes() == b"a\r\nc\r\n"


def test_paths_outside_repo_are_refused(tmp_path):
    with pytest.raises(EditError):
        Editor(tmp_path / "sub").resolve("../x")


def test_unchanged_content_yields_no_edit(tmp_path):
    (tmp_path / "a.txt").write_text("same")
    assert Editor(tmp_path).propose("a.txt", "same", rule="r", reason="why") is None


@pytest.mark.parametrize("path,text,ok", [
    ("x.py", "def f(:\n", False), ("x.py", "x = 1\n", True), ("x.json", "{", False),
    ("x.toml", "a = [", False), ("x.yaml", "a: b\n", True), ("x.txt", "{{{", True),
])
def test_validate(path, text, ok):
    assert (validate(path, text) is None) == ok
