import io
import re
import subprocess
import sys

from conftest import LEGACY_REPO
from rich.console import Console

from cloudy.config import load_config
from cloudy.repl import Repl


def make(root, tmp_path, lines=()):
    output = io.StringIO()
    feed = iter(lines)

    def fake_input(prompt):
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    console = Console(file=output, width=200, color_system=None)
    return Repl(root, load_config(root), console, state_dir=tmp_path / "s", input_fn=fake_input), output


def test_plan_diff_apply_revert_cycle(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO)
    original = (root / ".github/workflows/ci.yml").read_text()
    repl, out = make(root, tmp_path)

    assert repl.handle("plan fix the CI")
    assert repl.pending and (root / ".github/workflows/ci.yml").read_text() == original  # plan never writes
    assert "Type apply to write these edits" in out.getvalue()
    planned = {e.path: e.after for e in repl.pending.edits}

    repl.handle("diff")
    assert "+        run: ruff check ." in out.getvalue()

    repl.handle("apply")
    assert (root / ".github/workflows/ci.yml").read_text() == planned[".github/workflows/ci.yml"]
    assert repl.pending is None and repl.last.data["mode"] == "apply-plan"

    repl.handle("status")
    assert re.search(r"Pending\s+nothing", out.getvalue()) and "git status" in out.getvalue()

    repl.handle("revert")
    assert (root / ".github/workflows/ci.yml").read_text() == original
    assert repl.last.data["status"] == "reverted"
    assert repl.handle("quit") is False


def test_apply_refuses_stale_plan_and_keeps_nothing(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO)
    repl, _ = make(root, tmp_path)
    repl.handle("plan fix the CI and update the README")
    readme = root / "README.md"
    readme.write_text(readme.read_text() + "\nedited meanwhile\n")
    repl.handle("apply")
    assert repl.last.data["status"] == "needs_human" and "changed since" in repl.last.data["message"]
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout
    assert status.strip() == "M README.md"  # every other planned edit was rolled back


def test_commit_after_apply_and_free_text_plans(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO)
    repl, out = make(root, tmp_path)
    repl.handle("fix the CI")  # not a command: treated as `plan fix the CI`, never written
    assert repl.pending and repl.pending.data["mode"] == "safe" and repl.applied == []
    repl.handle("apply")
    repl.handle("commit")
    assert "Committed locally as" in out.getvalue()
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=root, capture_output=True, text=True).stdout
    assert log.strip() == "chore: fix the CI"


def test_apply_with_task_runs_the_full_loop(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO)
    repl, _ = make(root, tmp_path)
    repl.handle("apply fix the CI")
    assert repl.last.data["mode"] == "apply" and repl.applied and repl.pending is None
    assert "|| true" not in (root / ".github/workflows/ci.yml").read_text()


def test_messages_when_nothing_to_do_and_loop_ends_on_eof(make_repo, tmp_path):
    root = make_repo({"README.md": "# x\n"})
    repl, out = make(root, tmp_path, ["apply", "revert", "commit", "plan", "help"])
    assert repl.loop() == 0
    text = out.getvalue()
    assert "Nothing pending" in text and "Nothing to revert" in text and "Nothing applied" in text
    assert "usage: plan <task>" in text and "Commands" in text


def test_piped_stdin_terminates_the_prompt_line(make_repo, tmp_path, monkeypatch):
    root = make_repo({"README.md": "# x\n"})
    repl, out = make(root, tmp_path, ["quit"])
    monkeypatch.setattr(sys, "stdin", io.StringIO())  # non-tty: readline callers need complete lines
    assert repl.loop() == 0
    assert re.search(r"^cloudy> $", out.getvalue(), re.M)


def test_tty_prompt_goes_to_input_only(make_repo, tmp_path, monkeypatch):
    class TtyStdin(io.StringIO):
        def isatty(self):
            return True

    root = make_repo({"README.md": "# x\n"})
    seen = []

    def input_fn(prompt):
        seen.append(prompt)
        raise EOFError

    output = io.StringIO()
    repl = Repl(root, load_config(root), Console(file=output, width=200, color_system=None),
                state_dir=tmp_path / "s", input_fn=input_fn)
    monkeypatch.setattr(sys, "stdin", TtyStdin())
    assert repl.loop() == 0
    assert seen == ["cloudy> "]
    assert "cloudy> " not in output.getvalue()
