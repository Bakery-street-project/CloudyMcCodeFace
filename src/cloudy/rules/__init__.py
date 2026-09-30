"""Rule system. A rule inspects the repo (check) and may return new file contents (fix).

Rules are deterministic: the same repository state always yields the same findings and fixes.
A rule only marks a finding fixable when the correct change is unambiguous; everything else is reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from ..editor import Editor
from ..executor import Executor
from ..explorer import RepoProfile
from ..models import CheckResult, Finding


@dataclass
class RepoContext:
    root: Path
    profile: RepoProfile
    editor: Editor
    executor: Executor
    checks: dict[str, CheckResult] = field(default_factory=dict)

    def read(self, rel: str) -> str | None:
        return self.editor.read(rel)

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def match(self, *patterns: str) -> list[str]:
        return [f for f in self.profile.files if any(fnmatch(f, p) for p in patterns)]


class Rule:
    id: str = ""
    intents: frozenset[str] = frozenset()
    summary: str = ""

    def check(self, ctx: RepoContext) -> list[Finding]:
        raise NotImplementedError

    def fix(self, ctx: RepoContext) -> dict[str, str]:
        """Return {path: new_content} for fixable findings. Default: nothing is auto-fixable."""
        return {}

    def finding(self, path: str, message: str, line: int | None = None, fixable: bool = False) -> Finding:
        return Finding(self.id, path, message, line, fixable)


def all_rules() -> list[Rule]:
    """All rules in the order fixes are applied (config before docs before code)."""
    from . import ci, docs, python, repo

    return [
        ci.MaskedFailures(), ci.WorkflowPermissions(), ci.DependabotEcosystems(), repo.CodeOwners(),
        docs.LicenseMismatch(), docs.CloneUrl(), docs.BrokenLinks(), docs.PhantomCommands(),
        docs.PlaceholderContacts(), docs.DuplicatePolicies(),
        python.RuffAutofix(), python.PytestPythonPath(),
    ]
