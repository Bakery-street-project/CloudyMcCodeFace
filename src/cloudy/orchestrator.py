"""Orchestrator: explore -> baseline -> (plan fixes -> edit -> verify)* -> report.

Safe mode (default) proposes edits and shows diffs without writing. Apply mode writes edits with backups,
re-verifies, re-plans from fresh findings, and reverts a cycle that breaks a previously passing check.
"""

from __future__ import annotations

from pathlib import Path

from .config import load_config
from .editor import EditError, Editor
from .executor import Executor
from .explorer import explore
from .models import Finding
from .planner import Planner, classify
from .report import Reporter
from .rules import RepoContext, Rule, all_rules
from .state import Session
from .verifier import Verification, build_checks, categories_for, run_checks


class Orchestrator:
    def __init__(
        self,
        root: str | Path,
        *,
        apply: bool = False,
        config: dict | None = None,
        reporter: Reporter | None = None,
        state_dir: Path | None = None,
        max_cycles: int | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(f"not a directory: {self.root}")
        self.config = config if config is not None else load_config(self.root)
        self.apply = apply
        self.max_cycles = max_cycles or self.config["max_cycles"]
        self.reporter = reporter or Reporter()
        self.state_dir = state_dir

    def run(self, task: str) -> Session:
        session = Session(task, self.root, "apply" if self.apply else "safe", self.state_dir)
        self._git_baseline: tuple[Executor, list[str]] | None = None
        try:
            self._run(task, session)
        except KeyboardInterrupt:
            session.finish("interrupted", "stopped by user; applied edits are backed up in the session")
            raise
        finally:
            self._record_side_effects(session)
            session.save()
        return session

    def _record_side_effects(self, session: Session) -> None:
        """Files the project's own tools created or changed during the run (e.g. Cargo.lock), not cloudy's edits."""
        if not self._git_baseline:
            return
        executor, before = self._git_baseline
        status = executor.run(["git", "status", "--porcelain"], timeout=30)
        if status.ok:
            edited = {e.path for e in session.edits if e.applied}
            changed = {line[3:] for line in status.stdout.splitlines()} - {line[3:] for line in before}
            session.data["tool_side_effects"] = sorted(changed - edited)

    def _run(self, task: str, session: Session) -> None:
        intents = classify(task)
        session.data["intents"] = intents
        planner = Planner(intents)
        session.data["plan"] = planner.steps
        executor = Executor(self.root, timeout=self.config["timeout"], deny=self.config["deny"],
                            env_passthrough=self.config["env_passthrough"], on_result=session.record_command)
        editor = Editor(self.root)

        step = planner.add("explore", "Map repository and probe available tools")
        self.reporter.phase(f"Explore  ({', '.join(intents)})")
        profile = explore(self.root, executor)
        session.data["profile"] = profile.to_dict()
        if profile.git:
            self._git_baseline = (executor, profile.git["dirty"])
        step.status = "done"
        self.reporter.profile(profile)

        rules = [r for r in all_rules() if r.id not in self.config["disabled_rules"]]
        analyze_only = intents == ["analyze"]
        active = rules if analyze_only else [r for r in rules if r.intents & set(intents)]
        ctx = RepoContext(self.root, profile, editor, executor)

        if analyze_only:
            findings = self._collect(active, ctx)
            session.set_findings(findings)
            self.reporter.findings(findings, "Observations")
            planner.add("report", "Report repository analysis").status = "done"
            session.finish("analyzed", f"{len(findings)} observation(s); nothing was changed")
            return

        checks = build_checks(profile, self.config["checks"])
        categories = categories_for(set(intents))

        def verify(label: str) -> Verification:
            step = planner.add("verify", f"Run checks ({label})")
            self.reporter.phase(f"Verify  ({label})")
            result = run_checks(checks, executor, profile, editor.read, categories, self.reporter.check)
            ctx.checks = result.by_name()
            session.record_verification(label, result.checks)
            step.status = "done"
            return result

        baseline = latest = verify("baseline")
        for cycle in range(1, self.max_cycles + 1):
            steps = planner.next_fixes(self._collect(active, ctx), active, cycle)
            if not steps:
                break
            self.reporter.phase(f"Fix  (cycle {cycle})")
            self.reporter.plan(steps)
            cycle_edits = self._fix(steps, {r.id: r for r in active}, ctx, editor, session)
            if not self.apply or not cycle_edits:
                break
            latest = verify(f"cycle {cycle}")
            regressions = latest.regressions(baseline)
            if regressions:
                self._revert(cycle_edits, editor, planner, cycle)
                latest = verify(f"after revert of cycle {cycle}")
                session.set_findings(self._collect(active, ctx))
                session.finish("needs_human", f"cycle {cycle} broke {', '.join(regressions)}; its edits were "
                                              "reverted")
                return

        remaining = self._collect(active, ctx)
        session.set_findings(remaining)
        self.reporter.findings([f for f in remaining if not f.fixable], "Needs a human decision")
        self._finish(session, latest, remaining)

    def _fix(self, steps, rules: dict[str, Rule], ctx: RepoContext, editor: Editor, session: Session) -> list:
        edits = []
        for step in steps:
            try:
                changes = rules[step.rule].fix(ctx)
            except Exception as exc:  # a broken rule must not take the whole run down
                step.status, step.note = "failed", f"rule error: {type(exc).__name__}: {exc}"
                self.reporter.step(step)
                continue
            for path in sorted(changes):
                edit = None
                try:
                    edit = editor.propose(path, changes[path], rule=step.rule, reason=rules[step.rule].summary)
                    if edit and self.apply:
                        editor.apply(edit, session.backup_dir)
                except (EditError, OSError) as exc:
                    if edit:
                        editor.revert(edit)  # drop the pending overlay so later rules see the real file
                    step.status, step.note = "failed", str(exc)
                    continue
                if edit:
                    edits.append(edit)
                    session.edits.append(edit)
                    self.reporter.edit(edit)
            if step.status == "pending":
                changed = any(e.rule == step.rule for e in edits)
                step.status = ("applied" if self.apply else "proposed") if changed else "skipped"
                step.note = "" if changed else "no unambiguous fix available"
            self.reporter.step(step)
        return edits

    def _revert(self, edits: list, editor: Editor, planner: Planner, cycle: int) -> None:
        for edit in reversed(edits):
            try:
                editor.revert(edit)
            except EditError as exc:
                self.reporter.warn(str(exc))
        for step in planner.steps:
            if step.kind == "fix" and step.cycle == cycle and step.status == "applied":
                step.status = "reverted"

    def _collect(self, rules: list[Rule], ctx: RepoContext) -> list[Finding]:
        findings: list[Finding] = []
        for rule in rules:
            try:
                findings.extend(rule.check(ctx))
            except Exception as exc:
                findings.append(Finding(rule.id, "-", f"rule crashed: {type(exc).__name__}: {exc}"))
        return findings

    def _finish(self, session: Session, latest: Verification, remaining: list[Finding]) -> None:
        fixable = [f for f in remaining if f.fixable]
        failing = [c.name for c in latest.checks if c.failed]
        manual = len(remaining) - len(fixable)
        if not self.apply and session.edits:
            status, message = "proposed", f"{len(session.edits)} edit(s) proposed; re-run with --apply to write them"
        elif not failing and not fixable:
            status = "done" if session.edits else "clean"
            message = "all checks pass" + (f"; {manual} item(s) need a human decision" if manual else "")
        else:
            status = "needs_human"
            parts = [f"failing checks: {', '.join(failing)}" if failing else "",
                     f"{len(fixable)} fixable finding(s) could not be resolved" if fixable else ""]
            message = "; ".join(p for p in parts if p)
        session.finish(status, message)
