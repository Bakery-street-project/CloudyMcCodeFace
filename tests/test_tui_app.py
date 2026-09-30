import asyncio
import json
import sys

import pytest
from conftest import LEGACY_REPO

from cloudy.config import load_config

textual = pytest.importorskip("textual")

from textual.widgets import DataTable, Input, RichLog  # noqa: E402

from cloudy.tui.app import CloudyApp, ConfirmScreen  # noqa: E402


async def submit(app, pilot, line):
    app.query_one("#command", Input).value = line
    await pilot.press("enter")
    await settle(app, pilot)


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


def activity(app) -> str:
    return "\n".join(str(line.text) for line in app.query_one("#activity", RichLog).lines)


def make_app(root, tmp_path, **kwargs):
    return CloudyApp(root, load_config(root), state_dir=tmp_path / "s", **kwargs)


def test_first_frame_then_background_analysis(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO)

    async def scenario():
        app = make_app(root, tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            assert app.query_one("#command", Input).has_focus
            await settle(app, pilot)
            assert app.query_one("#findings", DataTable).row_count > 0
            assert "analyzed" in app.sub_title and "— Explore" in activity(app)

    asyncio.run(scenario())


def test_plan_confirm_apply_and_revert_end_to_end(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO)
    ci = root / ".github/workflows/ci.yml"
    original = ci.read_text()

    async def scenario():
        app = make_app(root, tmp_path, analyze_on_start=False)
        async with app.run_test(size=(160, 48)) as pilot:
            await submit(app, pilot, "plan fix the CI")
            assert app.query_one("#diffs", RichLog).border_title.startswith("Pending diffs (")
            assert "(0)" not in app.query_one("#diffs", RichLog).border_title
            assert ci.read_text() == original  # planning never writes

            await submit(app, pilot, "apply")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await settle(app, pilot)
            assert ci.read_text() == original and "Nothing was written" in activity(app)

            await submit(app, pilot, "apply")
            await pilot.press("y")
            await settle(app, pilot)
            assert "|| true" not in ci.read_text() and "done:" in activity(app)

            await submit(app, pilot, "revert")
            await pilot.press("y")
            await settle(app, pilot)
            assert ci.read_text() == original and "reverted:" in activity(app)

            await submit(app, pilot, "quit")
            assert not app.is_running

    asyncio.run(scenario())


def test_brackets_in_input_and_tool_output_are_not_markup(make_repo, tmp_path):
    root = make_repo({"pyproject.toml": '[tool.x]\nname = "x"\n'})

    async def scenario():
        app = make_app(root, tmp_path, analyze_on_start=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await submit(app, pilot, "plan fix [tool.x] and [/bold] docs")
            await submit(app, pilot, "revert")
            assert "[/bold]" in activity(app) and "Nothing applied" in activity(app)

    asyncio.run(scenario())


def test_missing_textual_gives_install_hint(make_repo, monkeypatch, capsys):
    from cloudy.tui import run
    monkeypatch.delitem(sys.modules, "cloudy.tui.app")  # force a fresh import of the Textual front end
    monkeypatch.setitem(sys.modules, "textual", None)  # simulate `pip install cloudy` without the [tui] extra
    assert run(make_repo({}), {}) == 2
    assert 'pip install "cloudy[tui]"' in capsys.readouterr().out


def test_chat_pane_ask_proposes_and_decline_writes_nothing(make_repo, tmp_path):
    from cloudy.ai.chat import Chat
    from cloudy.ai.client import Completion
    from cloudy.ai.config import AI_DEFAULTS

    root = make_repo({"src/app.py": "def f():\n    return 1\n"})
    replies = [json.dumps({"tool": "propose_edit", "args": {"path": "src/app.py", "find": "return 1",
                                                            "replace": "return 2", "reason": "demo"}}),
               json.dumps({"tool": "answer", "args": {"text": "Proposed returning 2."}})]

    class FakeClient:
        def complete(self, messages, schema=None, on_token=None):
            return Completion(replies.pop(0), 1, 0.01, 0.02)

    factory = lambda wb: Chat(wb, FakeClient(), model_name="fake-7b", ai_config=AI_DEFAULTS)  # noqa: E731

    async def scenario():
        plain = make_app(root, tmp_path, analyze_on_start=False)
        async with plain.run_test(size=(160, 48)):
            assert not plain.query_one("#chat").display  # no AI configured: no chat pane
        app = make_app(root, tmp_path, analyze_on_start=False, chat_factory=factory)
        async with app.run_test(size=(180, 48)) as pilot:
            assert app.query_one("#chat").display
            await submit(app, pilot, "ask make f return 2")
            chat = "\n".join(str(line.text) for line in app.query_one("#chat", RichLog).lines)
            assert "you:" in chat and "Proposed returning 2." in chat
            assert "(1)" in app.query_one("#diffs", RichLog).border_title
            await submit(app, pilot, "apply")
            await pilot.press("n")
            await settle(app, pilot)
            assert (root / "src/app.py").read_text() == "def f():\n    return 1\n"

    asyncio.run(scenario())
