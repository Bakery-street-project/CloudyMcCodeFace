"""Interactive mode: plan, inspect, apply exactly what was shown, verify, revert, commit — one command at a time."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .report import RichReporter, first_location
from .state import Session
from .workbench import AIUnavailable, Workbench, parse

HELP = """[bold]Commands[/]
  analyze             map the repository and list observations (never writes)
  plan <task>         propose fixes and show their diffs (never writes)
  ask <question>      ask the local model (optional); its edits become pending diffs, never written
  diff                show the pending diffs from the last plan again
  apply               write exactly the pending diffs, then verify (reverts itself if a passing check breaks)
  apply <task>        plan → apply → verify loop in one step
  status              pending edits, last result, what needs a human, git status
  revert              undo the last apply (only files unchanged since)
  commit              stage cloudy's last applied files and commit locally (never pushes)
  help                this text
  quit                leave (also: exit, :q, Ctrl-D)
Anything else is treated as [bold]plan <text>[/] (in `agent chat`: [bold]ask <text>[/]) — free text never writes."""


class Repl:
    def __init__(self, root: str | Path, config: dict, console: Console, *, state_dir: Path | None = None,
                 trust_repo_rules: bool = False, verbose: bool = False,
                 input_fn: Callable[[str], str] = input, chat_mode: bool = False, deep: bool = False,
                 chat_factory=None) -> None:
        self.console = console
        self.reporter = RichReporter(console, verbose)
        self.workbench = Workbench(root, config, self.reporter, state_dir=state_dir,
                                   trust_repo_rules=trust_repo_rules, deep=deep, chat_factory=chat_factory)
        self.chat_mode = chat_mode
        self.root = self.workbench.root
        self.input = input_fn

    # The session bookkeeping lives in the Workbench (shared with the TUI).
    @property
    def pending(self) -> Session | None:
        return self.workbench.pending

    @property
    def last(self) -> Session | None:
        return self.workbench.last

    @property
    def applied(self) -> list[Session]:
        return self.workbench.applied

    def loop(self) -> int:
        try:
            return self._loop()
        finally:
            self.workbench.close()  # never leave a model engine running

    def _loop(self) -> int:
        mode = "chat" if self.chat_mode else "interactive mode"
        self.console.print(f"cloudy {mode} in [bold]{escape(str(self.root))}[/]. Type [bold]help[/].")
        while True:
            try:
                line = self.input(f"cloudy{' (pending)' if self.workbench.has_pending() else ''}> ")
            except EOFError:
                self.console.print()
                return 0
            except KeyboardInterrupt:
                self.console.print("\n(interrupted — type quit to leave)")
                continue
            try:
                if not self.handle(line):
                    return 0
            except KeyboardInterrupt:
                self.console.print("\n[yellow]interrupted[/]; see `status` for anything already applied")

    def handle(self, line: str) -> bool:
        """Run one command. Returns False when the user wants to leave."""
        command = parse(line)
        if command is None:
            return True
        name, rest = command.name, command.argument
        if self.chat_mode and name == "plan" and not line.strip().lower().startswith("plan"):
            name = "ask"  # in chat mode free text goes to the model
        if name == "quit":
            return False
        if name == "help":
            self.console.print(HELP)
        elif name == "analyze":
            self._finish(self.workbench.analyze(rest))
        elif name == "plan":
            if not rest:
                self.console.print("usage: plan <task>")
            else:
                self._finish(self.workbench.plan(rest))
                if self.workbench.has_pending():
                    self.console.print("Type [bold]apply[/] to write these edits, or [bold]diff[/] to see them again.")
        elif name == "ask":
            if not rest:
                self.console.print("usage: ask <question>")
            else:
                self._ask(rest)
        elif name == "diff":
            self._show_pending()
        elif name == "apply" and rest:
            self._finish(self.workbench.apply_task(rest))
        elif name == "apply":
            self._apply()
        elif name == "status":
            self._status()
        elif name == "revert":
            self._revert()
        elif name == "commit":
            self._commit()
        return True

    # ---- commands --------------------------------------------------------------------------------------------------

    def _ask(self, question: str) -> None:
        tokens = 0
        try:
            with self.console.status("thinking…") as status:
                def on_token(_: str) -> None:
                    nonlocal tokens
                    tokens += 1
                    status.update(f"thinking… {tokens} tokens")

                result = self.workbench.ask(question, on_token=on_token,
                                            on_step=lambda step: self.console.print(f"  · {escape(step)}", style="dim"))
        except AIUnavailable as exc:
            self.console.print(f"[yellow]local AI unavailable:[/] {escape(str(exc))}")
            return
        except Exception as exc:  # engine crash or HTTP failure: report, keep the session alive
            self.console.print(f"[red]local AI error:[/] {escape(f'{type(exc).__name__}: {exc}')}")
            return
        for error in result.errors:
            self.console.print(f"  [yellow]rejected:[/] {escape(error)}")
        self.console.print(Panel(Markdown(result.answer or "(no answer)"), title=f"{escape(result.model)}",
                                 subtitle=f"{result.tokens} tokens · local, offline", border_style="cyan"))
        if result.session is not None:
            for edit in result.session.edits:
                self.reporter.edit(edit)
            self.console.print("These are [bold]proposals by a local model[/]. Review them, then type [bold]apply[/] "
                               "to write and verify (auto-revert if a passing check breaks).")

    def _apply(self) -> None:
        session = self.workbench.apply_pending()
        if session is None:
            self.console.print("Nothing pending. Use [bold]plan <task>[/] first.")
        else:
            self._finish(session)

    def _revert(self) -> None:
        session = self.workbench.revert_last()
        if session is None:
            self.console.print("Nothing to revert in this session.")
        else:
            self._finish(session)

    def _commit(self) -> None:
        outcome = self.workbench.commit_last()
        if outcome is None:
            self.console.print("Nothing applied in this session to commit.")
            return
        if outcome["commit"]:
            self.console.print(f"Committed locally as [bold]{escape(outcome['commit'])}[/] (not pushed): "
                               + escape(", ".join(outcome["staged"])))
        for path, reason in outcome["refused"].items():
            self.console.print(f"[yellow]Not staged:[/] {escape(path)} — {escape(reason)}")
        if outcome["error"]:
            self.console.print(f"[yellow]git:[/] {escape(outcome['error'])}")

    def _show_pending(self) -> None:
        edits = self.workbench.pending_edits()
        if not edits:
            self.console.print("No pending diffs.")
        for edit in edits:
            self.reporter.edit(edit)

    def _status(self) -> None:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        pending = self.workbench.pending_edits()
        table.add_row("Pending", escape(", ".join(f"{e.path} ({e.rule})" for e in pending)) or "nothing")
        table.add_row("Applied", escape(", ".join(self.workbench.applied_paths())) or "nothing")
        if self.last:
            table.add_row("Last run", escape(f"{self.last.data['mode']}: {self.last.data['status']} — "
                                             f"{self.last.data['message']}"))
        self.console.print(table)
        manual, failing = self.workbench.needs_human()
        if manual or failing:
            self.console.rule("[bold]Needs a human")
        for f in manual:
            where = f"{f['path']}:{f['line']}" if f["line"] else f["path"]
            self.console.print(f"  {escape(where)}  {escape(f['message'])}")
            if f.get("hint"):
                self.console.print(f"      → {escape(f['hint'])}", style="dim")
        for c in failing:
            where = first_location(c["details"])
            self.console.print(f"  [red]{escape(c['name'])}[/]" + (f" at {escape(where)}" if where else ""))
            if c.get("hint"):
                self.console.print(f"      → {escape(c['hint'])}", style="dim")
        lines = self.workbench.git_status()
        if lines is not None:
            self.console.rule("[bold]git status")
            self.console.print(escape("\n".join(lines[:20])) or "clean", style="dim")
            if len(lines) > 20:
                self.console.print(f"… {len(lines) - 20} more", style="dim")

    def _finish(self, session: Session) -> None:
        self.reporter.summary(session.to_dict())
