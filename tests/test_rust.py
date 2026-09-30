import shutil

import pytest

from cloudy.models import CommandResult
from cloudy.orchestrator import Orchestrator
from cloudy.rules import rust
from cloudy.verifier import build_checks, interpret_cargo

needs_rust = pytest.mark.skipif(not (shutil.which("cargo") and shutil.which("rustfmt")),
                                reason="needs cargo and rustfmt")

RUST_REPO = {
    ".gitignore": "target/\n",
    "Cargo.toml": '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": "pub fn add(a:i32,b:i32)->i32{a+b}\n\npub fn sub(a: i32, b: i32) -> i32 {\n    a + b\n}\n\n"
                  "#[cfg(test)]\nmod tests {\n    use super::*;\n\n    #[test]\n    fn subtracts() {\n"
                  "        assert_eq!(sub(5, 3), 2);\n    }\n}\n",
}


@needs_rust
def test_detection(make_repo, context):
    profile = context(make_repo(RUST_REPO, git=False)).profile
    assert "Rust" in profile.languages and "cargo" in profile.ecosystems
    assert profile.tools["cargo"].available and profile.tools["rustfmt"].available
    assert "cargo test" in profile.test_runners
    assert {c.name for c in build_checks(profile)} >= {"cargo fmt", "cargo clippy", "cargo test"}


def test_edition_resolution_and_missing_edition(make_repo, context):
    files = {"Cargo.toml": '[workspace]\nmembers = ["a", "b"]\n\n[workspace.package]\nedition = "2021"\n',
             "a/Cargo.toml": '[package]\nname = "a"\nversion = "0.1.0"\nedition.workspace = true\n',
             "b/Cargo.toml": '[package]\nname = "b"\nversion = "0.1.0"\n', "a/src/lib.rs": "", "b/src/lib.rs": ""}
    ctx = context(make_repo(files, git=False))
    assert rust.crate_dir(ctx, "a/src/lib.rs") == "a"
    assert rust.crate_edition(ctx, "a") == "2021" and rust.crate_edition(ctx, "b") == "2015"
    [finding] = rust.CrateEdition().check(ctx)
    assert finding.path == "b/Cargo.toml" and not finding.fixable


@needs_rust
def test_rustfmt_fix_via_stdin(make_repo, context):
    ctx = context(make_repo(RUST_REPO, git=False))
    assert [f.path for f in rust.Rustfmt().check(ctx)] == ["src/lib.rs"]
    fixed = rust.Rustfmt().fix(ctx)["src/lib.rs"]
    assert fixed.startswith("pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n")
    assert (ctx.root / "src/lib.rs").read_text() == RUST_REPO["src/lib.rs"]  # rules never write


def test_interpreter_parses_real_output():
    output = ("running 1 test\ntest tests::subtracts ... FAILED\n\nfailures:\n\n---- tests::subtracts stdout ----\n"
              "\nthread 'tests::subtracts' panicked at src/lib.rs:13:9:\nassertion `left == right` failed\n"
              "  left: 8\n right: 2\n")
    assert interpret_cargo("cargo test", "test")(CommandResult("cargo test", 101, output)).details == [
        "tests::subtracts", "src/lib.rs:13:9", "assertion `left == right` failed", "left: 8", "right: 2"]
    compile_error = "error[E0308]: mismatched types\n --> src/lib.rs:2:5\n"
    assert interpret_cargo("cargo test", "test")(CommandResult("c", 101, "", compile_error)).details == [
        "error[E0308]: mismatched types", "src/lib.rs:2:5"]


@needs_rust
def test_apply_formats_but_reports_logic_bug(make_repo, tmp_path):
    root = make_repo(RUST_REPO)
    task = "fix formatting and the failing tests"
    data = Orchestrator(root, apply=True, state_dir=tmp_path / "s").run(task).to_dict()
    assert [e["path"] for e in data["edits"]] == ["src/lib.rs"]
    source = (root / "src/lib.rs").read_text()
    assert "pub fn add(a: i32, b: i32) -> i32 {" in source and "-> i32 {\n    a + b\n}" in source.split("fn sub")[1]
    cargo_test = next(c for c in data["verifications"][-1]["checks"] if c["name"] == "cargo test")
    assert "tests::subtracts" in cargo_test["details"]
    assert any(d.startswith("src/lib.rs:") for d in cargo_test["details"])
    assert data["status"] == "needs_human"
    again = Orchestrator(root, apply=True, state_dir=tmp_path / "s").run(task).to_dict()
    assert again["edits"] == [] and again["status"] == "needs_human"
