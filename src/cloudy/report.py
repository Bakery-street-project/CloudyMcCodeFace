"""Reporting: a silent base reporter (for tests and --json) and a rich terminal reporter."""

from __future__ import annotations

import re

from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table

from .explorer import RepoProfile
from .models import CheckResult, Edit, Finding

STATUS_STYLE = {
    "pass": "green", "done": "green", "applied": "green", "clean": "green", "analyzed": "green",
    "proposed": "cyan", "skip": "dim", "skipped": "dim", "fail": "red", "error": "red", "failed": "red",
    "needs_human": "yellow", "reverted": "yellow", "interrupted": "yellow",
}


class Reporter:
    """No-op reporter; the orchestrator calls these hooks as work progresses."""

    def phase(self, title: str) -> None: ...
    def profile(self, profile: RepoProfile) -> None: ...
    def plan(self, steps: list) -> None: ...
    def step(self, step) -> None: ...
    def edit(self, edit: Edit) -> None: ...
    def check(self, result: CheckResult) -> None: ...
    def findings(self, findings: list[Finding], title: str) -> None: ...
    def failures(self, checks: list[CheckResult]) -> None: ...
    def warn(self, message: str) -> None: ...


LOCATION = re.compile(r"[\w./\\-]+(?::\d+){1,2}|[\w./-]+\(\d+,\d+\)")


def first_location(details: list[str]) -> str | None:
    """The first `file:line` (or tsc `file(line,col)`) mentioned in a check's details."""
    for line in details:
        if match := LOCATION.search(line):
            return match.group(0)
    return None


def _styled(status: str) -> str:
    return f"[{STATUS_STYLE.get(status, 'white')}]{status}[/]"


