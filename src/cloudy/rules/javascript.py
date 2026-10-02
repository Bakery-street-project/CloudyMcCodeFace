"""JavaScript / TypeScript rules. Fixes come only from the project's own eslint and prettier setup."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path, PurePosixPath

from ..explorer import LOCKFILES, NodeProject, read_package
from . import RepoContext, Rule, pipe_through

JS_SOURCES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")
FROZEN_INSTALL = {
    "npm ci": "package-lock.json",
    "yarn install --frozen-lockfile": "yarn.lock",
    "yarn install --immutable": "yarn.lock",
    "pnpm install --frozen-lockfile": "pnpm-lock.yaml",
}
# CLIs commonly used in scripts, mapped to the package that provides them.
SCRIPT_CLIS = {
    "eslint": "eslint", "prettier": "prettier", "tsc": "typescript", "jest": "jest", "vitest": "vitest",
    "mocha": "mocha", "ava": "ava", "ts-node": "ts-node", "tsx": "tsx", "vite": "vite", "webpack": "webpack",
}
PLACEHOLDER_TEST = "no test specified"


def _rel(ctx: RepoContext, path: str) -> str:
    candidate = Path(path)
    return candidate.relative_to(ctx.root).as_posix() if candidate.is_absolute() and candidate.is_relative_to(
        ctx.root) else path


def _tool_for(ctx: RepoContext, project: NodeProject, tool: str) -> tuple[str | None, str | None]:
    """(path, problem). Only runs a tool the project configured; a pinned tool must be installed locally."""
    if tool not in project.linters:
        return None, None
    path = ctx.profile.project_tool(project, tool)
    if path:
        return path, None
    if tool in project.deps:
        return None, (f"{tool} is pinned in package.json but not installed; run `{project.package_manager} "
                      "install` once while online")
    return None, None


def _in_project(project: NodeProject, path: str) -> str:
    """Repo-relative path -> path relative to the project directory (what the tools expect on stdin)."""
    return path if project.dir == "." else str(PurePosixPath(path).relative_to(project.dir))


def _manifest(project: NodeProject) -> str:
    return str(PurePosixPath(project.dir) / "package.json")


class Lockfiles(Rule):
    id = "js.lockfiles"
    intents = frozenset({"fix_ci", "fix_tests"})
    summary = "One package manager per project, and CI installs must match its lockfile"
    hint = "Pick one package manager, delete the other lockfiles, and commit the remaining one."

    def check(self, ctx: RepoContext):
        findings = []
        for manifest in ctx.match("package.json", "*/package.json"):
            base = PurePosixPath(manifest).parent
            locks = [lock for lock in LOCKFILES if ctx.exists(str(base / lock))]
            if len(locks) > 1:
                findings.append(self.finding(manifest, f"several lockfiles ({', '.join(locks)}); installs will "
                                                       "differ by package manager — keep one"))
            declared = str(read_package(ctx.root, manifest).get("packageManager", "")).split("@", 1)[0]
            if declared and locks and declared not in {LOCKFILES[lock] for lock in locks}:
                findings.append(self.finding(manifest, f"packageManager is {declared} but the lockfile is "
                                                       f"{', '.join(locks)}"))
        project_dirs = [p.dir for p in ctx.profile.node_projects] or ["."]
        for path in ctx.profile.ci_files:
            for number, line in enumerate((ctx.read(path) or "").splitlines(), 1):
                for command, lock in FROZEN_INSTALL.items():
                    if re.search(rf"(?<![\w-]){re.escape(command)}(?![\w-])", line) and not any(
                            ctx.exists(str(PurePosixPath(d) / lock)) for d in project_dirs):
                        findings.append(self.finding(path, f"`{command}` fails without {lock}", number))
        return findings


def _script_commands(body: str) -> list[str]:
    """First program of each command in a package.json script, skipping `VAR=value` prefixes."""
    programs = []
    for segment in re.split(r"&&|\|\||[;|]", body):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        tokens = [t for t in tokens if not re.match(r"^\w+=", t)]
        if tokens and tokens[0] in ("npx", "pnpm", "yarn") and len(tokens) > 1 and tokens[1] in ("exec", "dlx"):
            tokens = tokens[2:]
        elif tokens and tokens[0] == "npx":
            tokens = tokens[1:]
        if tokens:
            programs.append(tokens[0])
    return programs


def _owner(path: str, project: NodeProject, nested: list[str]) -> bool:
    """True when `path` belongs to `project` and not to a package nested inside it."""
    parents = {str(p) for p in PurePosixPath(path).parents}
    if project.dir != "." and project.dir not in parents:
        return False
    return not any(d in parents and d != project.dir and (project.dir == "." or d.startswith(f"{project.dir}/"))
                   for d in nested)


class ScriptTools(Rule):
    id = "js.script-tools"
    intents = frozenset({"fix_tests", "fix_lint"})
    summary = "package.json scripts must use tools the project declares, and a real test script when tests exist"
    hint = "Add the tool to devDependencies (so CI installs it) or change the script."

    def check(self, ctx: RepoContext):
        findings = []
        nested = [p.dir for p in ctx.profile.node_projects if p.dir != "."]
        for project in ctx.profile.node_projects:
            manifest = _manifest(project)
            js_tests = [f for f in ctx.profile.test_files if f.endswith(JS_SOURCES) and _owner(f, project, nested)]
            if js_tests and PLACEHOLDER_TEST in project.scripts.get("test", PLACEHOLDER_TEST):
                findings.append(self.finding(manifest, f"{len(js_tests)} test file(s) exist but the `test` "
                                                       "script is missing or the npm placeholder"))
            for name, body in sorted(project.scripts.items()):
                for program in _script_commands(body):
                    package = SCRIPT_CLIS.get(program)
                    if package and package not in project.deps:
                        findings.append(self.finding(manifest, f"script `{name}` runs `{program}` but `{package}` "
                                                               "is not in dependencies/devDependencies"))
        return findings


class EslintAutofix(Rule):
    id = "js.eslint-autofix"
    intents = frozenset({"fix_lint"})
    summary = "Apply the project's own eslint autofixes"

    def _report(self, ctx: RepoContext, project: NodeProject) -> tuple[str | None, list[dict], str | None]:
        tool, problem = _tool_for(ctx, project, "eslint")
        if not tool:
            return None, [], problem
        result = ctx.executor.run([tool, "-f", "json", "."], cwd=project.cwd)
        try:
            files = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            files = None
        if result.exit_code not in (0, 1) or files is None:
            lines = result.stderr.strip().splitlines()
            reason = next((line for line in lines if line.strip()), f"exit {result.exit_code}")
            return None, [], f"eslint could not run: {reason}"
        return tool, files, None

    def check(self, ctx: RepoContext):
        findings = []
        for project in ctx.profile.node_projects:
            _, files, problem = self._report(ctx, project)
            if problem:
                findings.append(self.finding(_manifest(project), problem))
            for item in files:
                for message in item.get("messages", []):
                    findings.append(self.finding(_rel(ctx, item.get("filePath", "")),
                                                 f"{message.get('ruleId') or 'error'} {message.get('message', '')}",
                                                 message.get("line"), "fix" in message))
        return findings

    def fix(self, ctx: RepoContext):
        changes = {}
        for project in ctx.profile.node_projects:
            changes.update(self._fix_project(ctx, project))
        return changes

    def _fix_project(self, ctx: RepoContext, project: NodeProject) -> dict[str, str]:
        tool, files, _ = self._report(ctx, project)
        changes = {}
        for item in files:
            if not any("fix" in m for m in item.get("messages", [])):
                continue
            path = _rel(ctx, item["filePath"])
            argv = [tool, "--fix-dry-run", "-f", "json", "--stdin", "--stdin-filename", _in_project(project, path)]
            output = pipe_through(ctx, argv, path, cwd=project.cwd, ok_codes=(0, 1))
            try:
                fixed = json.loads(output)[0].get("output") if output else None
            except (json.JSONDecodeError, IndexError, AttributeError):
                fixed = None
            if fixed and fixed != ctx.read(path):
                changes[path] = fixed
        return changes


class PrettierFormat(Rule):
    id = "js.prettier"
    intents = frozenset({"fix_lint"})
    summary = "Format files with the project's own Prettier configuration"

    def _unformatted(self, ctx: RepoContext, project: NodeProject) -> tuple[str | None, list[str], str | None]:
        tool, problem = _tool_for(ctx, project, "prettier")
        if not tool:
            return None, [], problem
        result = ctx.executor.run([tool, "--check", "."], cwd=project.cwd)
        files = [str(PurePosixPath(project.dir) / line[7:].strip()) for line in result.output.splitlines()
                 if line.startswith("[warn] ") and "Code style issues" not in line]
        if result.exit_code not in (0, 1):
            error = next((line for line in result.stderr.splitlines() if "[error]" in line), f"exit {result.exit_code}")
            return None, [], f"prettier could not check all files: {error}"
        return tool, files, None

    def check(self, ctx: RepoContext):
        findings = []
        for project in ctx.profile.node_projects:
            _, files, problem = self._unformatted(ctx, project)
            if problem:
                findings.append(self.finding(_manifest(project), problem))
            findings += [self.finding(path, "not formatted with the project's Prettier config", fixable=True)
                         for path in files]
        return findings

    def fix(self, ctx: RepoContext):
        changes = {}
        for project in ctx.profile.node_projects:
            tool, files, _ = self._unformatted(ctx, project)
            for path in files:
                argv = [tool, "--stdin-filepath", _in_project(project, path)]
                if formatted := pipe_through(ctx, argv, path, cwd=project.cwd):
                    changes[path] = formatted
        return changes


RULES = [Lockfiles(), ScriptTools(), EslintAutofix(), PrettierFormat()]
