import pytest

from cloudy.rules import ci, docs, repo


def fix_and_check(rule, ctx):
    return rule.fix(ctx), rule.check(ctx)


def test_run_lines_handles_single_and_block_commands():
    text = """jobs:
  a:
    steps:
      - run: echo one
      - name: b
        run: |
          echo two
          echo three
        env:
          X: 1
"""
    assert [cmd for _, cmd in ci.run_lines(text)] == ["echo one", "echo two", "echo three"]


def test_masked_failures_fix_tools_only(make_repo, context):
    root = make_repo({".github/workflows/ci.yml": """jobs:
  a:
    steps:
      - run: ruff check . || true
      - run: |
          grep -q foo file || true
          pytest -q || true
"""}, git=False)
    ctx = context(root)
    findings = ci.MaskedFailures().check(ctx)
    assert [f.fixable for f in findings] == [True, False, True]
    new = ci.MaskedFailures().fix(ctx)[".github/workflows/ci.yml"]
    assert "ruff check .\n" in new and "grep -q foo file || true" in new
    assert "pytest -q || [ $? -eq 5 ]" in new  # no tests exist yet


def test_masked_pytest_is_strict_when_tests_exist(make_repo, context):
    root = make_repo({".github/workflows/ci.yml": "jobs:\n  a:\n    steps:\n      - run: pytest || true\n",
                      "tests/test_x.py": "def test_x(): pass\n"}, git=False)
    assert "run: pytest\n" in ci.MaskedFailures().fix(context(root))[".github/workflows/ci.yml"]


def test_permissions_added_only_without_write_hints(make_repo, context):
    root = make_repo({".github/workflows/a.yml": "on: push\njobs:\n  a:\n    runs-on: x\n",
                      ".github/workflows/release.yml": "on: push\njobs:\n  publish:\n    runs-on: x\n"}, git=False)
    ctx = context(root)
    assert {f.path: f.fixable for f in ci.WorkflowPermissions().check(ctx)} == {
        ".github/workflows/a.yml": True, ".github/workflows/release.yml": False}
    assert ci.WorkflowPermissions().fix(ctx)[".github/workflows/a.yml"].startswith(
        "on: push\npermissions:\n  contents: read\n\njobs:")


def dependabot(*entries):
    body = "".join(f'  - package-ecosystem: "{e}"\n    directory: "/"\n    schedule:\n      interval: "weekly"\n'
                   for e in entries)
    return {".github/dependabot.yml": f"version: 2\nupdates:\n{body}"}


def test_dependabot_renames_aliases_and_drops_missing(make_repo, context):
    files = dependabot("javascript", "python", "cobol", "github-actions")
    files |= {"package.json": "{}", ".github/workflows/ci.yml": "on: push\n"}
    ctx = context(make_repo(files, git=False))
    messages = [(f.message, f.fixable) for f in ci.DependabotEcosystems().check(ctx)]
    assert [fixable for _, fixable in messages] == [True, True, False]
    new = ci.DependabotEcosystems().fix(ctx)[".github/dependabot.yml"]
    assert '"npm"' in new and "python" not in new and "cobol" in new and "github-actions" in new


def test_dependabot_never_empties_updates(make_repo, context):
    ctx = context(make_repo(dependabot("python"), git=False))
    assert ci.DependabotEcosystems().fix(ctx) == {}


@pytest.mark.parametrize("content,expected", [
    ("BoozeLee", "* @BoozeLee\n"),
    ("*.py alice @bob", "*.py @alice @bob\n"),
])
def test_codeowners_fixes(make_repo, context, content, expected):
    ctx = context(make_repo({".github/CODEOWNERS": content}, git=False))
    assert repo.CodeOwners().fix(ctx)[".github/CODEOWNERS"] == expected


def test_codeowners_lone_existing_path_is_valid_pattern(make_repo, context):
    ctx = context(make_repo({"CODEOWNERS": "docs\n", "docs/a.md": "x"}, git=False))
    assert repo.CodeOwners().check(ctx) == []


def test_license_mismatch_rewrites_only_license_section(make_repo, context):
    files = {"LICENSE": "MIT License\n\nPermission is hereby granted, free of charge, to any person",
             "README.md": "# X\n\nBuilt at MIT.\n\n## License\n\nApache 2.0\n\n## Next\n\nmore\n"}
    ctx = context(make_repo(files, git=False))
    assert docs.LicenseMismatch().fix(ctx)["README.md"] == (
        "# X\n\nBuilt at MIT.\n\n## License\n\nLicensed under the MIT License. See [LICENSE](LICENSE).\n\n"
        "## Next\n\nmore\n")


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/Org/Repo.git", "github.com/org/repo"),
    ("git@github.com:Org/Repo.git", "github.com/org/repo"),
    ("https://github.com/org/repo.cd", "github.com/org/repo.cd"),
    ("http://proxy@127.0.0.1:8080/git/org/repo", None),
])
def test_normalize_remote(url, expected):
    assert docs.normalize_remote(url) == expected


def test_clone_url_fixed_to_origin(make_repo, context):
    ctx = context(make_repo({"README.md": "```bash\ngit clone https://github.com/example-org/Widget.cd Widget\n```\n"}))
    assert docs.CloneUrl().fix(ctx)["README.md"] == (
        "```bash\ngit clone https://github.com/Example-Org/Widget.git Widget\n```\n")


def test_links_and_phantom_commands_are_reported_not_fixed(make_repo, context):
    files = {"README.md": "[a](missing.md) [b](../../outside) [c](https://x.org) [d](#top)\n\n```bash\n"
                          "npm test\nmake build\npip install -r requirements.txt\npython3 run.py\n```\n",
             "Makefile": "test:\n\ttrue\n"}
    ctx = context(make_repo(files, git=False))
    links = docs.BrokenLinks().check(ctx)
    assert [f.message.split("`")[1] for f in links] == ["missing.md", "../../outside"]
    commands = docs.PhantomCommands().check(ctx)
    assert len(commands) == 4 and not any(f.fixable for f in commands)
    assert docs.BrokenLinks().fix(ctx) == {} and docs.PhantomCommands().fix(ctx) == {}


def test_placeholders_deduplicated_per_line(make_repo, context):
    ctx = context(make_repo({"SECURITY.md": "[security@example.com](mailto:security@example.com)\n"}, git=False))
    assert len(docs.PlaceholderContacts().check(ctx)) == 1
