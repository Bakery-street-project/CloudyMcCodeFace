import pytest

from cloudy.config import ConfigError, load_config
from cloudy.models import Finding
from cloudy.planner import Planner, classify


@pytest.mark.parametrize("task,intents", [
    ("Analyze this repo and tell me what tools and conventions it already uses", ["analyze"]),
    ("Fix the CI so it can actually fail", ["fix_ci"]),
    ("Update the README and config files so they match reality", ["fix_ci", "sync_docs"]),
    ("Run the test suite, interpret the failures, and propose/apply minimal fixes", ["fix_tests"]),
    ("fix the failing tests and clean the CI config", ["fix_ci", "fix_tests"]),
    ("make it production ready", ["fix_ci", "sync_docs", "fix_lint", "fix_tests"]),
    ("hello", ["analyze"]),
])
def test_classify(task, intents):
    assert classify(task) == intents


class FakeRule:
    id, summary = "r", "fix r"


def test_planner_skips_rules_without_progress():
    planner = Planner(["fix_ci"])
    findings = [Finding("r", "a", "m", fixable=True)]
    assert len(planner.next_fixes(findings, [FakeRule()], 1)) == 1
    assert planner.next_fixes(findings, [FakeRule()], 2) == []
    assert len(planner.next_fixes([Finding("r", "a", "other", fixable=True)], [FakeRule()], 3)) == 1


def test_config_defaults_and_pyproject(tmp_path):
    assert load_config(tmp_path)["max_cycles"] == 3
    (tmp_path / "pyproject.toml").write_text('[tool.cloudy]\nmax_cycles = 5\nchecks = { smoke = "true" }\n')
    config = load_config(tmp_path)
    assert config["max_cycles"] == 5 and config["checks"] == {"smoke": "true"}


@pytest.mark.parametrize("body,message", [
    ("colour = 1", "unknown key"), ("timeout = -1", "timeout"), ("max_cycles = 99", "max_cycles"),
    ('deny = ["("]', "not a valid regex"), ("deny = 3", "list of strings"), ("x = [", "invalid TOML"),
])
def test_config_errors(tmp_path, body, message):
    (tmp_path / "cloudy.toml").write_text(body)
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path)
