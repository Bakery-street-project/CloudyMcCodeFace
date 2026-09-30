"""Safe git helpers: read-only summaries, staging of cloudy's own edits, and a local commit. Never pushes.

Refusals are deliberate: cloudy never stages a file that already had uncommitted user changes (the commit would
mix them in), never commits when something else is already staged, and never amends or skips hooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .executor import Executor
from .models import Edit

SUBJECT_LIMIT = 72


@dataclass
class GitOutcome:
    staged: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)
    commit: str | None = None
    error: str | None = None


def porcelain_paths(lines: list[str]) -> set[str]:
    """Paths from `git status --porcelain` lines (rename targets for `R old -> new`)."""
    return {line[3:].split(" -> ")[-1].strip('"') for line in lines if len(line) > 3}


def commit_message(task: str, edits: list[Edit], conventional: bool) -> str:
    """Deterministic: the same task and edits always give the same message."""
    applied = [e for e in edits if e.applied]
    summary = " ".join(task.split()) or "apply fixes"
    subject = f"chore: {summary[0].lower()}{summary[1:]}" if conventional else summary[0].upper() + summary[1:]
    if len(subject) > SUBJECT_LIMIT:
        subject = subject[:SUBJECT_LIMIT - 1].rstrip() + "…"
    rules: dict[str, set[str]] = {}
    for edit in applied:
        rules.setdefault(edit.rule, set()).add(edit.path)
    model_edits = any(rule.startswith("ai.") for rule in rules)
    body = ["Applied by cloudy" + (":" if model_edits else " (deterministic rules, no model):"), ""]
    body += [f"- {rule}: {', '.join(sorted(paths))}" for rule, paths in sorted(rules.items())]
    if model_edits:
        body += ["", "ai.proposal edits were proposed by a local model and approved by a human before being applied."]
    return "\n".join([subject, "", *body]) + "\n"


class Git:
    def __init__(self, executor: Executor) -> None:
        self.executor = executor

    def _git(self, *args: str):
        return self.executor.run(["git", *args], timeout=60)

    def available(self) -> bool:
        return bool(self.executor.which("git")) and self._git("rev-parse", "--is-inside-work-tree").ok

    def status(self) -> list[str]:
        result = self._git("status", "--porcelain")
        return result.stdout.splitlines() if result.ok else []

    def diff_stat(self, paths: list[str] | None = None) -> str:
        result = self._git("diff", "--stat", "--", *(paths or []))
        return result.stdout.rstrip() if result.ok else ""

    def staged(self) -> list[str]:
        result = self._git("diff", "--cached", "--name-only")
        return result.stdout.split() if result.ok else []

    def stage(self, edits: list[Edit], dirty_before: list[str]) -> GitOutcome:
        """Stage only files cloudy changed, and only if they had no uncommitted user changes before the run."""
        outcome = GitOutcome()
        user_changed = porcelain_paths(dirty_before)
        candidates = sorted({e.path for e in edits if e.applied})
        for path in candidates:
            if path in user_changed:
                outcome.refused[path] = "had uncommitted changes before cloudy ran; stage it yourself after review"
        allowed = [p for p in candidates if p not in outcome.refused]
        if allowed:
            result = self._git("add", "--", *allowed)
            if not result.ok:
                outcome.error = f"git add failed: {result.stderr.strip() or result.exit_code}"
                return outcome
        outcome.staged = allowed
        return outcome

    def commit(self, outcome: GitOutcome, message: str) -> GitOutcome:
        """Commit exactly what `stage` staged; refuse if anything else is in the index."""
        if outcome.error or not outcome.staged:
            outcome.error = outcome.error or "nothing to commit"
            return outcome
        others = sorted(set(self.staged()) - set(outcome.staged))
        if others:
            outcome.error = f"refusing to commit: other files are staged ({', '.join(others)})"
            return outcome
        result = self._git("commit", "-m", message)
        if not result.ok:
            lines = (result.stderr or result.stdout).strip().splitlines()
            outcome.error = f"git commit failed: {lines[-1] if lines else f'exit {result.exit_code}'}"
            return outcome
        outcome.commit = self._git("rev-parse", "--short", "HEAD").stdout.strip()
        return outcome
