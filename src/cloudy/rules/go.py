"""Go rules. gofmt has no configuration, so its output is always the unambiguous formatting."""

from __future__ import annotations

from pathlib import PurePosixPath

from ..explorer import SKIP_DIRS
from . import RepoContext, Rule, pipe_through


def _go_files(ctx: RepoContext) -> list[str]:
    return [f for f in ctx.profile.files if f.endswith(".go") and "testdata" not in PurePosixPath(f).parts]


class GoModule(Rule):
    id = "go.module"
    intents = frozenset({"fix_ci", "fix_tests", "fix_lint"})
    summary = "Go code needs a go.mod for `go build`, `go vet` and `go test` to run"
    hint = "Run `go mod init <module path>` with the module's real import path."

    def check(self, ctx: RepoContext):
        if _go_files(ctx) and not ctx.match("go.mod", "*/go.mod"):
            return [self.finding(".", "Go files but no go.mod; module-mode commands cannot run "
                                      "(create it with `go mod init <module path>`)")]
        return []


class Gofmt(Rule):
    id = "go.gofmt"
    intents = frozenset({"fix_lint"})
    summary = "Format Go files with gofmt"

    def _unformatted(self, ctx: RepoContext) -> tuple[list[str], str | None]:
        if not _go_files(ctx) or not ctx.profile.has_tool("gofmt"):
            return [], None
        result = ctx.executor.run([ctx.profile.tool_path("gofmt"), "-l", "."])
        if not result.ok:
            first = next((line for line in result.stderr.splitlines() if line.strip()), f"exit {result.exit_code}")
            return [], f"gofmt could not parse every file: {first}"
        return [f for f in result.stdout.splitlines()
                if f and not set(PurePosixPath(f).parts) & (SKIP_DIRS | {"testdata"})], None

    def check(self, ctx: RepoContext):
        files, problem = self._unformatted(ctx)
        findings = [self.finding(".", problem)] if problem else []
        return findings + [self.finding(f, "not gofmt-formatted", fixable=True) for f in files]

    def fix(self, ctx: RepoContext):
        files, _ = self._unformatted(ctx)
        changes = {}
        for path in files:
            if formatted := pipe_through(ctx, [ctx.profile.tool_path("gofmt")], path):
                changes[path] = formatted
        return changes


RULES = [GoModule(), Gofmt()]
