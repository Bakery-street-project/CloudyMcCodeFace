"""Repository metadata rules."""

from __future__ import annotations

import re

from . import RepoContext, Rule

CODEOWNERS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
HANDLE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:/[A-Za-z0-9._-]+)?$")


def _is_owner(token: str) -> bool:
    return token.startswith("@") or "@" in token


class CodeOwners(Rule):
    id = "repo.codeowners"
    intents = frozenset({"fix_ci"})
    summary = "CODEOWNERS lines need a path pattern followed by @owners"
    hint = "Write `<pattern> @user-or-team` on that line."

    def _fixed_line(self, ctx: RepoContext, line: str) -> tuple[str | None, str | None]:
        """Return (problem, fixed line or None if not safely fixable)."""
        tokens = line.split()
        if len(tokens) == 1:
            token = tokens[0]
            # A lone token that is an existing path is a valid "unowned" pattern.
            if _is_owner(token) or ctx.exists(token.strip("/")) or not HANDLE.match(token):
                return (f"`{token}` has no path pattern", None) if _is_owner(token) else (None, None)
            return f"`{token}` looks like an owner with no path pattern", f"* @{token}"
        bad = [t for t in tokens[1:] if not _is_owner(t) and not t.startswith("#")]
        if not bad:
            return None, None
        owners = [f"@{t}" if t in bad and HANDLE.match(t) else t for t in tokens[1:]]
        fixable = all(HANDLE.match(t) for t in bad)
        return f"owners {', '.join(bad)} are missing `@`", " ".join([tokens[0], *owners]) if fixable else None

    def _scan(self, ctx: RepoContext):
        for path in CODEOWNERS:
            text = ctx.read(path)
            if text is None:
                continue
            for index, raw in enumerate(text.splitlines()):
                line = raw.strip()
                if line and not line.startswith("#"):
                    problem, fixed = self._fixed_line(ctx, line)
                    if problem:
                        yield path, index, problem, fixed

    def check(self, ctx: RepoContext):
        return [self.finding(path, problem, index + 1, fixed is not None)
                for path, index, problem, fixed in self._scan(ctx)]

    def fix(self, ctx: RepoContext):
        changes: dict[str, str] = {}
        for path, index, _, fixed in self._scan(ctx):
            if fixed is None:
                continue
            lines = (changes.get(path) or ctx.read(path) or "").splitlines(keepends=True)
            lines[index] = fixed + "\n"
            changes[path] = "".join(lines)
        return changes


RULES = [CodeOwners()]
