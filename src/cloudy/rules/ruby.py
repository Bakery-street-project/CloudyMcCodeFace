"""Ruby, written against the plugin interface (see cloudy.plugins). Fixes come only from rubocop's safe autocorrect."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..models import CheckResult, CommandResult, sha256
from ..verifier import Check, command_check, match_output
from . import RepoContext, Rule

LANGUAGES = {".rb": "Ruby", ".rake": "Ruby", ".gemspec": "Ruby"}
LANG_TOOLS = {"Ruby": ("ruby", "bundle", "rake", "rubocop", "rspec")}
MANIFESTS = {"Gemfile": "bundler"}
TEST_PATTERNS = ("*_test.rb", "*_spec.rb", "test_*.rb")
SEPARATOR = "====================\n"  # rubocop -a --stdin prints its report, this line, then the corrected source
CORRECTED = re.compile(r":(\d+):\d+: \w: \[Corrected\] ([\w/]+):")  # emacs format: path:line:col: C: [Corrected] Cop


def _configured(profile) -> bool:
    """The project opted into rubocop: a .rubocop.yml, or rubocop in the Gemfile."""
    return ".rubocop.yml" in profile.files or "rubocop" in _read(profile, "Gemfile")


def _read(profile, rel: str) -> str:
    try:
        return (Path(profile.root) / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _runner(profile, tool: str) -> list[str] | None:
    """`bundle exec <tool>` when the Gemfile pins it (so its version is used), else the tool from PATH."""
    if tool in _read(profile, "Gemfile") and profile.has_tool("bundle"):
        return [profile.tool_path("bundle"), "exec", tool]
    return [profile.tool_path(tool)] if profile.has_tool(tool) else None


class Rubocop(Rule):
    id = "ruby.rubocop"
    intents = frozenset({"fix_lint"})
    summary = "Apply rubocop's safe autocorrections, using the project's .rubocop.yml"
    hint = "No safe autocorrection exists for this offense; fix it by hand or adjust .rubocop.yml deliberately."

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], tuple[str | None, set[tuple[int, str]]]] = {}

    def _offenses(self, ctx: RepoContext) -> tuple[list[dict], str | None]:
        runner = _runner(ctx.profile, "rubocop") if _configured(ctx.profile) else None
        if runner is None:
            return [], None
        result = ctx.executor.run([*runner, "--format", "json"])
        try:
            files = json.loads(result.stdout)["files"]
        except (json.JSONDecodeError, KeyError, TypeError):
            reason = next((line for line in result.output.splitlines() if line.strip()), f"exit {result.exit_code}")
            return [], f"rubocop could not run: {reason}"
        return [dict(o, path=f["path"]) for f in files for o in f.get("offenses", [])], None

    def _autocorrect(self, ctx: RepoContext, path: str) -> tuple[str | None, set[tuple[int, str]]]:
        """(corrected source or None, {(line, cop)} that -a corrects) — cached per file content."""
        source = ctx.read(path) or ""
        key = (path, sha256(source + (ctx.read(".rubocop.yml") or "")))
        if key not in self._cache:
            runner = _runner(ctx.profile, "rubocop")
            result = ctx.executor.run([*runner, "-a", "--format", "emacs", "--stdin", path], stdin=source)
            report, separator, corrected = result.stdout.rpartition(SEPARATOR)
            fixed = {(int(m.group(1)), m.group(2)) for m in CORRECTED.finditer(report)}
            self._cache[key] = (corrected if separator and fixed and corrected != source else None, fixed)
        return self._cache[key]

    def check(self, ctx: RepoContext):
        offenses, problem = self._offenses(ctx)
        findings = [self.finding("Gemfile" if ctx.exists("Gemfile") else ".rubocop.yml", problem)] if problem else []
        for path in sorted({o["path"] for o in offenses}):
            if ctx.read(path) != _read(ctx.profile, path):
                continue  # a pending (dry-run) edit already covers this file; rubocop only saw the disk version
            corrected = self._autocorrect(ctx, path)[1] if any(o["correctable"] for o in offenses
                                                               if o["path"] == path) else set()
            for o in (o for o in offenses if o["path"] == path):
                line = o["location"]["line"]
                findings.append(self.finding(path, f"{o['cop_name']}: {o['message']}", line,
                                             (line, o["cop_name"]) in corrected))
        return findings

    def fix(self, ctx: RepoContext):
        offenses, _ = self._offenses(ctx)
        changes = {}
        for path in sorted({o["path"] for o in offenses if o["correctable"]}):
            corrected, _ = self._autocorrect(ctx, path)
            if corrected:
                changes[path] = corrected
        return changes


class RubyVersion(Rule):
    id = "ruby.version"
    intents = frozenset({"fix_tests", "fix_ci"})
    summary = ".ruby-version should match the Ruby that runs the checks"
    hint = "Install the pinned Ruby (rbenv/asdf) or update .ruby-version deliberately; results may differ otherwise."

    def check(self, ctx: RepoContext):
        pinned = (ctx.read(".ruby-version") or "").strip().removeprefix("ruby-")
        info = ctx.profile.tools.get("ruby")
        match = re.search(r"ruby (\d+\.\d+)", info.version or "") if info and info.version else None
        if pinned and match and not pinned.startswith(match.group(1)):
            return [self.finding(".ruby-version", f"pins Ruby {pinned} but checks run on Ruby {match.group(1)}")]
        return []


RULES = [RubyVersion(), Rubocop()]


RUBY_TEST_PATTERNS = [re.compile(p) for p in (
    r"^\s*\d+\) (?:Failure|Error):$",
    r"^(\S+#\S+) \[(\S+:\d+)\]:?$",  # minitest: CalcTest#test_sub [/abs/test/calc_test.rb:6]:
    r"^\s*(Expected: .*)$",
    r"^\s*(Actual: .*)$",
    r"^(rspec \./\S+:\d+.*)$",  # rspec re-run lines
    r"^\s*(expected: .*)$",
    r"^\s*(got: .*)$",
    r"^\s*((?:\w+::)*\w+Error: .*)$",
)]


def interpret_ruby_tests(name: str):
    def interpret(result: CommandResult) -> CheckResult:
        checked = match_output(result, name, "test", RUBY_TEST_PATTERNS)
        checked.details = [d for d in checked.details if not re.match(r"^\s*\d+\) ", d)]
        return checked
    return interpret


def interpret_rubocop(result: CommandResult) -> CheckResult:
    base = match_output(result, "rubocop", "lint", [])
    try:
        files = json.loads(result.stdout)["files"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return base
    offenses = [f"{f['path']}:{o['location']['line']} {o['cop_name']}: {o['message']}"
                for f in files for o in f.get("offenses", [])]
    if offenses:
        base.status, base.summary, base.details = "fail", f"{len(offenses)} offense(s)", offenses[:30]
    return base


def checks(profile) -> list[Check]:
    if "Ruby" not in profile.languages:
        return []
    found: list[Check] = []
    if _configured(profile):
        runner = _runner(profile, "rubocop")
        found.append(Check("rubocop", "lint", [*runner, "--format", "json"], interpret_rubocop) if runner
                     else Check("rubocop", "lint", None, skip_reason="`rubocop` is not installed"))
    has_rakefile = "Rakefile" in profile.files and re.search(r"\b(TestTask|task\s+:?test)\b",
                                                             _read(profile, "Rakefile"))
    if any(f.startswith("spec/") and f.endswith("_spec.rb") for f in profile.files):
        runner = _runner(profile, "rspec")
        found.append(Check("rspec", "test", runner, interpret_ruby_tests("rspec")) if runner
                     else Check("rspec", "test", None, skip_reason="`rspec` is not installed"))
    elif has_rakefile:
        runner = _runner(profile, "rake")
        found.append(Check("rake test", "test", [*runner, "test"], interpret_ruby_tests("rake test")) if runner
                     else Check("rake test", "test", None, skip_reason="`rake` is not installed"))
    elif any(f.startswith("test/") and f.endswith("_test.rb") for f in profile.files):
        # plain minitest without a Rakefile: load every test file, as `rake test` would
        script = 'Dir.glob("test/**/*_test.rb").sort.each { |f| require File.expand_path(f) }'
        found.append(command_check(profile, "minitest", "test", "ruby", ["-Ilib", "-Itest", "-e", script],
                                   interpret_ruby_tests("minitest")))
    return found
