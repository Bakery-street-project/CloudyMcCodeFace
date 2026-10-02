import json
import shutil

import pytest

from cloudy.models import CommandResult
from cloudy.orchestrator import Orchestrator
from cloudy.rules import docs, javascript
from cloudy.verifier import build_checks, interpret_eslint, interpret_js_test, interpret_prettier, interpret_tsc

needs_node = pytest.mark.skipif(not all(shutil.which(t) for t in ("node", "npm", "eslint", "prettier")),
                                reason="needs node, npm, eslint and prettier")

PACKAGE = json.dumps({"name": "calc", "type": "module", "scripts": {"test": "node --test"}}, indent=2) + "\n"
JS_REPO = {
    "package.json": PACKAGE,
    ".prettierrc": "{}\n",
    "eslint.config.mjs": 'export default [{ rules: { "no-var": "error", "prefer-const": "error" } }];\n',
    "src/greeting.js": 'var greeting = "hi";\nexport { greeting };\n',
    "src/calc.js": "export function add(a,b){return a+b}\n\nexport function sub(a, b) {\n  return a + b;\n}\n",
    "test/calc.test.js": 'import test from "node:test";\nimport assert from "node:assert";\n'
                         'import { add, sub } from "../src/calc.js";\n\n'
                         'test("add", () => {\n  assert.strictEqual(add(2, 2), 4);\n});\n\n'
                         'test("sub", () => {\n  assert.strictEqual(sub(5, 3), 2);\n});\n',
}


def test_detection(make_repo, context):
    files = JS_REPO | {"pnpm-lock.yaml": "lockfileVersion: '9.0'\n", "tsconfig.json": "{}\n"}
    profile = context(make_repo(files, git=False)).profile
    [project] = profile.node_projects
    assert project.dir == "." and project.package_manager == "pnpm" and project.scripts == {"test": "node --test"}
    assert {"eslint", "prettier", "tsc"} <= set(project.linters) <= set(profile.linters)
    assert "node:test" in profile.test_runners and "test/calc.test.js" in profile.test_files
    assert {"node", "pnpm", "eslint", "prettier", "tsc"} <= set(profile.tools)


def test_pinned_tool_is_never_replaced_by_a_global_one(make_repo, context):
    package = json.dumps({"devDependencies": {"prettier": "3.0.0"}}) + "\n"
    ctx = context(make_repo({"package.json": package, ".prettierrc": "{}\n", "a.js": "x=1\n"}, git=False))
    assert ctx.profile.project_tool(ctx.profile.node_projects[0], "prettier") is None
    [finding] = javascript.PrettierFormat().check(ctx)
    assert "pinned in package.json but not installed" in finding.message and not finding.fixable
    prettier = next(c for c in build_checks(ctx.profile) if c.name == "prettier")
    assert prettier.command is None and "run `npm install`" in prettier.skip_reason


@needs_node
def test_prettier_and_eslint_fix_via_stdin(make_repo, context):
    ctx = context(make_repo(JS_REPO, git=False))
    assert javascript.PrettierFormat().fix(ctx) == {
        "src/calc.js": "export function add(a, b) {\n  return a + b;\n}\n\nexport function sub(a, b) {\n"
                       "  return a + b;\n}\n"}
    assert javascript.EslintAutofix().fix(ctx) == {"src/greeting.js": 'const greeting = "hi";\nexport { greeting };\n'}
    assert (ctx.root / "src/greeting.js").read_text().startswith("var")  # rules never write


def test_lockfiles_and_script_tools_are_reported(make_repo, context):
    package = json.dumps({"packageManager": "yarn@4.0.0",
                          "scripts": {"test": 'echo "Error: no test specified" && exit 1', "lint": "eslint ."}})
    files = {"package.json": package, "package-lock.json": "{}", "pnpm-lock.yaml": "",
             ".github/workflows/ci.yml": "jobs:\n  a:\n    steps:\n      - run: yarn install --frozen-lockfile\n",
             "src/a.test.js": ""}
    ctx = context(make_repo(files, git=False))
    locks = [f.message for f in javascript.Lockfiles().check(ctx)]
    assert any("several lockfiles" in m for m in locks) and any("packageManager is yarn" in m for m in locks)
    assert any("fails without yarn.lock" in m for m in locks)
    scripts = [f.message for f in javascript.ScriptTools().check(ctx)]
    assert any("npm placeholder" in m for m in scripts) and any("`eslint` is not in" in m for m in scripts)
    assert not any(f.fixable for f in javascript.Lockfiles().check(ctx) + javascript.ScriptTools().check(ctx))


