"""Verifier agent: runs the project's existing checks and interprets their results."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .editor import validate
from .executor import CommandBlocked, Executor
from .explorer import RepoProfile
from .models import CheckResult, CommandResult

INTENT_CATEGORIES = {
    "fix_ci": {"static"},
    "sync_docs": {"static"},
    "fix_lint": {"static", "lint"},
    "fix_tests": {"static", "test"},
}
PYTEST_SUMMARY = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$")
PY_LOCATION = re.compile(r"^([^/\s]\S*\.py):(\d+): in (\S+)")
PY_ERROR = re.compile(r"^E\s+(\w+(?:Error|Exception)\b.*)$")
NPM_PLACEHOLDER_TEST = "no test specified"


@dataclass
class Check:
    name: str
    category: str
    command: list[str] | str | None
    interpret: Callable[[CommandResult], CheckResult] | None = None
    skip_reason: str = ""


@dataclass
class Verification:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(c.failed for c in self.checks)

    def by_name(self) -> dict[str, CheckResult]:
        return {c.name: c for c in self.checks}

    def regressions(self, baseline: Verification) -> list[str]:
        before = baseline.by_name()
        return [c.name for c in self.checks if c.failed and c.name in before and before[c.name].status == "pass"]


def _tail(text: str, lines: int = 12) -> list[str]:
    return [line for line in text.strip().splitlines()[-lines:] if line.strip()]


def generic(name: str, category: str) -> Callable[[CommandResult], CheckResult]:
    def interpret(result: CommandResult) -> CheckResult:
        status = "pass" if result.ok else "error" if result.timed_out or result.exit_code in (126, 127) else "fail"
        summary = "ok" if result.ok else f"exit {result.exit_code}" + (" (timed out)" if result.timed_out else "")
        return CheckResult(name, category, status, summary, result.cmd,
                           [] if result.ok else _tail(result.output), result.output[-4000:])
    return interpret


def interpret_pytest(result: CommandResult) -> CheckResult:
    output = result.output
    lines = output.strip().splitlines()
    last = lines[-1].strip("= ") if lines else ""
    if result.exit_code == 5:
        return CheckResult("pytest", "test", "skip", "no tests collected", result.cmd, [], output[-4000:])
    details = []
    for line in lines:
        if match := PYTEST_SUMMARY.match(line.strip()):
            kind, node, reason = match.groups()
            details.append(f"{kind} {node}" + (f" — {reason}" if reason else ""))
        elif match := PY_LOCATION.match(line.strip()):
            details.append(f"at {match.group(1)}:{match.group(2)} in {match.group(3)}")
        elif match := PY_ERROR.match(line.strip()):
            details.append(match.group(1))
    base = generic("pytest", "test")(result)
    base.summary = last or base.summary
    base.details = list(dict.fromkeys(details))[:30] or base.details
    return base


def interpret_ruff(result: CommandResult) -> CheckResult:
    base = generic("ruff", "lint")(result)
    diagnostics = [line for line in result.stdout.splitlines() if re.match(r"^\S+:\d+:\d+: ", line)]
    if diagnostics:
        base.summary = f"{len(diagnostics)} issue(s)"
        base.details = diagnostics[:30]
    return base


def config_syntax(files: Iterable[str], read: Callable[[str], str | None]) -> CheckResult:
    errors = []
    for path in files:
        if path.endswith((".yml", ".yaml", ".json", ".toml")) and not path.endswith("package-lock.json"):
            text = read(path)
            if text is not None and (error := validate(path, text)):
                errors.append(f"{path}: {error}")
    return CheckResult("config-syntax", "static", "fail" if errors else "pass",
                       f"{len(errors)} invalid file(s)" if errors else "all YAML/JSON/TOML files parse",
                       "(built-in)", errors)


def build_checks(profile: RepoProfile, custom: dict[str, str] | None = None) -> list[Check]:
    """Checks the project already supports. A missing tool yields a skipped check, never a guess."""
    checks: list[Check] = []
    python = "Python" in profile.languages

    def tool_check(name: str, category: str, tool: str, args: list[str], applies: bool,
                   interpret: Callable[[CommandResult], CheckResult] | None = None) -> None:
        if not applies:
            return
        if not profile.has_tool(tool):
            checks.append(Check(name, category, None, skip_reason=f"`{tool}` is not installed"))
        else:
            checks.append(Check(name, category, [profile.tool_path(tool), *args],
                                interpret or generic(name, category)))

    tool_check("ruff", "lint", "ruff", ["check", "--no-cache", "--output-format=concise", "."],
               python and ("ruff" in profile.linters or "ruff" in profile.ci_tools or profile.has_tool("ruff")),
               interpret_ruff)
    tool_check("mypy", "lint", "mypy", ["."], python and "mypy" in profile.linters)
    tool_check("pytest", "test", "pytest", ["-q", "-rfE", "--tb=short", "-p", "no:cacheprovider"],
               python or "pytest" in profile.ci_tools, interpret_pytest)

    if "package.json" in profile.files:
        root = Path(profile.root)
        try:
            package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            package = {}
        declared = package.get("scripts", {}) if isinstance(package, dict) else {}
        for script, category in (("lint", "lint"), ("test", "test")):
            body = declared.get(script)
            if not isinstance(body, str) or NPM_PLACEHOLDER_TEST in body:
                continue
            if not (root / "node_modules").is_dir():
                checks.append(Check(f"npm {script}", category, None,
                                    skip_reason="node_modules missing; run `npm install` once while online"))
            else:
                tool_check(f"npm {script}", category, "npm", ["run", "--silent", script], True)
    tool_check("tsc", "lint", "tsc", ["--noEmit"], "tsc" in profile.linters)
    tool_check("go vet", "lint", "go", ["vet", "./..."], "go.mod" in profile.files)
    tool_check("go test", "test", "go", ["test", "./..."], "go.mod" in profile.files)
    tool_check("cargo test", "test", "cargo", ["test", "--offline", "--quiet"], "Cargo.toml" in profile.files)
    if "test" in profile.scripts.get("Makefile", []) and not any(c.category == "test" for c in checks):
        tool_check("make test", "test", "make", ["test"], True)
    for name, command in (custom or {}).items():
        category = "test" if "test" in name else "lint"
        checks.append(Check(name, category, command, generic(name, category)))
    return checks


def run_checks(
    checks: list[Check],
    executor: Executor,
    profile: RepoProfile,
    read: Callable[[str], str | None],
    categories: set[str] | None = None,
    on_check: Callable[[CheckResult], None] | None = None,
) -> Verification:
    verification = Verification()

    def add(result: CheckResult) -> None:
        verification.checks.append(result)
        if on_check:
            on_check(result)

    if categories is None or "static" in categories:
        add(config_syntax(profile.files, read))
    for check in checks:
        if categories is not None and check.category not in categories:
            continue
        if check.command is None:
            add(CheckResult(check.name, check.category, "skip", check.skip_reason))
            continue
        try:
            add(check.interpret(executor.run(check.command)))
        except CommandBlocked as exc:
            add(CheckResult(check.name, check.category, "error", str(exc)))
    return verification


def categories_for(intents: set[str]) -> set[str] | None:
    wanted: set[str] = set()
    for intent in intents:
        wanted |= INTENT_CATEGORIES.get(intent, set())
    return wanted or None
