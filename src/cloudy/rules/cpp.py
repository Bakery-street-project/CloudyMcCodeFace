"""C and C++, written against the plugin interface (see cloudy.plugins).

Formatting happens only when the project has a .clang-format (otherwise any style would be a guess). CMake builds go
to a directory outside the repository, so checks never leave build output in the working tree.
"""

from __future__ import annotations

import re
import shlex
import tempfile
from pathlib import Path, PurePosixPath

from ..models import CheckResult, CommandResult, sha256
from ..verifier import Check, command_check, match_output
from . import RepoContext, Rule, pipe_through

LANGUAGES = {".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".hpp": "C++", ".hh": "C++"}
LANG_TOOLS = {"C": ("clang-format", "cmake", "ctest", "make"), "C++": ("clang-format", "cmake", "ctest", "make")}
MANIFESTS = {"CMakeLists.txt": "cmake"}
TEST_PATTERNS = ("test_*.c", "*_test.c", "test_*.cpp", "*_test.cpp", "*_test.cc")
CONFIG_NAMES = (".clang-format", "_clang-format")
VIOLATION = re.compile(r"^(.+?):\d+:\d+: (?:error|warning): code should be clang-formatted")


def formattable(profile) -> list[str]:
    """C/C++ files covered by a .clang-format in their directory or an ancestor."""
    config_dirs = {PurePosixPath(f).parent for f in profile.files if PurePosixPath(f).name in CONFIG_NAMES}
    return [f for f in profile.files if PurePosixPath(f).suffix.lower() in LANGUAGES
            and config_dirs & {PurePosixPath(f).parent, *PurePosixPath(f).parents}]


class ClangFormat(Rule):
    id = "cpp.clang-format"
    intents = frozenset({"fix_lint"})
    summary = "Format C/C++ files with clang-format, using the project's .clang-format"

    def _unformatted(self, ctx: RepoContext) -> list[str]:
        files = formattable(ctx.profile)
        if not files or not ctx.profile.has_tool("clang-format"):
            return []
        found: set[str] = set()
        for start in range(0, len(files), 200):  # keep argv small on huge trees
            result = ctx.executor.run([ctx.profile.tool_path("clang-format"), "--dry-run", "-Werror",
                                       *files[start:start + 200]])
            found |= {m.group(1) for line in result.stderr.splitlines() if (m := VIOLATION.match(line))}
        return sorted(found)

    def check(self, ctx: RepoContext):
        return [self.finding(path, "not formatted with the project's .clang-format", fixable=True)
                for path in self._unformatted(ctx)]

    def fix(self, ctx: RepoContext):
        changes = {}
        for path in self._unformatted(ctx):
            argv = [ctx.profile.tool_path("clang-format"), f"--assume-filename={path}"]
            if formatted := pipe_through(ctx, argv, path):
                changes[path] = formatted
        return changes


class CommittedBuildOutput(Rule):
    id = "cpp.build-output"
    intents = frozenset({"fix_ci"})
    summary = "Build output (CMakeCache.txt, CMakeFiles/, object files) must not be committed"
    hint = "`git rm --cached` these files yourself, then add the patterns to .gitignore."

    def check(self, ctx: RepoContext):
        if not ctx.profile.git or not (set(ctx.profile.code_languages) & {"C", "C++"}):
            return []
        result = ctx.executor.run(["git", "ls-files", "--", "CMakeCache.txt", "*/CMakeCache.txt", "*CMakeFiles/*",
                                   "*.o", "*.obj", "*.a"])
        tracked = result.stdout.split() if result.ok else []
        return [self.finding(path, "build output is tracked by git") for path in tracked[:20]]


RULES = [CommittedBuildOutput(), ClangFormat()]


CTEST_PATTERNS = [re.compile(p) for p in (
    r"^\s*\d+ - (\S+ \((?:Failed|SEGFAULT|Subprocess aborted|Timeout|Not Run|Exception)\))",
    r"((?:/|\w)[\w./-]*\.(?:c|cc|cpp|cxx|h|hpp):\d+:\d+: error: .*)$",  # compile errors
    r"((?:/|\w)[\w./-]*\.(?:c|cc|cpp|cxx|h|hpp):\d+: .*)$",  # assert.h: file:line: func: Assertion `x' failed.
)]


def interpret_ctest(result: CommandResult) -> CheckResult:
    checked = match_output(result, "ctest", "test", CTEST_PATTERNS)
    if result.ok and "No tests were found" in result.output:
        checked.status, checked.summary = "skip", "CMake project defines no tests"
    return checked


def interpret_clang_format(result: CommandResult) -> CheckResult:
    files = sorted({m.group(1) for line in result.stderr.splitlines() if (m := VIOLATION.match(line))})
    checked = match_output(result, "clang-format", "lint", [])
    if files:
        checked.status, checked.summary, checked.details = "fail", f"{len(files)} file(s) not formatted", files
    return checked


def build_dir(profile) -> str:
    """Per-repository CMake build directory in the system temp dir: never inside the working tree."""
    return str(Path(tempfile.gettempdir()) / f"cloudy-cmake-{sha256(profile.root)[:12]}")


def checks(profile) -> list[Check]:
    found: list[Check] = []
    files = formattable(profile)
    if files:
        found.append(command_check(profile, "clang-format", "lint", "clang-format", ["--dry-run", "-Werror", *files],
                                   interpret_clang_format))
    if "CMakeLists.txt" in profile.files:
        missing = [tool for tool in ("cmake", "ctest") if not profile.has_tool(tool)]
        if missing:
            return found + [Check("ctest", "test", None, skip_reason=f"`{missing[0]}` is not installed")]
        out = shlex.quote(build_dir(profile))
        cmake, ctest = shlex.quote(profile.tool_path("cmake")), shlex.quote(profile.tool_path("ctest"))
        command = (f"{cmake} -S . -B {out} -DFETCHCONTENT_FULLY_DISCONNECTED=ON >/dev/null && "
                   f"{cmake} --build {out} && {ctest} --test-dir {out} --output-on-failure")
        found.append(Check("ctest", "test", command, interpret_ctest))
    return found
