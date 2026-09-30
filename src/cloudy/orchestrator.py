"""Orchestrator: explore -> baseline -> (plan fixes -> edit -> verify)* -> report.

Safe mode (default) proposes edits and shows diffs without writing. Apply mode writes edits with backups,
re-verifies, re-plans from fresh findings, and reverts a cycle that breaks a previously passing check.
`apply_edits` applies exactly the diffs a safe-mode session showed; `revert` undoes an applied session.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .config import load_config
from .editor import EditError, Editor
from .executor import Executor
from .explorer import RepoProfile, explore, is_ignored
from .git import Git, commit_message, porcelain_paths
from .models import Edit, Finding
from .planner import Planner, classify
from .plugins import Registry, load_registry
from .report import Reporter
from .rules import RepoContext, Rule
from .state import Session
from .verifier import Verification, build_checks, categories_for, run_checks


@dataclass
class Workspace:
    """Everything one run needs, built fresh per run so state never leaks between runs."""
    executor: Executor
    editor: Editor
    profile: RepoProfile
    ctx: RepoContext
    rules: list[Rule]


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
        registry: Registry | None = None,
        trust_repo_rules: bool = False,
        git_action: str | None = None,  # None | "stage" | "commit"
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(f"not a directory: {self.root}")
        if git_action not in (None, "stage", "commit"):
            raise ValueError(f"unknown git action: {git_action}")
        self.config = config if config is not None else load_config(self.root)
        self.apply = apply
        self.max_cycles = max_cycles or self.config["max_cycles"]
        self.reporter = reporter or Reporter()
        self.state_dir = state_dir
        self.registry = registry or load_registry(self.root, trust_repo=trust_repo_rules)
        self.git_action = git_action
        self._git_baseline: tuple[Executor, list[str]] | None = None

    # ---- public entry points -------------------------------------------------------------------------------------

    def run(self, task: str) -> Session:
        session = Session(task, self.root, "apply" if self.apply else "safe", self.state_dir)
        return self._guarded(session, lambda: self._run(task, session))

    def apply_edits(self, plan: Session) -> Session:
        """Apply exactly the edits a safe-mode session proposed, then verify against that session's baseline."""
        session = Session(plan.data["task"], self.root, "apply-plan", self.state_dir)
        session.data["plan_session"] = plan.id
        return self._guarded(session, lambda: self._apply_plan(plan, session))

    def revert(self, applied: Session) -> Session:
        """Undo an applied session's edits (newest first). Files changed since then are left alone and reported."""
        session = Session(applied.data["task"], self.root, "revert", self.state_dir)
        editor = Editor(self.root)
        failed = []
        for edit in reversed([e for e in applied.edits if e.applied]):
            try:
                editor.revert(edit)
                session.edits.append(edit)
            except EditError as exc:
                failed.append(str(exc))
                self.reporter.warn(str(exc))
        session.data["reverted_session"] = applied.id
        if failed:
            session.finish("needs_human", f"{len(failed)} file(s) changed since they were applied; not reverted")
        else:
            session.finish("reverted", f"reverted {len(session.edits)} edit(s) from session {applied.id}")
        session.save()
        return session

    # ---- shared plumbing -----------------------------------------------------------------------------------------

    def _guarded(self, session: Session, work) -> Session:
        self._git_baseline = None
        try:
            work()
        except KeyboardInterrupt:
            session.finish("interrupted", "stopped by user; applied edits are backed up in the session")
            raise
        finally:
            self._record_side_effects(session)
            session.data["warnings"] = list(dict.fromkeys(self.registry.warnings + session.data.get("warnings", [])))
            session.save()
        return session

    def _setup(self, session: Session, intents: list[str]) -> Workspace:
        executor = Executor(self.root, timeout=self.config["timeout"], deny=self.config["deny"],
                            env_passthrough=self.config["env_passthrough"], on_result=session.record_command)
        editor = Editor(self.root)
        self.reporter.phase(f"Explore  ({', '.join(intents)})")
        profile = explore(self.root, executor, self.registry, ignore=self.config["ignore"],
                          tool_paths=self.config["tools"])
        session.data["profile"] = profile.to_dict()
        session.data["warnings"] = [f"configured tool `{name}` not found at {path}"
                                    for name, path in self.config["tools"].items()
                                    if not profile.has_tool(name)]
        if profile.git:
            self._git_baseline = (executor, profile.git["dirty"])
        self.reporter.profile(profile)
        rules = self.registry.rules(self.config["disabled_rules"], self.config["disabled_groups"])
        return Workspace(executor, editor, profile, RepoContext(self.root, profile, editor, executor), rules)

    def _record_side_effects(self, session: Session) -> None:
        """Files the project's own tools created or changed during the run (e.g. Cargo.lock), not cloudy's edits."""
        if not self._git_baseline:
            return
        executor, before = self._git_baseline
        status = executor.run(["git", "status", "--porcelain"], timeout=30)
        if not status.ok:
            return
        lines = status.stdout.splitlines()
        edited = {e.path for e in session.edits if e.applied}
        effects = sorted(porcelain_paths(lines) - porcelain_paths(before) - edited)
        session.data["tool_side_effects"] = effects
        if self.config["clean_tool_files"]:
            new_untracked = porcelain_paths([line for line in lines if line.startswith("??")])
            removed = []
            for path in effects:
                target = (self.root / path).resolve()
                # only plain files the tools created during this run; directories are reported, never deleted
                if path in new_untracked and target.is_file() and target.is_relative_to(self.root):
                    target.unlink()
                    removed.append(path)
            session.data["tool_files_removed"] = removed

    def _verify(self, ws: Workspace, session: Session, planner: Planner | None, checks, categories,
                label: str) -> Verification:
        step = planner.add("verify", f"Run checks ({label})") if planner else None
        self.reporter.phase(f"Verify  ({label})")
        result = run_checks(checks, ws.executor, ws.profile, ws.editor.read, categories, self.reporter.check)
        ws.ctx.checks = result.by_name()
        session.record_verification(label, result.checks)
        if step:
            step.status = "done"
        return result

    def _collect(self, rules: list[Rule], ctx: RepoContext) -> list[Finding]:
        findings: list[Finding] = []
        for rule in rules:
            try:
                found = rule.check(ctx)
            except Exception as exc:
                found = [Finding(rule.id, "-", f"rule crashed: {type(exc).__name__}: {exc}")]
            findings.extend(f for f in found if not is_ignored(f.path, ctx.profile.ignore))
        return findings

    # ---- the loop ------------------------------------------------------------------------------------------------

    def _run(self, task: str, session: Session) -> None:
        intents = classify(task)
        session.data["intents"] = intents
        planner = Planner(intents)
        session.data["plan"] = planner.steps
        step = planner.add("explore", "Map repository and probe available tools")
        ws = self._setup(session, intents)
        step.status = "done"

        analyze_only = intents == ["analyze"]
        active = ws.rules if analyze_only else [r for r in ws.rules if r.intents & set(intents)]
        if analyze_only:
            findings = self._collect(active, ws.ctx)
            session.set_findings(findings)
            self.reporter.findings(findings, "Observations")
            planner.add("report", "Report repository analysis").status = "done"
            session.finish("analyzed", f"{len(findings)} observation(s); nothing was changed")
            return

        checks = build_checks(ws.profile, self.config["checks"], self.registry)
        categories = categories_for(set(intents))
        baseline = latest = self._verify(ws, session, planner, checks, categories, "baseline")
        for cycle in range(1, self.max_cycles + 1):
            steps = planner.next_fixes(self._collect(active, ws.ctx), active, cycle)
            if not steps:
                break
            self.reporter.phase(f"Fix  (cycle {cycle})")
            self.reporter.plan(steps)
            cycle_edits = self._fix(steps, {r.id: r for r in active}, ws, session)
            if not self.apply or not cycle_edits:
                break
            latest = self._verify(ws, session, planner, checks, categories, f"cycle {cycle}")
            regressions = latest.regressions(baseline)
            if regressions:
                self._revert_edits(cycle_edits, ws.editor)
                for s in planner.steps:
                    if s.kind == "fix" and s.cycle == cycle and s.status == "applied":
                        s.status = "reverted"
                self._verify(ws, session, planner, checks, categories, f"after revert of cycle {cycle}")
                session.set_findings(self._collect(active, ws.ctx))
                session.finish("needs_human", f"cycle {cycle} broke {', '.join(regressions)}; its edits were "
                                              "reverted")
                return

        remaining = self._collect(active, ws.ctx)
        self._finish(session, latest, remaining)
        self._git(ws, session)

    def _apply_plan(self, plan: Session, session: Session) -> None:
        intents = plan.data["intents"]
        session.data["intents"] = intents
        ws = self._setup(session, intents)
        pending = [e for e in plan.edits if not e.applied]
        if not pending:
            session.finish("clean", "the plan has no pending edits")
            return
        self.reporter.phase(f"Apply  ({len(pending)} planned edit(s))")
        applied: list[Edit] = []
        for edit in pending:
            try:
                ws.editor.apply(edit, session.backup_dir)
            except (EditError, OSError) as exc:
                self._revert_edits(applied, ws.editor)
                session.edits = applied
                session.finish("needs_human", f"{exc}; nothing from the plan was kept. Run `plan` again.")
                return
            applied.append(edit)
            session.edits.append(edit)
            self.reporter.edit(edit)
        active = [r for r in ws.rules if r.intents & set(intents)]
        checks = build_checks(ws.profile, self.config["checks"], self.registry)
        latest = self._verify(ws, session, None, checks, categories_for(set(intents)), "after apply")
        before = {c["name"]: c["status"] for c in (plan.data["verifications"] or [{"checks": []}])[0]["checks"]}
        regressions = [c.name for c in latest.checks if c.failed and before.get(c.name) == "pass"]
        if regressions:
            self._revert_edits(applied, ws.editor)
            session.finish("needs_human", f"applying the plan broke {', '.join(regressions)}; edits were reverted")
            return
        self._finish(session, latest, self._collect(active, ws.ctx))
        self._git(ws, session)

    def _fix(self, steps, rules: dict[str, Rule], ws: Workspace, session: Session) -> list[Edit]:
        edits = []
        for step in steps:
            try:
                changes = rules[step.rule].fix(ws.ctx)
            except Exception as exc:  # a broken rule must not take the whole run down
                step.status, step.note = "failed", f"rule error: {type(exc).__name__}: {exc}"
                self.reporter.step(step)
                continue
            for path in sorted(changes):
                if is_ignored(path, ws.profile.ignore):
                    continue
                edit = None
                try:
                    edit = ws.editor.propose(path, changes[path], rule=step.rule, reason=rules[step.rule].summary)
                    if edit and self.apply:
                        ws.editor.apply(edit, session.backup_dir)
                except (EditError, OSError) as exc:
                    if edit:
                        ws.editor.revert(edit)  # drop the pending overlay so later rules see the real file
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

    def _revert_edits(self, edits: list[Edit], editor: Editor) -> None:
        for edit in reversed(edits):
            try:
                editor.revert(edit)
            except EditError as exc:
                self.reporter.warn(str(exc))

    def _git(self, ws: Workspace, session: Session) -> None:
        """Record the diff summary; stage/commit cloudy's own edits when asked (--stage / --commit)."""
        applied = [e for e in session.edits if e.applied]
        if not ws.profile.git or not applied:
            return
        session.data["git_diff_stat"] = Git(ws.executor).diff_stat(sorted({e.path for e in applied}))
        if self.git_action:
            self._stage_and_commit(Git(ws.executor), session, commit=self.git_action == "commit")

    def commit_session(self, session: Session, *, commit: bool = True) -> dict:
        """Stage (and commit) the files an applied session changed. Used by the REPL's `commit` command."""
        git = Git(Executor(self.root, timeout=self.config["timeout"], deny=self.config["deny"]))
        if not git.available():
            session.data["git"] = {"staged": [], "refused": {}, "commit": None, "error": "not a git repository"}
        else:
            self._stage_and_commit(git, session, commit=commit)
        session.save()
        return session.data["git"]

    def _stage_and_commit(self, git: Git, session: Session, *, commit: bool) -> None:
        """Never stages files that had user changes before the run, never commits foreign staged files, never pushes."""
        applied = [e for e in session.edits if e.applied]
        git_state = (session.data.get("profile") or {}).get("git") or {}
        outcome = git.stage(applied, git_state.get("dirty", []))
        if commit:
            conventional = git_state.get("commit_style") == "conventional commits"
            outcome = git.commit(outcome, commit_message(session.data["task"], applied, conventional))
        session.data["git"] = asdict(outcome)

    def _finish(self, session: Session, latest: Verification, remaining: list[Finding]) -> None:
        session.set_findings(remaining)
        self.reporter.findings([f for f in remaining if not f.fixable], "Needs a human decision")
        self.reporter.failures([c for c in latest.checks if c.failed])
        fixable = [f for f in remaining if f.fixable]
        failing = [c.name for c in latest.checks if c.failed]
        manual = len(remaining) - len(fixable)
        if session.data["mode"] == "safe" and session.edits:
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
