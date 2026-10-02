import io
import os
import stat

import pytest
from conftest import LEGACY_REPO
from rich.console import Console

from cloudy.config import ConfigError, load_config
from cloudy.executor import Executor
from cloudy.explorer import is_ignored
from cloudy.models import CheckResult
from cloudy.orchestrator import Orchestrator
from cloudy.report import RichReporter, first_location


def run(root, task, tmp_path, apply=False):
    return Orchestrator(root, apply=apply, state_dir=tmp_path / "s").run(task).to_dict()


@pytest.mark.parametrize("path,patterns,expected", [
    ("generated/a/b.md", ["generated/*"], True), ("generated", ["generated/*"], True),
    ("docs/a.md", ["generated/*"], False), ("x.min.js", ["*.min.js"], True),
])
def test_ignore_patterns(path, patterns, expected):
    assert is_ignored(path, patterns) == expected


def test_ignored_files_are_neither_analysed_nor_edited(make_repo, tmp_path):
    files = LEGACY_REPO | {"vendored/README.md": "[x](missing.md)\n",
                           ".cloudy.toml": 'ignore = ["vendored/*", "README.md"]\n'}
    root = make_repo(files)
    data = run(root, "update the README and docs", tmp_path, apply=True)
    assert data["profile"]["ignore"] == ["vendored/*", "README.md"]
    assert not any(f["path"].startswith("vendored/") or f["path"] == "README.md" for f in data["findings"])
    assert all(e["path"] != "README.md" for e in data["edits"])
    assert "MIT" in (root / "README.md").read_text()  # left exactly as it was


def test_configured_tool_paths(make_repo, tmp_path):
    fake = tmp_path / "bin" / "my-ruff"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'ruff 9.9.9 (pinned)'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    root = make_repo({"a.py": "x = 1\n", "cloudy.toml": f'[tools]\nruff = "{fake}"\nmypy = "missing/mypy"\n'})
    data = run(root, "analyze", tmp_path)
    assert data["profile"]["tools"]["ruff"] == {"path": str(fake), "version": "ruff 9.9.9 (pinned)"}
    assert data["profile"]["tools"]["mypy"]["path"] is None
    assert "configured tool `mypy` not found at missing/mypy" in data["warnings"]


@pytest.mark.parametrize("body,message", [
    ("tools = 3", "tools must be a table"), ("ignore = [1]", "ignore must be a list"),
    ('disabled_groups = "docs"', "disabled_groups must be a list"), ('clean_tool_files = "yes"', "true or false"),
])
def test_new_config_keys_are_validated(tmp_path, body, message):
    (tmp_path / "cloudy.toml").write_text(body)
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path)


@pytest.mark.parametrize("clean", [False, True])
def test_files_created_by_tools_are_reported_and_optionally_removed(make_repo, tmp_path, clean):
    config = f'clean_tool_files = {str(clean).lower()}\nchecks = {{ junk = "echo x > tool-cache.txt" }}\n'
    root = make_repo({"cloudy.toml": config, "keep.txt": "mine\n"})
    (root / "untracked-by-user.txt").write_text("already here\n")  # existed before the run: never touched
    data = run(root, "fix lint", tmp_path)
    assert data["tool_side_effects"] == ["tool-cache.txt"]
    assert (root / "tool-cache.txt").exists() is not clean
    assert data.get("tool_files_removed", []) == (["tool-cache.txt"] if clean else [])
    assert (root / "untracked-by-user.txt").exists()


def test_needs_human_output_names_location_and_hint(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO)
    data = run(root, "update the README", tmp_path)
    broken = next(f for f in data["findings"] if f["rule"] == "docs.broken-links")
    assert broken["line"] and broken["hint"].startswith("Create the target")
    out = io.StringIO()
    reporter = RichReporter(Console(file=out, width=200, color_system=None))
    reporter.failures([CheckResult("go test", "test", "fail", "exit 1", details=[
        "--- FAIL: TestSub", "calc_test.go:7: Sub(5, 3) = 8, want 2"], hint="A test fails: look here")])
    assert "go test  at calc_test.go:7" in out.getvalue() and "→ A test fails: look here" in out.getvalue()


@pytest.mark.parametrize("details,expected", [
    (["not ok 2 - sub", "web/test/calc.test.js:9:1"], "web/test/calc.test.js:9:1"),
    (["src/t.ts(1,14): error TS2322"], "src/t.ts(1,14)"), (["no location here"], None),
])
def test_first_location(details, expected):
    assert first_location(details) == expected


def test_windows_shell_fallback(tmp_path, monkeypatch):
    executor = Executor(tmp_path)
    monkeypatch.setattr(executor, "which", lambda name: None)
    monkeypatch.setattr(os, "name", "nt")
    assert executor.shell() == ["cmd.exe", "/d", "/c"]
    monkeypatch.setattr(os, "name", "posix")
    assert executor.shell() == ["/bin/sh", "-c"]
