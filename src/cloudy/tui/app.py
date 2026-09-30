"""Textual front end. A view over TuiState/Workbench: it renders, reads input and asks for confirmation.

All work runs in one exclusive background worker, so the screen stays responsive and the first frame never waits
for exploration or checks.
"""

from __future__ import annotations

import threading
from pathlib import Path

from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, RichLog, Static

from ..repl import HELP
from ..workbench import Command, Workbench
from .state import Action, EventReporter, TuiState

STATUS_STYLE = {"pass": "green", "fail": "red", "error": "red", "skip": "dim"}


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no question before anything is written."""

    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n", "answer(False)", "No"),
                Binding("escape", "answer(False)", "No", show=False)]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.question, id="question")
            with Horizontal(id="buttons"):
                yield Button("Yes (y)", id="yes", variant="warning")
                yield Button("No (n)", id="no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class CloudyApp(App):
    TITLE = "cloudy"
    CSS = """
    #main { height: 1fr; }
    #left, #right { width: 1fr; }
    #findings, #checks, #diffs, #git, #activity { border: round $primary; }
    #findings { height: 1fr; }
    #checks { height: 1fr; }
    #diffs { height: 2fr; }
    #git { height: 1fr; }
    #activity { height: 8; }
    #chat { display: none; }
    ConfirmScreen { align: center middle; }
    #dialog { width: 72; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    #buttons { height: auto; margin-top: 1; }
    #buttons Button { margin-right: 2; }
    """
    BINDINGS = [Binding("ctrl+q", "quit", "Quit"), Binding("f1", "help", "Help"),
                Binding("f5", "command('status')", "Refresh"), Binding("escape", "focus_input", "Command bar")]

    def __init__(self, root: str | Path, config: dict, *, state_dir: Path | None = None,
                 trust_repo_rules: bool = False, analyze_on_start: bool = True) -> None:
        super().__init__()
        self.state = TuiState(Workbench(root, config, EventReporter(self._emit), state_dir=state_dir,
                                        trust_repo_rules=trust_repo_rules))
        self.analyze_on_start = analyze_on_start
        self.sub_title = str(self.state.workbench.root)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield DataTable(id="findings", cursor_type="row", zebra_stripes=True)
                yield DataTable(id="checks", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right"):
                yield RichLog(id="diffs", wrap=False, highlight=False)
                yield RichLog(id="git", wrap=True)
            yield Static("Local AI chat arrives in v2.0 (optional extra).", id="chat")
        yield RichLog(id="activity", wrap=True, markup=True)
        yield Input(placeholder="plan <task> · apply · revert · commit · analyze · status · help", id="command")
        yield Footer()

    def on_mount(self) -> None:
        self._main_thread = threading.get_ident()
        titles = {"findings": "Findings", "checks": "Checks", "diffs": "Pending diffs", "git": "git status",
                  "activity": "Activity"}
        for widget_id, title in titles.items():
            self.query_one(f"#{widget_id}").border_title = title
        self.query_one("#findings", DataTable).add_columns("Location", "Rule", "Kind", "Message")
        self.query_one("#checks", DataTable).add_columns("Status", "Check", "Where", "Summary")
        self.query_one("#command", Input).focus()
        self._log("Type a command below. Anything that writes asks for confirmation first. F1: help.")
        if self.analyze_on_start:
            self.run_command(Command("analyze"))
        else:
            self.refresh_view()

    # ---- input ---------------------------------------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.value = ""
        self.dispatch(self.state.submit(event.value))

    def dispatch(self, action: Action) -> None:
        if action.kind == "quit":
            self.exit()
        elif action.kind == "help":
            self.action_help()
        elif action.kind in ("busy", "refused"):
            self._log(f"[yellow]{escape(action.message)}[/]", markup=True)
        elif action.kind == "confirm":
            self.push_screen(ConfirmScreen(action.message), lambda yes: self.dispatch(self.state.confirm(bool(yes))))
        elif action.kind == "run":
            self._log(f"[bold]> {escape(f'{action.command.name} {action.command.argument}'.strip())}[/]", markup=True)
            self.run_command(action.command)

    def action_help(self) -> None:
        self._log(HELP, markup=True)

    def action_focus_input(self) -> None:
        self.query_one("#command", Input).focus()

    def action_command(self, line: str) -> None:
        self.dispatch(self.state.submit(line))

    # ---- work ----------------------------------------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="cloudy")
    def run_command(self, command: Command) -> None:
        try:
            message = self.state.execute(command)
        except Exception as exc:  # show the failure instead of killing the UI
            message = f"error: {type(exc).__name__}: {exc}"
        self.call_from_thread(self._finish, message)

    def _finish(self, message: str) -> None:
        self._log(message)
        self.refresh_view()

    def refresh_view(self) -> None:
        view = self.state.view()
        findings = self.query_one("#findings", DataTable)
        findings.clear()
        for row in view.findings:
            findings.add_row(*row)
        checks = self.query_one("#checks", DataTable)
        checks.clear()
        for status, name, where, summary in view.checks:
            checks.add_row(Text(status, style=STATUS_STYLE.get(status, "")), name, where, summary)
        diffs = self.query_one("#diffs", RichLog)
        diffs.clear()
        diffs.border_title = f"Pending diffs ({len(view.pending)})"
        for edit in view.pending:
            diffs.write(Text(f"{edit.path}  ({edit.rule})", style="bold"))
            diffs.write(Syntax(edit.diff, "diff", theme="ansi_dark", background_color="default"))
        if not view.pending:
            diffs.write(Text("No pending edits. `plan <task>` proposes some; nothing is written until you apply.",
                             style="dim"))
        git = self.query_one("#git", RichLog)
        git.clear()
        git.write("\n".join(view.git) if view.git else "clean" if view.git is not None else "not a git repository")
        if view.applied:
            git.write(Text(f"applied this session: {', '.join(view.applied)}", style="dim"))
        self.sub_title = f"{self.state.workbench.root} — {view.status}"

    # ---- helpers -------------------------------------------------------------------------------------------------

    def _emit(self, message: str) -> None:
        """Progress from the orchestrator; safe to call from the worker thread."""
        if threading.get_ident() == getattr(self, "_main_thread", None):
            self._log(message)
        else:
            self.call_from_thread(self._log, message)

    def _log(self, message: str, markup: bool = False) -> None:
        """Plain text unless `markup` (only for cloudy's own strings; tool output may contain brackets)."""
        self.query_one("#activity", RichLog).write(Text.from_markup(message) if markup else Text(message))