def test_docs_understand_yarn_and_pnpm_script_shorthand(make_repo, context):
    files = {"package.json": json.dumps({"scripts": {"build": "tsc"}}),
             "README.md": "```bash\nyarn build\npnpm lint\nyarn install\nbun dev\n```\n"}
    ctx = context(make_repo(files, git=False))
    messages = [f.message for f in docs.PhantomCommands().check(ctx)]
    assert messages == ["`pnpm lint` cannot work here: no `lint` script in package.json",
                        "`bun dev` cannot work here: no `dev` script in package.json"]


def test_interpreters_parse_real_output():
    node = CommandResult("npm run --silent test", 1, "not ok 2 - sub\n  location: 'test/calc.test.js:9:1'\n"
                         "  error: 'Expected values to be strictly equal:\\n\\n8 !== 2\\n'\n# fail 1\n")
    assert interpret_js_test("npm test")(node).details == ["not ok 2 - sub", "test/calc.test.js:9:1"]
    spec = CommandResult("npm run --silent test", 1,  # node >=25 default spec reporter
                         "✔ add (1.2ms)\n✖ sub (0.780366ms)\nℹ tests 2\nℹ pass 1\nℹ fail 1\n\n✖ failing tests:\n\n"
                         "test at test/calc.test.js:9:1\n✖ sub (0.780366ms)\n"
                         "  AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:\n  \n  8 !== 2\n")
    assert interpret_js_test("npm test")(spec).details == [
        "sub", "test/calc.test.js:9:1", "AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:"]
    eslint = CommandResult("eslint", 1, json.dumps([{"filePath": "/r/a.js", "messages": [
        {"line": 1, "ruleId": "no-var", "message": "Unexpected var"}]}]))
    assert interpret_eslint(eslint).details == ["/r/a.js:1 no-var Unexpected var"]
    prettier = CommandResult("prettier", 1, "Checking formatting...\n", "[warn] a.js\n[warn] Code style issues found\n")
    assert interpret_prettier(prettier).details == ["a.js"]
    tsc = CommandResult("tsc", 2, "src/t.ts(1,7): error TS2322: Type 'string' is not assignable to type 'number'.\n")
    assert interpret_tsc(tsc).details == [tsc.stdout.strip()]


@needs_node
def test_apply_formats_and_fixes_lint_but_reports_logic_bug(make_repo, tmp_path):
    root = make_repo(JS_REPO)
    task = "fix the lint and formatting, then the failing tests"
    data = Orchestrator(root, apply=True, state_dir=tmp_path / "s").run(task).to_dict()
    assert {e["rule"] for e in data["edits"]} == {"js.prettier", "js.eslint-autofix"}
    assert (root / "src/greeting.js").read_text().startswith("const greeting")
    assert "return a + b;\n}\n" in (root / "src/calc.js").read_text().split("function sub")[1]  # bug untouched
    assert data["status"] == "needs_human"
    test_check = next(c for c in data["verifications"][-1]["checks"] if c["name"] == "npm test")
    # node <25 prints TAP ("not ok 2 - sub"), node >=25 the spec reporter ("sub"); both report the location.
    assert "test/calc.test.js:9:1" in test_check["details"]
    assert any("sub" in d for d in test_check["details"])
    again = Orchestrator(root, apply=True, state_dir=tmp_path / "s").run(task).to_dict()
    assert again["edits"] == [] and again["status"] == "needs_human"


@pytest.mark.skipif(not shutil.which("tsc"), reason="needs tsc")
def test_type_error_is_reported_not_fixed(make_repo, tmp_path):
    files = {"package.json": json.dumps({"name": "t"}) + "\n",
             "tsconfig.json": '{"compilerOptions": {"strict": true}}\n',
             "src/t.ts": 'export const n: number = "s";\n'}
    root = make_repo(files)
    data = Orchestrator(root, apply=True, state_dir=tmp_path / "s").run("fix lint").to_dict()
    tsc = next(c for c in data["verifications"][-1]["checks"] if c["name"] == "tsc")
    assert tsc["status"] == "fail" and any("src/t.ts(1,14): error TS2322" in d for d in tsc["details"])
    assert data["edits"] == [] and data["status"] == "needs_human"
