import json
import shutil
import subprocess
import sys

import pytest
from conftest import LEGACY_REPO

from cloudy.cli import main
from cloudy.orchestrator import Orchestrator
from cloudy.plugins import Plugin, Registry
from cloudy.rules import Rule


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
            and ".git" not in p.relative_to(root).parts}


def run(root, task, tmp_path, apply=False):
    return Orchestrator(root, apply=apply, state_dir=tmp_path / "state").run(task).to_dict()


def test_analyze_changes_nothing(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO)
    before = snapshot(root)
    data = run(root, "analyze this repo and tell me what tools it uses", tmp_path)
    assert data["status"] == "analyzed" and data["edits"] == []
    assert data["profile"]["license"] == "Proprietary"
    assert snapshot(root) == before


def test_safe_mode_proposes_without_writing(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO)
    before = snapshot(root)
    data = run(root, "fix the CI and update the README to match reality", tmp_path)
    assert data["status"] == "proposed"
    assert {e["rule"] for e in data["edits"]} >= {"ci.masked-failures", "ci.dependabot-ecosystems",
                                                  "repo.codeowners", "docs.license-mismatch", "docs.clone-url"}
    assert snapshot(root) == before


def test_apply_mode_fixes_legacy_repo_and_is_idempotent(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO)
    data = run(root, "fix the CI and update the README to match reality", tmp_path, apply=True)
    assert data["status"] == "done", data["message"]
    assert "|| true" not in (root / ".github/workflows/ci.yml").read_text()
    assert "javascript" not in (root / ".github/dependabot.yml").read_text()
    assert (root / ".github/CODEOWNERS").read_text().endswith("* @BoozeLee\n")
    readme = (root / "README.md").read_text()
    assert "MIT" not in readme and "Example-Org/Widget.git" in readme
    manual = {f["rule"] for f in data["findings"] if not f["fixable"]}
    assert {"docs.broken-links", "docs.phantom-commands", "docs.placeholders"} <= manual
    assert all((tmp_path / "state").rglob("backups/*/README.md"))
    again = run(root, "fix the CI and update the README to match reality", tmp_path, apply=True)
    assert again["status"] == "clean" and again["edits"] == []


PY_REPO = {
    "pyproject.toml": '[project]\nname = "calc"\nversion = "0"\n\n[tool.ruff]\nline-length = 100\n',
    "src/calc/__init__.py": "",
    "src/calc/ops.py": "import os\n\n\ndef add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a + b\n",
    "tests/test_ops.py": "from calc.ops import add, sub\n\n\ndef test_add():\n    assert add(2, 2) == 4\n\n\n"
                         "def test_sub():\n    assert sub(5, 3) == 2\n",
}


@pytest.mark.skipif(not (shutil.which("ruff") and shutil.which("pytest")), reason="needs ruff and pytest")
def test_test_loop_fixes_config_and_lint_but_leaves_logic_bug(make_repo, tmp_path):
    root = make_repo(PY_REPO)
    data = run(root, "run the test suite, fix the failing tests and lint", tmp_path, apply=True)
    assert 'pythonpath = ["src"]' in (root / "pyproject.toml").read_text()
    assert "import os" not in (root / "src/calc/ops.py").read_text()
    assert "return a + b" in (root / "src/calc/ops.py").read_text().split("def sub")[1]  # never guesses logic
    assert data["status"] == "needs_human"
    pytest_check = next(c for c in data["verifications"][-1]["checks"] if c["name"] == "pytest")
    assert any("test_sub" in d for d in pytest_check["details"])


class BreakingRule(Rule):
    id = "test.breaking"
    intents = frozenset({"fix_tests"})
    summary = "deliberately break add()"

    def check(self, ctx):
        text = ctx.read("src/calc/ops.py") or ""
        return [self.finding("src/calc/ops.py", "break it", fixable=True)] if "a + b" in text else []

    def fix(self, ctx):
        return {"src/calc/ops.py": ctx.read("src/calc/ops.py").replace("a + b", "a * b")}


@pytest.mark.skipif(not shutil.which("pytest"), reason="needs pytest")
def test_cycle_that_regresses_is_reverted(make_repo, tmp_path):
    files = PY_REPO | {
        "pyproject.toml": PY_REPO["pyproject.toml"] + '\n[tool.pytest.ini_options]\npythonpath = ["src"]\n',
        "tests/test_ops.py": "from calc.ops import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    }
    root = make_repo(files)
    original = (root / "src/calc/ops.py").read_text()
    registry = Registry([Plugin("breaking", "user", [BreakingRule()])])
    orchestrator = Orchestrator(root, apply=True, state_dir=tmp_path / "state", registry=registry)
    data = orchestrator.run("fix the tests").to_dict()
    assert data["status"] == "needs_human" and "reverted" in data["message"]
    assert (root / "src/calc/ops.py").read_text() == original
    assert any(s["status"] == "reverted" for s in data["plan"])


def test_cli_json_and_errors(make_repo, tmp_path, capsys):
    root = make_repo(LEGACY_REPO)
    assert main(["-C", str(root), "--json", "--state-dir", str(tmp_path / "s"), "fix the CI"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "proposed" and data["mode"] == "safe"
    assert main(["-C", str(tmp_path / "nope"), "x"]) == 2
    (root / "cloudy.toml").write_text("bogus = 1\n")
    assert main(["-C", str(root), "x"]) == 2


def test_cli_entry_point_runs(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO)
    result = subprocess.run([sys.executable, "-m", "cloudy.cli", "-C", str(root), "--state-dir", str(tmp_path / "s"),
                             "analyze"], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "Summary" in result.stdout


def test_failed_apply_does_not_leave_pending_overlay(make_repo, tmp_path, monkeypatch):
    root = make_repo(LEGACY_REPO)

    def refuse(self, edit, backup_dir=None):
        raise OSError("disk full")

    monkeypatch.setattr("cloudy.editor.Editor.apply", refuse)
    data = run(root, "fix the CI", tmp_path, apply=True)
    assert data["status"] == "needs_human" and data["edits"] == []
    assert any(s["status"] == "failed" and "disk full" in s["note"] for s in data["plan"])
