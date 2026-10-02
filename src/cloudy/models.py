"""Plain data types shared by all agents."""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unified_diff(path: str, before: str, after: str) -> str:
    def lines(text: str) -> list[str]:
        out = text.splitlines(keepends=True)
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n\\ No newline at end of file\n"
        return out

    return "".join(difflib.unified_diff(lines(before), lines(after), f"a/{path}", f"b/{path}"))


@dataclass
class CommandResult:
    cmd: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


@dataclass
class Finding:
    rule: str
    path: str
    message: str
    line: int | None = None
    fixable: bool = False
    hint: str = ""  # what a human should look at when this is not auto-fixable

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path


@dataclass
class Edit:
    path: str
    before: str
    after: str
    rule: str
    reason: str
    applied: bool = False

    @property
    def diff(self) -> str:
        return unified_diff(self.path, self.before, self.after)


@dataclass
class CheckResult:
    name: str
    category: str  # static | lint | test | rules
    status: str  # pass | fail | skip | error
    summary: str = ""
    command: str = ""
    details: list[str] = field(default_factory=list)
    output: str = ""
    hint: str = ""

    @property
    def failed(self) -> bool:
        return self.status in ("fail", "error")
