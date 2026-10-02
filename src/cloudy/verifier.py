"""Verifier agent: runs the project's existing checks and interprets their results."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .editor import validate
from .executor import CommandBlocked, Executor
from .explorer import SKIP_DIRS, RepoProfile
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
    cwd: str | None = None


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


HINTS = {
    "test": "A test fails: compare the assertion's expected value with the code under test at the first location. "
            "cloudy never changes application logic.",
    "lint": "No safe autofix covers this: fix the reported lines by hand, then re-run the check.",
    "static": "Fix the syntax at the reported location.",
    "timeout": "The command timed out: raise `timeout` in cloudy.toml or run it manually to see where it hangs.",
    "missing": "The tool could not start: install it or set its path under [tools] in cloudy.toml.",
}


def generic(name: str, category: str) -> Callable[[CommandResult], CheckResult]:
    def interpret(result: CommandResult) -> CheckResult:
        status = "pass" if result.ok else "error" if result.timed_out or result.exit_code in (126, 127) else "fail"
        summary = "ok" if result.ok else f"exit {result.exit_code}" + (" (timed out)" if result.timed_out else "")
        hint = "" if result.ok else HINTS["timeout"] if result.timed_out else HINTS["missing"] \
            if result.exit_code in (126, 127) else HINTS.get(category, "")
        return CheckResult(name, category, status, summary, result.cmd,
                           [] if result.ok else _tail(result.output), result.output[-4000:], hint)
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


def match_output(result: CommandResult, name: str, category: str, patterns: list[re.Pattern],
                 summary: str | None = None) -> CheckResult:
    """Generic result, with details replaced by the lines matching any pattern (groups joined if present).

    Public helper for plugin interpreters.
    """
    base = generic(name, category)(result)
    details = []
    for line in result.output.splitlines():
        for pattern in patterns:
            if match := pattern.search(line):
                details.append(" ".join(g for g in match.groups() if g) if match.groups() else line.strip())
                break
    if details:
        base.details = list(dict.fromkeys(details))[:30]
    if summary and base.failed:
        base.summary = summary
    return base


JS_TEST_PATTERNS = [re.compile(p) for p in (
    r"^\s*(not ok \d+ - .+)$",  # node:test / TAP
    r"^\s*location: '(.+)'$",
    # jest / vitest; also node:test spec reporter (node >=25 default), whose header line "✖ failing tests:"
    # is excluded by the no-trailing-colon group.
    r"^\s*(?:FAIL|✕|✖|×)\s+(.+?[^:])(?: \(\d[\d.]*m?s\))?$",
    r"^test at (\S+:\d+:\d+)$",  # spec reporter failure location
    r"^\s*●\s+(.+)$",
    r"^\s*((?:AssertionError|SyntaxError|Error|TypeError|ReferenceError)\b.*)$",
    r"^\s*((?:expected|actual|Expected|Received):?\s.*)$",
)]


def interpret_js_test(name: str) -> Callable[[CommandResult], CheckResult]:
    return lambda result: match_output(result, name, "test", JS_TEST_PATTERNS)


def interpret_eslint(result: CommandResult) -> CheckResult:
    base = generic("eslint", "lint")(result)
    try:
        files = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return base
    problems = [f"{f['filePath']}:{m.get('line', 0)} {m.get('ruleId') or 'error'} {m.get('message', '')}"
                for f in files for m in f.get("messages", [])]
    if problems:
        base.summary, base.details = f"{len(problems)} problem(s)", problems[:30]
    return base


def interpret_prettier(result: CommandResult) -> CheckResult:
    files = [line[7:].strip() for line in result.output.splitlines()
             if line.startswith("[warn] ") and "Code style issues" not in line]
    base = generic("prettier", "lint")(result)
    if files:
        base.summary, base.details = f"{len(files)} file(s) not formatted", files[:30]
    return base


def interpret_tsc(result: CommandResult) -> CheckResult:
    return match_output(result, "tsc", "lint", [re.compile(r"^(\S+\(\d+,\d+\): error TS\d+: .*)$")])


def interpret_gofmt(result: CommandResult) -> CheckResult:
    files = [f for f in result.stdout.splitlines() if f and not set(Path(f).parts) & SKIP_DIRS]
    base = generic("gofmt", "lint")(result)
    if result.ok and files:
        base.status, base.summary, base.details = "fail", f"{len(files)} file(s) not gofmt-formatted", files
    elif result.ok:
        base.summary = "all files formatted"
    return base


GO_PATTERNS = [re.compile(p) for p in (
    r"^\s*(--- FAIL: \S+)",
    r"^\s+(\S+\.go:\d+: .*)$",  # t.Errorf location + message
    r"^(?:vet: )?(?:\./)?(\S+\.go:\d+:\d+: .*)$",  # vet / compile errors
    r"^(panic: .*)$",
    r"^(FAIL\s+\S+)",
)]


def interpret_go(name: str, category: str) -> Callable[[CommandResult], CheckResult]:
    return lambda result: match_output(result, name, category, GO_PATTERNS)


CARGO_PATTERNS = [re.compile(p) for p in (
    r"^test (\S+) \.\.\. FAILED$",
    r"panicked at (\S+:\d+:\d+):?$",
    r"^\s*((?:left|right): .*)$",
    r"^(assertion .* failed.*)$",
    r"^(error(?:\[E\d+\])?: .*)$",
    r"^\s*--> (\S+:\d+:\d+)$",
    r"^(/?\S+\.rs)$",  # cargo fmt -l
)]


def interpret_cargo(name: str, category: str) -> Callable[[CommandResult], CheckResult]:
    return lambda result: match_output(result, name, category, CARGO_PATTERNS)


def _renamed(interpret: Callable[[CommandResult], CheckResult], name: str) -> Callable[[CommandResult], CheckResult]:
    def run(result: CommandResult) -> CheckResult:
        checked = interpret(result)
        checked.name = name
        return checked
    return run


def config_syntax(files: Iterable[str], read: Callable[[str], str | None]) -> CheckResult:
    errors = []
    for path in files:
        if path.endswith((".yml", ".yaml", ".json", ".toml")) and not path.endswith("package-lock.json"):
            text = read(path)
            if text is not None and (error := validate(path, text)):
                errors.append(f"{path}: {error}")
    return CheckResult("config-syntax", "static", "fail" if errors else "pass",
                       f"{len(errors)} invalid file(s)" if errors else "all YAML/JSON/TOML files parse",
                       "(built-in)", errors, hint=HINTS["static"] if errors else "")


def command_check(profile: RepoProfile, name: str, category: str, tool: str, args: list[str],
                  interpret: Callable[[CommandResult], CheckResult] | None = None, cwd: str | None = None) -> Check:
    """A check running `tool args` (in `cwd`); skipped with a reason when the tool is not installed.

    This is the helper plugins use from their `checks(profile)` function.
    """
    if not profile.has_tool(tool):
        return Check(name, category, None, skip_reason=f"`{tool}` is not installed")
    return Check(name, category, [profile.tool_path(tool), *args], interpret or generic(name, category), cwd=cwd)


def build_checks(profile: RepoProfile, custom: dict[str, str] | None = None, registry=None) -> list[Check]:
    """Checks the project already supports. A missing tool yields a skipped check, never a guess."""
    checks: list[Check] = []
    python = "Python" in profile.languages

    def tool_check(name: str, category: str, tool: str, args: list[str], applies: bool,
                   interpret: Callable[[CommandResult], CheckResult] | None = None, cwd: str | None = None) -> None:
        if applies:
            checks.append(command_check(profile, name, category, tool, args, interpret, cwd))

    tool_check("ruff", "lint", "ruff", ["check", "--no-cache", "--output-format=concise", "."],
               python and ("ruff" in profile.linters or "ruff" in profile.ci_tools or profile.has_tool("ruff")),
               interpret_ruff)
    def python_check(name: str, category: str, args: list[str], applies: bool,
                     interpret: Callable[[CommandResult], CheckResult] | None = None) -> None:
        """Prefer `<project python> -m <tool>`: a standalone tool install cannot import the project's deps."""
        if applies and name in profile.python_modules:
            checks.append(Check(name, category, [profile.python, "-m", name, *args],
                                interpret or generic(name, category)))
        else:
            tool_check(name, category, name, args, applies, interpret)

    python_check("mypy", "lint", ["."], python and "mypy" in profile.linters)
    python_check("pytest", "test", ["-q", "-rfE", "--tb=short", "-p", "no:cacheprovider"],
                 python or "pytest" in profile.ci_tools, interpret_pytest)

    for project in profile.node_projects:
        pm = project.package_manager
        missing_modules = bool(project.deps) and not (Path(profile.root) / project.dir / "node_modules").is_dir()
        for script, category in (("lint", "lint"), ("test", "test")):
            body = project.scripts.get(script)
            if body is None or NPM_PLACEHOLDER_TEST in body:
                continue
            name = project.label(f"{pm} {script}")
            if missing_modules:
                checks.append(Check(name, category, None,
                                    skip_reason=f"node_modules missing; run `{pm} install` once while online"))
            elif not profile.has_tool(pm):
                checks.append(Check(name, category, None, skip_reason=f"`{pm}` is not installed"))
            else:
                checks.append(Check(name, category, [profile.tool_path(pm), "run", "--silent", script],
                                    interpret_js_test(name) if category == "test" else generic(name, category),
                                    cwd=project.cwd))
        for tool, package, args, interpret, applies in (
            ("eslint", "eslint", ["-f", "json", "."], interpret_eslint, "lint" not in project.scripts),
            ("prettier", "prettier", ["--check", "."], interpret_prettier, True),
            ("tsc", "typescript", ["--noEmit", "--pretty", "false"], interpret_tsc, True),
        ):
            if tool not in project.linters or not applies:
                continue
            name = project.label(tool)
            path = profile.project_tool(project, tool, package)
            if path:
                checks.append(Check(name, "lint", [path, *args], _renamed(interpret, name), cwd=project.cwd))
            else:
                reason = (f"`{package}` is pinned in package.json but not installed; run `{pm} install` once while "
                          "online") if package in project.deps else f"`{tool}` is not installed"
                checks.append(Check(name, "lint", None, skip_reason=reason))

    tool_check("gofmt", "lint", "gofmt", ["-l", "."], bool(profile.go_modules) or "Go" in profile.languages,
               interpret_gofmt)
    for module in profile.go_modules:
        cwd = None if module == "." else module
        vet, lint, test = (name if cwd is None else f"{module}: {name}"
                           for name in ("go vet", "golangci-lint", "go test"))
        tool_check(vet, "lint", "go", ["vet", "./..."], True, interpret_go(vet, "lint"), cwd)
        has_config = any(f"{module}/{name}".removeprefix("./") in profile.files
                         for name in (".golangci.yml", ".golangci.yaml", ".golangci.toml"))
        tool_check(lint, "lint", "golangci-lint", ["run"], has_config, None, cwd)
        tool_check(test, "test", "go", ["test", "./..."], True, interpret_go(test, "test"), cwd)

    for crate in profile.cargo_roots:
        cwd = None if crate == "." else crate
        for name, helper, args, category in (
            ("cargo fmt", "rustfmt", ["fmt", "--check", "--", "-l"], "lint"),
            ("cargo clippy", "cargo-clippy", ["clippy", "--offline", "--quiet"], "lint"),
            ("cargo test", None, ["test", "--offline"], "test"),
        ):
            name = name if crate == "." else f"{crate}: {name}"
            if helper and not profile.has_tool(helper):
                checks.append(Check(name, category, None, skip_reason=f"`{helper}` is not installed"))
            else:
                tool_check(name, category, "cargo", args, True, interpret_cargo(name, category), cwd)
    if "test" in profile.scripts.get("Makefile", []) and not any(c.category == "test" for c in checks):
        tool_check("make test", "test", "make", ["test"], True)
    if registry is not None:
        checks.extend(registry.checks(profile))
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
            result = check.interpret(executor.run(check.command, cwd=check.cwd))
            prefix = f"{profile.root}/"
            result.details = [line.replace(prefix, "") for line in result.details]
            add(result)
        except CommandBlocked as exc:
            add(CheckResult(check.name, check.category, "error", str(exc)))
    return verification


def categories_for(intents: set[str]) -> set[str] | None:
    wanted: set[str] = set()
    for intent in intents:
        wanted |= INTENT_CATEGORIES.get(intent, set())
    return wanted or None
