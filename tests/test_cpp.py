import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloudy.models import CommandResult
from cloudy.orchestrator import Orchestrator
from cloudy.rules import cpp

needs_clang_format = pytest.mark.skipif(not shutil.which("clang-format"), reason="needs clang-format")
needs_cmake = pytest.mark.skipif(not all(shutil.which(t) for t in ("clang-format", "cmake", "ctest", "cc")),
                                 reason="needs clang-format, cmake, ctest and a C compiler")

C_REPO = {
    ".clang-format": "BasedOnStyle: LLVM\n",
    "CMakeLists.txt": "cmake_minimum_required(VERSION 3.10)\nproject(calc C)\nenable_testing()\n"
                      "add_executable(calc_test tests/calc_test.c src/calc.c)\n"
                      "add_test(NAME calc_test COMMAND calc_test)\n",
    "src/calc.h": "int add(int a, int b);\nint sub(int a, int b);\n",
    "src/calc.c": '#include "calc.h"\n\nint add(int a,int b){return a+b;}\nint sub(int a, int b) { return a + b; }\n',
    "tests/calc_test.c": '#include "../src/calc.h"\n#include <assert.h>\n\nint main(void) {\n'
                         "  assert(add(2, 2) == 4);\n  assert(sub(5, 3) == 2);\n  return 0;\n}\n",
}


def test_detection_and_opt_in(make_repo, context):
    profile = context(make_repo(C_REPO, git=False)).profile
    assert profile.languages["C"] == 3 and "cmake" in profile.ecosystems
    assert "tests/calc_test.c" in profile.test_files
    assert sorted(cpp.formattable(profile)) == ["src/calc.c", "src/calc.h", "tests/calc_test.c"]


def test_no_clang_format_config_means_no_formatting(make_repo, context):
    files = {k: v for k, v in C_REPO.items() if k != ".clang-format"}
    ctx = context(make_repo(files, git=False))
    assert cpp.formattable(ctx.profile) == [] and cpp.ClangFormat().check(ctx) == []


def test_nested_config_only_covers_its_subtree(make_repo, context):
    ctx = context(make_repo({"lib/.clang-format": "BasedOnStyle: LLVM\n", "lib/a.c": "", "app/b.c": ""}, git=False))
    assert cpp.formattable(ctx.profile) == ["lib/a.c"]


@needs_clang_format
def test_clang_format_fix_via_stdin(make_repo, context):
    ctx = context(make_repo(C_REPO, git=False))
    assert [f.path for f in cpp.ClangFormat().check(ctx)] == ["src/calc.c"]
    assert cpp.ClangFormat().fix(ctx) == {
        "src/calc.c": '#include "calc.h"\n\nint add(int a, int b) { return a + b; }\nint sub(int a, int b) '
                      "{ return a + b; }\n"}


def test_committed_build_output_is_reported(make_repo, context):
    ctx = context(make_repo(C_REPO | {"CMakeCache.txt": "x\n", "src/calc.o": "x"}))
    assert sorted(f.path for f in cpp.CommittedBuildOutput().check(ctx)) == ["CMakeCache.txt", "src/calc.o"]


def test_interpreters_parse_real_output():
    ctest = ("calc_test: /r/tests/calc_test.c:6: main: Assertion `sub(5, 3) == 2' failed.\n"
             "The following tests FAILED:\n\t  1 - calc_test (Subprocess aborted)\n")
    assert cpp.interpret_ctest(CommandResult("ctest", 8, ctest)).details == [
        "/r/tests/calc_test.c:6: main: Assertion `sub(5, 3) == 2' failed.", "calc_test (Subprocess aborted)"]
    none = cpp.interpret_ctest(CommandResult("ctest", 0, "No tests were found!!!\n"))
    assert none.status == "skip"


@needs_cmake
def test_apply_formats_but_reports_logic_bug_and_keeps_tree_clean(make_repo, tmp_path):
    root = make_repo(C_REPO)
    task = "fix formatting and the failing tests"
    data = Orchestrator(root, apply=True, state_dir=tmp_path / "s").run(task).to_dict()
    assert [(e["path"], e["rule"]) for e in data["edits"]] == [("src/calc.c", "cpp.clang-format")]
    assert "int sub(int a, int b) { return a + b; }" in (root / "src/calc.c").read_text()  # the bug stays
    ctest = next(c for c in data["verifications"][-1]["checks"] if c["name"] == "ctest")
    assert ctest["status"] == "fail"
    assert "tests/calc_test.c:6: main: Assertion `sub(5, 3) == 2' failed." in ctest["details"]
    assert data["status"] == "needs_human" and data["tool_side_effects"] == []
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout
    assert status.strip() == "M src/calc.c"  # the CMake build went outside the repository
    shutil.rmtree(cpp.build_dir(SimpleNamespace(root=str(Path(root).resolve()))), ignore_errors=True)
