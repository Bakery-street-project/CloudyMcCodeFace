"""Planner agent: turns a task into intents, and findings into an ordered list of fix steps."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Finding

INTENT_PATTERNS = {
    "fix_ci": r"\bci\b|\bworkflows?\b|\bpipelines?\b|\bgithub actions\b|\bdependabot\b|\bcodeowners\b|\bconfig",
    "sync_docs": r"\breadme\b|\bdocs?\b|\bdocumentation\b|\bmatch(es)? reality\b|\blicen[cs]e\b|\bcontributing\b",
    "fix_lint": r"\blint(s|er|ers|ing)?\b|\bruff\b|\bstyle\b|\bwarnings?\b|\bformat(ting|ter)?\b|\bfmt\b",
    "fix_tests": r"\btests?\b|\bpytest\b|\bfailing\b|\bfailures?\b",
}
ANALYZE = r"\banaly[sz]|\bwhat tools\b|\bconventions?\b|\bexplore\b|\binspect\b|\bdescribe\b|\boverview\b|\baudit\b"
EVERYTHING = r"\bproduction\b|\bshippable\b|\beverything\b|\ball (issues|checks|problems)\b"
FIX_INTENTS = tuple(INTENT_PATTERNS)


def classify(task: str) -> list[str]:
    """Map a free-text task to intents. Fix intents win over analysis; nothing recognised means analyze."""
    text = task.lower()
    if re.search(EVERYTHING, text):
        return list(FIX_INTENTS)
    intents = [intent for intent, pattern in INTENT_PATTERNS.items() if re.search(pattern, text)]
    if intents and re.search(ANALYZE, text) and not re.search(r"\b(fix|update|clean|apply|repair|make)\b", text):
        return ["analyze"]
    return intents or ["analyze"]


@dataclass
class Step:
    id: int
    kind: str  # explore | verify | fix | report
    title: str
    rule: str | None = None
    cycle: int = 0
    status: str = "pending"  # pending | done | proposed | applied | skipped | failed | reverted
    note: str = ""


class Planner:
    def __init__(self, intents: list[str]) -> None:
        self.intents = intents
        self.steps: list[Step] = []
        self._attempted: dict[str, tuple] = {}

    def add(self, kind: str, title: str, rule: str | None = None, cycle: int = 0) -> Step:
        step = Step(len(self.steps) + 1, kind, title, rule, cycle)
        self.steps.append(step)
        return step

    def next_fixes(self, findings: list[Finding], rules: list, cycle: int) -> list[Step]:
        """Plan one fix step per rule with fixable findings, skipping rules that made no progress last time."""
        planned = []
        for rule in rules:
            signature = tuple(sorted((f.path, f.line or 0, f.message) for f in findings
                                     if f.rule == rule.id and f.fixable))
            if not signature or self._attempted.get(rule.id) == signature:
                continue
            self._attempted[rule.id] = signature
            planned.append(self.add("fix", f"{rule.summary} ({len(signature)} finding(s))", rule.id, cycle))
        return planned
