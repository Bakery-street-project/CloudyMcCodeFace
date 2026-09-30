import textwrap

from cloudy.config import load_config
from cloudy.orchestrator import Orchestrator
from cloudy.plugins import load_registry

FOO_PLUGIN = '''
from cloudy.rules import Rule
from cloudy.verifier import command_check

LANGUAGES = {".foo": "Foo"}
LANG_TOOLS = {"Foo": ("cat",)}
VERSION_ARGS = {"cat": None}


class TrailingSpace(Rule):
    id = "foo.trailing-space"
    intents = frozenset({"fix_lint"})
    summary = "Foo files must not have trailing spaces"

    def _files(self, ctx):
        return [f for f in ctx.match("*.foo") if any(l != l.rstrip() for l in ctx.read(f).splitlines())]

    def check(self, ctx):
        return [self.finding(f, "trailing spaces", fixable=True) for f in self._files(ctx)]

    def fix(self, ctx):
        return {f: "\\n".join(l.rstrip() for l in ctx.read(f).splitlines()) + "\\n" for f in self._files(ctx)}


RULES = [TrailingSpace()]


def checks(profile):
    if "Foo" not in profile.languages:
        return []
    return [command_check(profile, "foo syntax", "lint", "cat", ["a.foo"])]
'''


def write(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(textwrap.dedent(text))


def test_user_plugin_adds_language_tools_checks_and_rules(make_repo, tmp_path):
    user = tmp_path / "user-rules"
    write(user, "foo.py", FOO_PLUGIN)
    root = make_repo({"a.foo": "x  \ny\n"})
    registry = load_registry(root, user_dir=user)
    assert [p.name for p in registry.plugins][-1] == "user:foo.py" and not registry.warnings
    data = Orchestrator(root, apply=True, state_dir=tmp_path / "s", registry=registry).run("fix lint").to_dict()
    assert data["profile"]["languages"]["Foo"] == 1 and data["profile"]["tools"]["cat"]["path"]
    assert [e["rule"] for e in data["edits"]] == ["foo.trailing-space"]
    assert (root / "a.foo").read_text() == "x\ny\n"
    checks = {c["name"]: c["status"] for c in data["verifications"][-1]["checks"]}
    assert checks["foo syntax"] == "pass" and data["status"] == "done"


def test_repo_plugins_need_explicit_trust(make_repo, tmp_path):
    root = make_repo({"a.foo": "x  \n"})
    write(root / ".cloudy" / "rules", "foo.py", FOO_PLUGIN)
    untrusted = load_registry(root, user_dir=tmp_path / "none")
    assert all(p.source == "builtin" for p in untrusted.plugins)
    assert "pass --trust-repo-rules" in untrusted.warnings[0]
    trusted = load_registry(root, trust_repo=True, user_dir=tmp_path / "none")
    assert trusted.plugins[-1].name == "repo:foo.py" and trusted.plugins[-1].source == "repo"


def test_broken_and_conflicting_plugins_are_reported_not_fatal(make_repo, tmp_path):
    user = tmp_path / "rules"
    write(user, "bad_shape.py", "RULES = ['not a rule']\n")
    write(user, "crash.py", "raise RuntimeError('boom')\n")
    write(user, "no_rules.py", "X = 1\n")
    write(user, "dupe.py", "from cloudy.rules.ci import MaskedFailures\nRULES = [MaskedFailures()]\n")
    write(user, "bad_intent.py", '''
        from cloudy.rules import Rule
        class R(Rule):
            id = "x.y"
            intents = frozenset({"deploy"})
        RULES = [R()]
    ''')
    write(user, "_private.py", "raise RuntimeError('never imported')\n")
    registry = load_registry(make_repo({}), user_dir=user)
    warnings = "\n".join(registry.warnings)
    assert "not a cloudy.rules.Rule instance" in warnings and "RuntimeError: boom" in warnings
    assert "must define RULES" in warnings and "intents must be" in warnings and "never imported" not in warnings
    rules = registry.rules()
    assert [r.id for r in rules].count("ci.masked-failures") == 1
    assert "already defined by ci" in "\n".join(registry.warnings)


def test_disabled_groups_and_rules_from_config(make_repo, tmp_path):
    config_text = 'disabled_groups = ["docs", "js"]\ndisabled_rules = ["ci.workflow-permissions"]\n'
    root = make_repo({"cloudy.toml": config_text})
    registry = load_registry(root, user_dir=tmp_path / "none")
    config = load_config(root)
    ids = {r.id for r in registry.rules(config["disabled_rules"], config["disabled_groups"])}
    assert not any(i.startswith(("docs.", "js.")) for i in ids)
    assert "ci.workflow-permissions" not in ids and "ci.masked-failures" in ids
