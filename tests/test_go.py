import shutil

import pytest

from cloudy.models import CommandResult
from cloudy.orchestrator import Orchestrator
from cloudy.rules import go
from cloudy.verifier import interpret_go, interpret_gofmt

needs_go = pytest.mark.skipif(not (shutil.which("go") and shutil.which("gofmt")), reason="needs go and gofmt")

GO_REPO = {
    "go.mod": "module example.com/calc\n\ngo 1.22\n",
    "calc.go": "package calc\n\nfunc Add(a,b int) int {\nreturn a+b}\n\nfunc Sub(a, b int) int { return a + b }\n",
    "calc_test.go": 'package calc\n\nimport "testing"\n\nfunc TestSub(t *testing.T) {\n'
                    '\tif got := Sub(5, 3); got != 2 {\n\t\tt.Errorf("Sub(5, 3) = %d, want 2", got)\n\t}\n}\n',
    "vendor/x/x.go": "package x\nfunc  X() {}\n",
}


@needs_go
def test_detection(make_repo, context):
    profile = context(make_repo(GO_REPO, git=False)).profile
    assert "Go" in profile.languages and "gomod" in profile.ecosystems
    assert profile.tools["go"].version.startswith("go version") and profile.tools["gofmt"].available
    assert "go test" in profile.test_runners and "calc_test.go" in profile.test_files


@needs_go
def test_gofmt_fix_via_stdin_skips_vendor(make_repo, context):
    ctx = context(make_repo(GO_REPO, git=False))
    assert [f.path for f in go.Gofmt().check(ctx)] == ["calc.go"]
    assert go.Gofmt().fix(ctx) == {"calc.go": "package calc\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n\n"
                                              "func Sub(a, b int) int { return a + b }\n"}


@needs_go
def test_syntax_error_is_reported_not_fixed(make_repo, context):
    ctx = context(make_repo({"go.mod": "module m\n", "a.go": "package a\nfunc (\n"}, git=False))
    [finding] = go.Gofmt().check(ctx)
    assert "could not parse" in finding.message and not finding.fixable and go.Gofmt().fix(ctx) == {}


def test_go_files_without_module_are_reported(make_repo, context):
    ctx = context(make_repo({"main.go": "package main\n"}, git=False))
    [finding] = go.GoModule().check(ctx)
    assert "no go.mod" in finding.message and not finding.fixable


def test_interpreters_parse_real_output():
    test = CommandResult("go test ./...", 1, "--- FAIL: TestSub (0.00s)\n    calc_test.go:7: Sub(5, 3) = 8, want 2\n"
                                             "FAIL\nFAIL\texample.com/calc\t0.002s\nFAIL\n")
    assert interpret_go("go test", "test")(test).details == [
        "--- FAIL: TestSub", "calc_test.go:7: Sub(5, 3) = 8, want 2", "FAIL\texample.com/calc"]
    vet = CommandResult("go vet ./...", 1, "", "# example.com/calc\n./calc.go:5:2: unreachable code\n")
    assert interpret_go("go vet", "lint")(vet).details == ["calc.go:5:2: unreachable code"]
    fmt = interpret_gofmt(CommandResult("gofmt -l .", 0, "calc.go\nvendor/x/x.go\n"))
    assert fmt.status == "fail" and fmt.details == ["calc.go"]


@needs_go
def test_apply_formats_but_reports_logic_bug(make_repo, tmp_path):
    root = make_repo(GO_REPO)
    task = "fix formatting and the failing tests"
    data = Orchestrator(root, apply=True, state_dir=tmp_path / "s").run(task).to_dict()
    assert [e["path"] for e in data["edits"]] == ["calc.go"]
    assert "return a + b }" in (root / "calc.go").read_text()  # Sub's logic bug is untouched
    assert (root / "vendor/x/x.go").read_text() == GO_REPO["vendor/x/x.go"]
    go_test = next(c for c in data["verifications"][-1]["checks"] if c["name"] == "go test")
    assert "calc_test.go:7: Sub(5, 3) = 8, want 2" in go_test["details"]
    assert data["status"] == "needs_human"
    again = Orchestrator(root, apply=True, state_dir=tmp_path / "s").run(task).to_dict()
    assert again["edits"] == [] and again["status"] == "needs_human"
