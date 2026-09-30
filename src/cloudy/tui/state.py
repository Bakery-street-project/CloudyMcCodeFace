"""TUI state machine. Pure Python (no Textual import), so every transition is testable without a terminal.

    idle ──submit(read-only)──► busy ──execute──► idle
    idle ──submit(writes)─────► confirming ──confirm(yes)──► busy ──execute──► idle
                                           └─confirm(no)───► idle (nothing written)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..models import CheckResult, Edit, Finding
from ..report import Reporter, first_location
from ..workbench import Command, Workbench, parse


class EventReporter(Reporter):
    """Turns orchestrator progress into one-line messages for the activity log."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self.emit = emit

    def phase(self, title: str) -> None:
        self.emit(f"— {title}")

    def check(self, result: CheckResult) -> None:
        self.emit(f"  {result.status:5} {result.name}  {result.summary}")

    def edit(self, edit: Edit) -> None:
        self.emit(f"  {'applied' if edit.applied else 'proposed'} {edit.path} ({edit.rule})")

    def step(self, step) -> None:
        if step.status in ("failed", "skipped", "reverted"):
            self.emit(f"  {step.status} {step.rule or step.title}{f' — {step.note}' if step.note else ''}")

    def warn(self, message: str) -> None:
        self.emit(f"  warning: {message}")


@dataclass(frozen=True)
class Action:
    kind: str  # none | quit | help | busy | refused | confirm | run
    command: Command | None = None
    message: str = ""


@dataclass
class View:
    """Everything the screen shows, derived from the Workbench after each command."""
    findings: list[tuple[str, str, str, str]] = field(default_factory=list)  # location, rule, kind, message
    checks: list[tuple[str, str, str, str]] = field(default_factory=list)  # status, name, where, summary
    pending: list[Edit] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    git: list[str] | None = None
    status: str = "ready"


QUESTIONS = {
    "apply": "Write the {n} pending edit(s) shown in the diff panel, then verify?",
    "apply-task": "Plan, write and verify “{arg}” in one step?",
    "revert": "Revert the last applied edits ({files})?",
    "commit": "Stage and commit cloudy's last applied files locally ({files})? Nothing is pushed.",
}


class TuiState:
    def __init__(self, workbench: Workbench) -> None:
        self.workbench = workbench
        self.busy = False
        self.confirming: Command | None = None

    # ---- transitions -------------------------------------------------------------------------------------------------

    def submit(self, line: str) -> Action:
        """Decide what a line of input does. Never runs anything itself."""
        command = parse(line)
        if command is None:
            return Action("none")
        if command.name == "quit":
            return Action("quit")
        if command.name == "help":
            return Action("help")
        if self.busy or self.confirming:
            return Action("busy", message="Busy — wait for the current command to finish.")
        if command.name == "plan" and not command.argument:
            return Action("refused", message="usage: plan <task>")
        if not command.writes:
            return Action("run", command)
        refusal = self._precondition(command)
        if refusal:
            return Action("refused", message=refusal)
        self.confirming = command
        return Action("confirm", command, self.question(command))

    def confirm(self, yes: bool) -> Action:
        command, self.confirming = self.confirming, None
        if command is None:
            return Action("none")
        if not yes:
            return Action("refused", message=f"Cancelled: {command.name}. Nothing was written.")
        return Action("run", command)

    def execute(self, command: Command) -> str:
        """Run a command against the Workbench (called from a worker thread). Returns a one-line result."""
        self.busy = True
        try:
            return self._execute(command)
        finally:
            self.busy = False

    # ---- helpers -----------------------------------------------------------------------------------------------------

    def _precondition(self, command: Command) -> str | None:
        wb = self.workbench
        if command.name == "apply" and not command.argument and not wb.has_pending():
            return "Nothing pending. Use plan <task> first."
        if command.name in ("revert", "commit") and not wb.applied:
            return f"Nothing applied in this session to {command.name}."
        return None

    def question(self, command: Command) -> str:
        wb = self.workbench
        files = ", ".join(sorted({e.path for e in wb.applied[-1].edits if e.applied})) if wb.applied else ""
        if command.name == "apply" and command.argument:
            return QUESTIONS["apply-task"].format(arg=command.argument)
        return QUESTIONS[command.name].format(n=len(wb.pending_edits()), files=files)

    def _execute(self, command: Command) -> str:
        wb, name, arg = self.workbench, command.name, command.argument
        if name == "analyze":
            session = wb.analyze(arg)
        elif name == "plan":
            session = wb.plan(arg)
        elif name == "apply":
            session = wb.apply_task(arg) if arg else wb.apply_pending()
        elif name == "revert":
            session = wb.revert_last()
        elif name == "commit":
            outcome = wb.commit_last()
            if outcome is None:
                return "Nothing applied in this session to commit."
            if outcome["commit"]:
                return f"Committed locally as {outcome['commit']} (not pushed): {', '.join(outcome['staged'])}"
            return f"Not committed: {outcome['error'] or 'nothing staged'}"
        else:  # diff, status: only refresh the view
            return {"diff": "Pending diffs shown.", "status": "Status refreshed."}.get(name, "")
        if session is None:
            return "Nothing to do."
        return f"{session.data['status']}: {session.data['message']}"

    def view(self) -> View:
        wb = self.workbench
        view = View(pending=wb.pending_edits(), applied=wb.applied_paths(), git=wb.git_status())
        if wb.last:
            for f in wb.last.data["findings"]:
                finding = Finding(**f)
                view.findings.append((finding.location, finding.rule, "fixable" if finding.fixable else "manual",
                                      finding.message + (f" → {finding.hint}" if finding.hint and not
                                                         finding.fixable else "")))
            for c in (wb.last.data["verifications"] or [{"checks": []}])[-1]["checks"]:
                view.checks.append((c["status"], c["name"], first_location(c["details"]) or "", c["summary"]))
            view.status = f"{wb.last.data['mode']}: {wb.last.data['status']}"
        if wb.has_pending():
            view.status += f" · {len(view.pending)} pending edit(s) — type apply to write them"
        return view
