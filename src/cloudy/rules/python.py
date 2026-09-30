"""Python rules. Fixes come from the project's own tools (ruff) or from narrow, well-understood recipes."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..editor import validate
from . import RepoContext, Rule

MISSING_MODULE = re.compile(r"ModuleNotFoundError: No module named '([\w.]+)'")


def ruff_diagnostics(ctx: RepoContext) -> list[dict]:
    if not ctx.profile.has_tool("ruff") or "Python" not in ctx.profile.languages:
        return []
    result = ctx.executor.run([ctx.profile.tool_path("ruff"), "check", "--no-cache", "--output-format=json", "."])
    try:
        raw = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    diagnostics = []
    for item in raw:
        path = Path(item.get("filename", ""))
        rel = path.relative_to(ctx.root).as_posix() if path.is_absolute() and path.is_relative_to(ctx.root) \
            else path.as_posix()
        diagnostics.append({"path": rel, "line": (item.get("location") or {}).get("row"),
                            "code": item.get("code") or "", "msg": item.get("message", ""),
                            "fix": (item.get("fix") or {}).get("applicability") == "safe"})
    return diagnostics


class RuffAutofix(Rule):
    id = "python.ruff-autofix"
    intents = frozenset({"fix_lint"})
    summary = "Apply ruff's own safe autofixes, using the project's ruff configuration"

    def check(self, ctx: RepoContext):
        return [self.finding(d["path"], f"{d['code']} {d['msg']}", d["line"], d["fix"])
                for d in ruff_diagnostics(ctx)]

    def fix(self, ctx: RepoContext):
        changes = {}
        for path in sorted({d["path"] for d in ruff_diagnostics(ctx) if d["fix"]}):
            source = ctx.read(path)
            if source is None:
                continue
            result = ctx.executor.run(
                [ctx.profile.tool_path("ruff"), "check", "--no-cache", "--fix", "--stdin-filename", path, "-"],
                stdin=source,
            )
            # ruff prints the fixed source on stdout (exit 1 means diagnostics remain, which is fine).
            if result.exit_code in (0, 1) and result.stdout and result.stdout != source \
                    and not validate(path, result.stdout):
                changes[path] = result.stdout
        return changes


class PytestPythonPath(Rule):
    id = "python.pytest-pythonpath"
    intents = frozenset({"fix_tests"})
    summary = "Tests that import a src-layout package need pytest's `pythonpath` set"

    def _module(self, ctx: RepoContext) -> str | None:
        check = ctx.checks.get("pytest")
        if not check or not check.failed:
            return None
        for name in MISSING_MODULE.findall(check.output):
            top = name.split(".")[0]
            if (ctx.root / "src" / top).is_dir() or (ctx.root / "src" / f"{top}.py").is_file():
                return top
        return None

    def check(self, ctx: RepoContext):
        module = self._module(ctx)
        if not module:
            return []
        pyproject = ctx.read("pyproject.toml")
        other = [f for f in ("pytest.ini", "tox.ini", "setup.cfg") if ctx.exists(f)]
        fixable = pyproject is not None and "pythonpath" not in pyproject and not other
        return [self.finding("pyproject.toml" if pyproject is not None else "tests",
                             f"tests cannot import `{module}` from src/; pytest needs pythonpath = [\"src\"]"
                             + ("" if fixable else " (configure it in your pytest config file)"),
                             fixable=fixable)]

    def fix(self, ctx: RepoContext):
        if not any(f.fixable for f in self.check(ctx)):
            return {}
        text = ctx.read("pyproject.toml") or ""
        header = "[tool.pytest.ini_options]"
        if re.search(rf"^{re.escape(header)}\s*$", text, re.M):
            new = re.sub(rf"^{re.escape(header)}\s*$", f'{header}\npythonpath = ["src"]', text, count=1, flags=re.M)
        else:
            new = text.rstrip("\n") + f'\n\n{header}\npythonpath = ["src"]\n'
        return {"pyproject.toml": new}
