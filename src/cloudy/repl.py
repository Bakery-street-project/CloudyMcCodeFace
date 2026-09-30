"""Interactive mode: plan, inspect, apply exactly what was shown, verify, revert, commit — one command at a time."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .executor import Executor
from .git import Git
from .orchestrator import Orchestrator
from .plugins import load_registry
from .report import RichReporter, first_location
from .state import Session

HELP = """[bold]Commands[/]
  analyze             map the repository and list observations (never writes)
  plan <task>         propose fixes and show their diffs (never writes)
  diff                show the pending diffs from the last plan again
  apply               write exactly the pending diffs, then verify (reverts itself if a passing check breaks)
  apply <task>        plan → apply → verify loop in one step
  status              pending edits, last result, what needs a human, git status
  revert              undo the last apply (only files unchanged since)
  commit              stage cloudy's last applied files and commit locally (never pushes)
  help                this text
  quit                leave (also: exit, :q, Ctrl-D)
Anything else is treated as [bold]plan <text>[/] — free text never writes files."""


class Repl:
    def __init__(self, root: str | Path, config: dict, console: Console, *, state_dir: Path | None = None,
                 trust_repo_rules: bool = False, verbose: bool = False,
                 input_fn: Callable[[str], str] = input) -> None:
        self.root = Path(root).resolve()
        self.config = config
        self.console = console
        self.state_dir = state_dir
        self.reporter = RichReporter(console, verbose)
        self.registry = load_registry(self.root, trust_repo=trust_repo_rules)
        self.input = input_fn
        self.pending: Session | None = None  # last plan with edits not yet written
        self.last: Session | None = None  # last session of any kind
        self.applied: list[Session] = []  # applied sessions, newest last (for revert/commit)

    def orchestrator(self, apply: bool = False) -> Orchestrator:
        return Orchestrator(self.root, apply=apply, config=self.config, reporter=self.reporter,
                            state_dir=self.state_dir, registry=self.registry)

    def loop(self) -> int:
        self.console.print(f"cloudy interactive mode in [bold]{escape(str(self.root))}[/]. Type [bold]help[/].")
        while True:
            try:
                line = self.input(f"cloudy{' (pending)' if self._pending_edits() else ''}> ")
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
        command, _, rest = line.strip().partition(" ")
        command, rest = command.lower(), rest.strip()
        if not command:
            return True
        if command in ("quit", "exit", ":q", "q"):
            return False
        if command in ("help", "?"):
            self.console.print(HELP)
        elif command == "analyze":
            self._finish(self.orchestrator().run(rest or "analyze"))
        elif command == "plan":
            if not rest:
                self.console.print("usage: plan <task>")
            else:
                self._run_task(rest, apply=False)
        elif command == "diff":
            self._show_pending()
        elif command == "apply" and rest:
            self._run_task(rest, apply=True)
        elif command == "apply":
            self._apply()
        elif command == "status":
            self._status()
        elif command == "revert":
            self._revert()
        elif command == "commit":
            self._commit()
        else:
            self._run_task(line.strip(), apply=False)
        return True

    # ---- commands --------------------------------------------------------------------------------------------------

    def _run_task(self, task: str, apply: bool) -> None:
        session = self.orchestrator(apply=apply).run(task)
        self._finish(session)
        if apply and any(e.applied for e in session.edits):
            self.applied.append(session)
        self.pending = session if self._pending_edits(session) else None
        if self.pending:
            self.console.print("Type [bold]apply[/] to write these edits, or [bold]diff[/] to see them again.")

    def _apply(self) -> None:
        if not self._pending_edits():
            self.console.print("Nothing pending. Use [bold]plan <task>[/] first.")
            return
        session = self.orchestrator(apply=True).apply_edits(self.pending)
        self._finish(session)
        self.pending = None
        if any(e.applied for e in session.edits):
            self.applied.append(session)

    def _revert(self) -> None:
        if not self.applied:
            self.console.print("Nothing to revert in this session.")
            return
        self._finish(self.orchestrator().revert(self.applied.pop()))

    def _commit(self) -> None:
        if not self.applied:
            self.console.print("Nothing applied in this session to commit.")
            return
        outcome = self.orchestrator().commit_session(self.applied[-1])
        if outcome["commit"]:
            self.console.print(f"Committed locally as [bold]{escape(outcome['commit'])}[/] (not pushed): "
                               + escape(", ".join(outcome["staged"])))
        for path, reason in outcome["refused"].items():
            self.console.print(f"[yellow]Not staged:[/] {escape(path)} — {escape(reason)}")
        if outcome["error"]:
            self.console.print(f"[yellow]git:[/] {escape(outcome['error'])}")

    def _show_pending(self) -> None:
        if not self._pending_edits():
            self.console.print("No pending diffs.")
            return
        for edit in self.pending.edits:
            if not edit.applied:
                self.reporter.edit(edit)

    def _status(self) -> None:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        pending = [e for e in (self.pending.edits if self.pending else []) if not e.applied]
        table.add_row("Pending", escape(", ".join(f"{e.path} ({e.rule})" for e in pending)) or "nothing")
        table.add_row("Applied", escape(", ".join(sorted({e.path for s in self.applied for e in s.edits
                                                          if e.applied}))) or "nothing")
        if self.last:
            table.add_row("Last run", escape(f"{self.last.data['mode']}: {self.last.data['status']} — "
                                             f"{self.last.data['message']}"))
        self.console.print(table)
        if self.last:
            manual = [f for f in self.last.data["findings"] if not f["fixable"]]
            failing = [c for c in (self.last.data["verifications"] or [{"checks": []}])[-1]["checks"]
                       if c["status"] in ("fail", "error")]
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
        git = Git(Executor(self.root, timeout=30))
        if git.available():
            lines = git.status()
            self.console.rule("[bold]git status")
            self.console.print(escape("\n".join(lines[:20])) or "clean", style="dim")
            if len(lines) > 20:
                self.console.print(f"… {len(lines) - 20} more", style="dim")

    # ---- helpers ---------------------------------------------------------------------------------------------------

    def _finish(self, session: Session) -> None:
        self.last = session
        self.reporter.summary(session.to_dict())

    def _pending_edits(self, session: Session | None = None) -> bool:
        session = session if session is not None else self.pending
        return bool(session and session.data["mode"] == "safe" and any(not e.applied for e in session.edits))
