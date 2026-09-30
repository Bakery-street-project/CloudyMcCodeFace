"""The only actions a local model can take. Read-only tools plus `propose_edit`, which creates a pending diff that
nothing writes until a human applies it. There is no tool that writes, applies, commits or runs a shell command.

Every reply must be one JSON object `{"tool": ..., "args": {...}}`. The engine constrains output to RESPONSE_SCHEMA;
`parse` validates again, because a malformed reply must be rejected even if constraining fails.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from ..editor import EditError, Editor
from ..explorer import RepoProfile, is_ignored
from ..models import Edit
from ..report import first_location

MAX_READ_LINES = 200
MAX_RESULT_CHARS = 6000
MAX_SEARCH_HITS = 50
MAX_FILE_BYTES = 1_000_000
AI_RULE = "ai.proposal"

# tool -> {argument: (type, required)}
TOOLS: dict[str, dict[str, tuple[type, bool]]] = {
    "read_file": {"path": (str, True), "start": (int, False), "end": (int, False)},
    "search": {"pattern": (str, True), "glob": (str, False)},
    "list_findings": {},
    "run_checks": {},
    "propose_edit": {"path": (str, True), "find": (str, True), "replace": (str, True), "reason": (str, True)},
    "answer": {"text": (str, True)},
}
JSON_TYPES = {str: "string", int: "integer"}
RESPONSE_SCHEMA = {"anyOf": [
    {"type": "object", "additionalProperties": False, "required": ["tool", "args"], "properties": {
        "tool": {"const": name},
        "args": {"type": "object", "additionalProperties": False,
                 "required": [a for a, (_, req) in spec.items() if req],
                 "properties": {a: {"type": JSON_TYPES[t]} for a, (t, _) in spec.items()}}}}
    for name, spec in TOOLS.items()
]}


class ToolError(Exception):
    """Invalid call or failed tool; the message goes back to the model."""


def parse(text: str) -> tuple[str, dict]:
    """Validate one model reply strictly: known tool, known arguments, right types."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolError(f"reply is not valid JSON ({exc.msg}); answer with one JSON object") from None
    if not isinstance(data, dict) or set(data) != {"tool", "args"} or not isinstance(data["args"], dict):
        raise ToolError('reply must be exactly {"tool": <name>, "args": {...}}')
    tool, args = data["tool"], data["args"]
    if tool not in TOOLS:
        raise ToolError(f"unknown tool {tool!r}; allowed: {', '.join(TOOLS)}. You cannot run shell commands.")
    spec = TOOLS[tool]
    extra = sorted(set(args) - set(spec))
    if extra:
        raise ToolError(f"{tool}: unexpected argument(s) {', '.join(extra)}")
    for name, (kind, required) in spec.items():
        if name not in args:
            if required:
                raise ToolError(f"{tool}: missing argument {name!r}")
        elif not isinstance(args[name], kind) or isinstance(args[name], bool):
            raise ToolError(f"{tool}: {name!r} must be a {JSON_TYPES[kind]}")
    return tool, args


def clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[… {len(text) - limit} characters cut]"


@dataclass
class Toolbox:
    profile: RepoProfile
    editor: Editor
    findings: list[dict] = field(default_factory=list)
    failing_checks: list[dict] = field(default_factory=list)
    run_checks_fn: object = None  # callable returning list[dict] of check results
    model_name: str = "local model"
    proposals: list[Edit] = field(default_factory=list)

    def run(self, tool: str, args: dict) -> str:
        return clip(getattr(self, f"_{tool}")(**args))

    def _path(self, path: str) -> str:
        rel = path.strip().removeprefix("./")
        try:
            self.editor.resolve(rel)
        except EditError as exc:
            raise ToolError(str(exc)) from None
        if is_ignored(rel, self.profile.ignore) or rel not in self.profile.files:
            raise ToolError(f"{rel}: not a file cloudy may read (missing, ignored or outside the repository)")
        return rel

    def _read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        rel = self._path(path)
        text = self.editor.read(rel)
        if text is None:
            raise ToolError(f"{rel}: not a text file")
        lines = text.splitlines()
        start = max(1, start)
        end = min(len(lines), end or start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
        body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))
        return f"{rel} lines {start}-{end} of {len(lines)}:\n{body}"

    def _search(self, pattern: str, glob: str = "*") -> str:
        if len(pattern) > 200:
            raise ToolError("search: pattern longer than 200 characters")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"search: invalid regular expression: {exc}") from None
        hits = []
        for rel in self.profile.files:
            if not fnmatch(rel, glob) or _too_big(self.profile.root, rel):
                continue
            for number, line in enumerate((self.editor.read(rel) or "").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{rel}:{number}: {line.strip()[:160]}")
                    if len(hits) >= MAX_SEARCH_HITS:
                        return "\n".join(hits) + f"\n[stopped at {MAX_SEARCH_HITS} hits]"
        return "\n".join(hits) or "no matches"

    def _list_findings(self) -> str:
        rows = [f"{f['path']}:{f['line'] or ''} {f['rule']} {'(fixable)' if f['fixable'] else ''} {f['message']}"
                for f in self.findings]
        rows += [f"check {c['name']} {c['status']}"
                 + (f" at {first_location(c['details'])}" if first_location(c["details"]) else "")
                 + (f": {'; '.join(c['details'][:3])}" if c["details"] else "") for c in self.failing_checks]
        return "\n".join(rows) or "no findings and no failing checks"

    def _run_checks(self) -> str:
        if self.run_checks_fn is None:
            raise ToolError("run_checks is not available here")
        checks = self.run_checks_fn()
        self.failing_checks = [c for c in checks if c["status"] in ("fail", "error")]
        return "\n".join(f"{c['status']:5} {c['name']}: {c['summary']}"
                         + (f"\n      {'; '.join(c['details'][:5])}" if c["status"] in ("fail", "error") else "")
                         for c in checks) or "no checks apply to this repository"

    def _propose_edit(self, path: str, find: str, replace: str, reason: str) -> str:
        rel = self._path(path)
        current = self.editor.read(rel) or ""
        count = current.count(find) if find else 0
        if count != 1:
            raise ToolError(f"propose_edit: `find` must match exactly once in {rel}; it matches {count} time(s). "
                            "Quote a longer, exact snippet from read_file.")
        try:
            edit = self.editor.propose(rel, current.replace(find, replace, 1), rule=AI_RULE,
                                       reason=f"proposed by {self.model_name} (not a deterministic rule): {reason}")
        except EditError as exc:
            raise ToolError(f"propose_edit rejected: {exc}") from None
        if edit is None:
            raise ToolError("propose_edit: the replacement is identical to the current text")
        self.proposals.append(edit)
        return f"proposed edit #{len(self.proposals)} for {rel} (pending; a human reviews and applies it)"

    def _answer(self, text: str) -> str:
        return text


def _too_big(root: str, rel: str) -> bool:
    try:
        return (Path(root) / rel).stat().st_size > MAX_FILE_BYTES
    except OSError:
        return True
