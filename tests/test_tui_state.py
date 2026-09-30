import pytest
from conftest import LEGACY_REPO

from cloudy.config import load_config
from cloudy.tui.state import EventReporter, TuiState
from cloudy.workbench import Command, Workbench, parse


def make_state(root, tmp_path, events=None):
    reporter = EventReporter((events if events is not None else []).append)
    return TuiState(Workbench(root, load_config(root), reporter, state_dir=tmp_path / "s"))


@pytest.mark.parametrize("line,expected", [
    ("", None), ("   ", None), ("quit", Command("quit")), (":q", Command("quit")), ("?", Command("help")),
    ("PLAN fix it", Command("plan", "fix it")), ("apply", Command("apply")),
    ("apply fix the CI", Command("apply", "fix the CI")), ("fix the CI", Command("plan", "fix the CI")),
])
def test_parse(line, expected):
    assert parse(line) == expected


def test_only_writing_commands_need_confirmation():
    assert [c for c in ("apply", "revert", "commit") if Command(c).writes] == ["apply", "revert", "commit"]
    assert not any(Command(c).writes for c in ("analyze", "plan", "diff", "status", "help"))


def test_read_only_commands_run_and_preconditions_refuse_without_asking(make_repo, tmp_path):
    state = make_state(make_repo(LEGACY_REPO), tmp_path)
    assert state.submit("analyze").kind == "run" and state.submit("status").kind == "run"
    assert state.submit("plan").message == "usage: plan <task>"
    assert state.submit("apply").message == "Nothing pending. Use plan <task> first."
    assert state.submit("revert").message == "Nothing applied in this session to revert."
    assert state.submit("commit").message == "Nothing applied in this session to commit."
    assert state.confirming is None


def test_plan_confirm_apply_revert_commit(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO)
    ci = root / ".github/workflows/ci.yml"
    original = ci.read_text()
    events = []
    state = make_state(root, tmp_path, events)

    assert state.execute(state.submit("plan fix the CI").command).startswith("proposed:")
    assert ci.read_text() == original and state.view().pending
    assert any(e.startswith("— Explore") for e in events)

    action = state.submit("apply")
    assert action.kind == "confirm" and "Write the" in action.message and state.confirming == Command("apply")
    assert state.submit("plan other").kind == "busy"  # nothing else runs while a question is open
    assert state.confirm(False).message == "Cancelled: apply. Nothing was written."
    assert ci.read_text() == original and state.workbench.has_pending()

    assert state.submit("apply").kind == "confirm"
    run = state.confirm(True)
    assert run.kind == "run" and state.execute(run.command).startswith("done:")
    assert "|| true" not in ci.read_text() and state.view().applied

    assert state.submit("commit").kind == "confirm"
    assert state.execute(state.confirm(True).command).startswith("Committed locally as")

    # the commit is recorded; revert still undoes the applied edits in the working tree
    assert state.submit("revert").kind == "confirm"
    assert state.execute(state.confirm(True).command).startswith("reverted:")
    assert ci.read_text() == original


def test_busy_blocks_new_commands(make_repo, tmp_path):
    state = make_state(make_repo({"a.txt": "x\n"}), tmp_path)
    state.busy = True
    assert state.submit("analyze").kind == "busy"
    assert state.submit("quit").kind == "quit" and state.submit("help").kind == "help"


def test_view_shows_findings_with_hints_checks_and_status(make_repo, tmp_path):
    state = make_state(make_repo(LEGACY_REPO), tmp_path)
    state.execute(Command("plan", "update the README and fix the CI"))
    view = state.view()
    manual = [row for row in view.findings if row[2] == "manual"]
    assert any(row[1] == "docs.broken-links" and "→ Create the target" in row[3] for row in manual)
    assert view.checks and view.checks[0][1] == "config-syntax"
    assert "pending edit(s)" in view.status and view.git == []
