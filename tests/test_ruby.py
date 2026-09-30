import shutil
import subprocess

import pytest

from cloudy.models import CommandResult
from cloudy.orchestrator import Orchestrator
from cloudy.rules import ruby

needs_ruby = pytest.mark.skipif(not (shutil.which("ruby") and shutil.which("rubocop")), reason="needs ruby, rubocop")

RUBOCOP_YML = ("AllCops:\n  NewCops: enable\n  SuggestExtensions: false\nStyle/FrozenStringLiteralComment:\n"
               "  Enabled: false\nNaming/MethodParameterName:\n  Enabled: false\nStyle/Documentation:\n"
               "  Enabled: false\n")
RUBY_REPO = {
    ".rubocop.yml": RUBOCOP_YML,
    "lib/calc.rb": "def add(a,b)\n  a+b\nend\n\ndef sub(a, b)\n  a + b\nend\n",
    "test/calc_test.rb": "require 'minitest/autorun'\nrequire_relative '../lib/calc'\n\n"
                         "class CalcTest < Minitest::Test\n  def test_sub\n    assert_equal 2, sub(5, 3)\n  end\nend\n",
}


@needs_ruby
def test_detection(make_repo, context):
    profile = context(make_repo(RUBY_REPO, git=False)).profile
    assert profile.languages["Ruby"] == 2 and "test/calc_test.rb" in profile.test_files
    assert profile.tools["ruby"].available and profile.tools["rubocop"].available
    assert [c.name for c in ruby.checks(profile)] == ["rubocop", "minitest"]


@needs_ruby
def test_only_safe_autocorrections_count_as_fixable(make_repo, context):
    config = RUBOCOP_YML.replace("Style/FrozenStringLiteralComment:\n  Enabled: false\n", "")
    ctx = context(make_repo(RUBY_REPO | {".rubocop.yml": config}, git=False))
    findings = {f.message.split(":")[0]: f.fixable for f in ruby.Rubocop().check(ctx) if f.path == "lib/calc.rb"}
    assert findings["Layout/SpaceAfterComma"] and findings["Layout/SpaceAroundOperators"]
    assert findings["Style/FrozenStringLiteralComment"] is False  # needs `rubocop -A` (unsafe): reported only
    assert ruby.Rubocop().fix(ctx)["lib/calc.rb"].startswith("def add(a, b)\n  a + b\nend\n")
    assert (ctx.root / "lib/calc.rb").read_text() == RUBY_REPO["lib/calc.rb"]  # rules never write


def test_rubocop_is_opt_in(make_repo, context):
    ctx = context(make_repo({"lib/a.rb": "def  x;end\n"}, git=False))
    assert ruby.Rubocop().check(ctx) == [] and [c.name for c in ruby.checks(ctx.profile)] == []


def test_ruby_version_mismatch_is_reported(make_repo, context):
    ctx = context(make_repo({".ruby-version": "2.7.8\n", "a.rb": "x = 1\n"}, git=False))
    if not (ctx.profile.tools.get("ruby") and ctx.profile.tools["ruby"].version):
        pytest.skip("needs ruby")
    [finding] = ruby.RubyVersion().check(ctx)
    assert "pins Ruby 2.7.8" in finding.message and not finding.fixable


def test_interpreters_parse_real_output():
    minitest = ("  1) Failure:\nCalcTest#test_sub [/r/test/calc_test.rb:6]:\nExpected: 2\n  Actual: 8\n\n"
                "1 runs, 1 assertions, 1 failures, 0 errors, 0 skips\n")
    details = ruby.interpret_ruby_tests("minitest")(CommandResult("ruby", 1, minitest)).details
    assert details == ["CalcTest#test_sub /r/test/calc_test.rb:6", "Expected: 2", "Actual: 8"]
    rspec = ("Failure/Error: expect(sub(5, 3)).to eq(2)\n\n  expected: 2\n       got: 8\n"
             "rspec ./spec/calc_spec.rb:4 # sub\n")
    assert ruby.interpret_ruby_tests("rspec")(CommandResult("rspec", 1, rspec)).details == [
        "expected: 2", "got: 8", "rspec ./spec/calc_spec.rb:4 # sub"]


@needs_ruby
def test_plan_does_not_report_offenses_its_own_pending_edit_fixes(make_repo, tmp_path):
    root = make_repo(RUBY_REPO)
    data = Orchestrator(root, state_dir=tmp_path / "s").run("fix lint").to_dict()
    assert data["status"] == "proposed" and [e["path"] for e in data["edits"]] == ["lib/calc.rb"]
    assert not [f for f in data["findings"] if f["path"] == "lib/calc.rb"]


@needs_ruby
def test_apply_formats_but_reports_logic_bug(make_repo, tmp_path):
    root = make_repo(RUBY_REPO)
    task = "fix lint and the failing tests"
    data = Orchestrator(root, apply=True, state_dir=tmp_path / "s").run(task).to_dict()
    assert [(e["path"], e["rule"]) for e in data["edits"]] == [("lib/calc.rb", "ruby.rubocop")]
    assert "def sub(a, b)\n  a + b\nend" in (root / "lib/calc.rb").read_text()  # the bug stays
    minitest = next(c for c in data["verifications"][-1]["checks"] if c["name"] == "minitest")
    assert minitest["details"][:3] == ["CalcTest#test_sub test/calc_test.rb:6", "Expected: 2", "Actual: 8"]
    assert data["status"] == "needs_human"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout
    assert status.strip() == "M lib/calc.rb"
    again = Orchestrator(root, apply=True, state_dir=tmp_path / "s").run(task).to_dict()
    assert again["edits"] == [] and again["status"] == "needs_human"