class RichReporter(Reporter):
    def __init__(self, console: Console | None = None, verbose: bool = False) -> None:
        self.console = console or Console()
        self.verbose = verbose

    def phase(self, title: str) -> None:
        self.console.rule(f"[bold]{escape(title)}")

    def profile(self, p: RepoProfile) -> None:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        langs = ", ".join(f"{k} ({v})" for k, v in p.languages.items()) or "none"
        table.add_row("Languages", escape(langs + (" [file limit reached]" if p.truncated else "")))
        table.add_row("Manifests", escape(", ".join(p.manifests) or "none"))
        table.add_row("CI", escape(", ".join(p.ci_files) or "none")
                      + (f"  (uses: {escape(', '.join(p.ci_tools))})" if p.ci_tools else ""))
        tests = f"{len(p.test_files)} file(s)" if p.test_files else "none found"
        table.add_row("Tests", escape(tests + (f"; runners: {', '.join(p.test_runners)}" if p.test_runners else "")))
        projects = [f"{n.dir} ({n.package_manager}, {len(n.deps)} deps)" for n in p.node_projects]
        projects += [f"{d} (go)" for d in p.go_modules] + [f"{d} (cargo)" for d in p.cargo_roots]
        if projects:
            table.add_row("Projects", escape(", ".join(projects)))
        table.add_row("Linters", escape(", ".join(f"{k} ({v})" for k, v in p.linters.items()) or "none configured"))
        if any(p.scripts.values()):
            table.add_row("Scripts", escape("; ".join(f"{k}: {', '.join(v)}" for k, v in p.scripts.items() if v)))
        tools = [f"[green]{t.name}[/]" if t.available else f"[red]{t.name}✗[/]" for t in p.tools.values()]
        table.add_row("Tools", " ".join(tools) or "none probed")
        table.add_row("License", escape(p.license or "no LICENSE file"))
        if p.git:
            dirty = f", {len(p.git['dirty'])} uncommitted change(s)" if p.git["dirty"] else ", clean"
            table.add_row("Git", escape(f"{p.git['branch']}{dirty}; {p.git['commit_style']}"))
        self.console.print(table)

    def plan(self, steps: list) -> None:
        for step in steps:
            self.console.print(f"  {step.id}. [bold]{escape(step.rule or step.kind)}[/] — {escape(step.title)}")

    def step(self, step) -> None:
        note = f" — {escape(step.note)}" if step.note else ""
        self.console.print(f"  {_styled(step.status)} {escape(step.rule or step.title)}{note}")

    def edit(self, edit: Edit) -> None:
        verb = "applied" if edit.applied else "proposed"
        self.console.print(f"[bold]{escape(edit.path)}[/] ({verb}, {escape(edit.rule)})")
        self.console.print(Syntax(edit.diff, "diff", theme="ansi_dark", background_color="default", word_wrap=True))

    def check(self, result: CheckResult) -> None:
        self.console.print(f"  {_styled(result.status)} [bold]{escape(result.name)}[/] {escape(result.summary)}")
        shown = result.details if self.verbose else result.details[:8]
        for line in shown:
            self.console.print(f"      {escape(line)}", style="dim")
        if len(result.details) > len(shown):
            self.console.print(f"      … {len(result.details) - len(shown)} more (use -v)", style="dim")

    def findings(self, findings: list[Finding], title: str) -> None:
        if not findings:
            return
        self.phase(title)
        for f in findings:
            mark = "[cyan]fixable[/]" if f.fixable else "[yellow]manual[/]"
            self.console.print(f"  {mark} {escape(f.location)}  {escape(f.message)}  [dim]{escape(f.rule)}[/]")
            if f.hint and not f.fixable:
                self.console.print(f"         → {escape(f.hint)}", style="dim")

    def failures(self, checks: list[CheckResult]) -> None:
        if not checks:
            return
        self.phase("Failing checks — where to look")
        for check in checks:
            where = first_location(check.details)
            self.console.print(f"  [red]{escape(check.name)}[/]" + (f"  at [bold]{escape(where)}[/]" if where else "")
                               + (f"  {escape(check.details[0])}" if check.details and not where else ""))
            if check.hint:
                self.console.print(f"         → {escape(check.hint)}", style="dim")

    def warn(self, message: str) -> None:
        self.console.print(f"[yellow]warning:[/] {escape(message)}")

    def summary(self, data: dict) -> None:
        self.phase("Summary")
        self.console.print(f"Status: {_styled(data['status'])} — {escape(data['message'])}")
        reverted = data["mode"] == "revert"
        changed = sorted({e["path"] for e in data["edits"] if e["applied"]})
        proposed = sorted({e["path"] for e in data["edits"] if not e["applied"] and not reverted})
        if reverted and data["edits"]:
            self.console.print("Reverted: " + escape(", ".join(sorted({e["path"] for e in data["edits"]}))))
        if changed:
            self.console.print("Changed: " + escape(", ".join(changed)))
        if proposed:
            self.console.print("Proposed (not written): " + escape(", ".join(proposed)))
        commands = []
        for verification in data["verifications"][-1:]:
            commands = [c["command"] for c in verification["checks"]
                        if c["command"] not in ("", "(built-in)") and c["status"] != "skip"]
        if changed or commands:
            self.console.print("Verify yourself:")
            for command in (["git diff"] if changed else []) + list(dict.fromkeys(commands)):
                self.console.print(f"  {escape(command)}")
        if data.get("git_diff_stat"):
            self.console.print("git diff --stat (cloudy's files):")
            self.console.print(escape(data["git_diff_stat"]), style="dim")
        git = data.get("git")
        if git:
            if git["staged"]:
                self.console.print("Staged: " + escape(", ".join(git["staged"])))
            for path, reason in git["refused"].items():
                self.console.print(f"[yellow]Not staged:[/] {escape(path)} — {escape(reason)}")
            if git["commit"]:
                self.console.print(f"Committed locally: [bold]{escape(git['commit'])}[/] (not pushed)")
            if git["error"]:
                self.console.print(f"[yellow]git:[/] {escape(git['error'])}")
        if data.get("tool_side_effects"):
            self.console.print("Created or changed by the project's own tools, not by cloudy: "
                               + escape(", ".join(data["tool_side_effects"])))
        if data.get("tool_files_removed"):
            self.console.print("Removed again (clean_tool_files): " + escape(", ".join(data["tool_files_removed"])))
        for warning in data.get("warnings", []):
            self.warn(warning)
        if data.get("backup_dir"):
            self.console.print(f"Backups: {escape(data['backup_dir'])}")
        self.console.print(f"Session log: {escape(data['session_file'])}", style="dim")
