import json
import shutil
from pathlib import Path

import pytest
from test_go import GO_REPO
from test_javascript import JS_REPO
from test_rust import RUST_REPO

from cloudy.orchestrator import Orchestrator
from cloudy.rules import javascript
from cloudy.verifier import build_checks

TOOLS = ("node", "npm", "eslint", "prettier", "go", "gofmt", "cargo", "rustfmt", "git")
POLYGLOT = ({f"web/{k}": v for k, v in JS_REPO.items()}
            | {f"svc/{k}": v for k, v in GO_REPO.items() if not k.startswith("vendor")}
            | RUST_REPO
            | {".github/workflows/ci.yml": "on: [push]\njobs:\n  b:\n    runs-on: x\n    steps:\n"
                                          "      - run: npm ci || true\n      - run: cargo test || true\n"})


def test_nested_projects_are_detected(make_repo, context):
    files = {"package.json": json.dumps({"workspaces": ["packages/*"], "devDependencies": {"eslint": "9"}}),
             "packages/ui/package.json": json.dumps({"scripts": {"lint": "eslint ."}}),
             "packages/ui/eslint.config.mjs": "export default [];\n",
             "svc/go.mod": "module m\n", "svc/main.go": "package main\n",
             "Cargo.toml": '[workspace]\nmembers = ["crates/a"]\n',
             "crates/a/Cargo.toml": '[package]\nname = "a"\nedition = "2021"\n', "crates/a/src/lib.rs": ""}
    profile = context(make_repo(files, git=False)).profile
    assert [p.dir for p in profile.node_projects] == [".", "packages/ui"]
    assert profile.node_projects[1].deps == ["eslint"]  # inherited from the workspace root
    assert profile.node_projects[1].linters == {"eslint": "packages/ui/eslint.config.mjs"}
    assert profile.go_modules == ["svc"] and profile.cargo_roots == ["."]
    assert javascript.ScriptTools().check(context(Path(profile.root))) == []  # eslint declared by workspace root
    checks = {c.name: c for c in build_checks(profile)}
    assert checks["svc: go test"].cwd == "svc" and checks["cargo test"].cwd is None
    ui_lint = checks["packages/ui: npm lint"]  # dependencies declared but not installed: skipped, not guessed
    assert ui_lint.command is None and "run `npm install`" in ui_lint.skip_reason


@pytest.mark.skipif(not all(shutil.which(t) for t in TOOLS), reason="needs node, go and rust toolchains")
def test_polyglot_repo_end_to_end(make_repo, tmp_path):
    root = make_repo(POLYGLOT)
    task = "fix the CI, formatting and the failing tests"
    safe = Orchestrator(root, state_dir=tmp_path / "s").run(task).to_dict()
    assert safe["status"] == "proposed" and not (root / "web/src/greeting.js").read_text().startswith("const")
    assert safe["tool_side_effects"] == ["Cargo.lock"]  # written by cargo during checks, reported as such

    data = Orchestrator(root, apply=True, state_dir=tmp_path / "s").run(task).to_dict()
    assert {e["rule"] for e in data["edits"]} == {"ci.masked-failures", "ci.workflow-permissions", "js.prettier",
                                                  "js.eslint-autofix", "go.gofmt", "rust.rustfmt"}
    checks = {c["name"]: c for c in data["verifications"][-1]["checks"]}
    assert "calc_test.go:7: Sub(5, 3) = 8, want 2" in checks["svc: go test"]["details"]
    # reporter-agnostic: node <25 TAP and node >=25 spec both surface the failing test and its location
    assert any(d.endswith("calc.test.js:9:1") for d in checks["web: npm test"]["details"])
    assert any("sub" in d for d in checks["web: npm test"]["details"])
    assert "tests::subtracts" in checks["cargo test"]["details"]
    assert data["status"] == "needs_human"
    manual = [f["message"] for f in data["findings"] if not f["fixable"]]
    assert any("`npm ci` fails without package-lock.json" in m for m in manual)

    again = Orchestrator(root, apply=True, state_dir=tmp_path / "s").run(task).to_dict()
    assert again["edits"] == [] and again["status"] == "needs_human"
