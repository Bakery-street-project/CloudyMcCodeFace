"""Rust rules. Formatting uses rustfmt with the crate's own edition and rustfmt.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath

from . import RepoContext, Rule, pipe_through


def _manifest(ctx: RepoContext, rel: str) -> dict:
    try:
        return tomllib.loads(ctx.read(rel) or "")
    except tomllib.TOMLDecodeError:
        return {}


def crate_dir(ctx: RepoContext, path: str) -> str | None:
    """Nearest directory at or above `path` that has a Cargo.toml with a [package]."""
    for parent in PurePosixPath(path).parents:
        manifest = str(parent / "Cargo.toml")
        if ctx.exists(manifest) and "package" in _manifest(ctx, manifest):
            return str(parent)
    return None


def crate_edition(ctx: RepoContext, directory: str) -> str:
    edition = _manifest(ctx, str(PurePosixPath(directory) / "Cargo.toml"))["package"].get("edition", "2015")
    if isinstance(edition, dict):  # edition.workspace = true
        for parent in PurePosixPath(directory).parents:
            workspace = _manifest(ctx, str(parent / "Cargo.toml")).get("workspace", {})
            if "package" in workspace:
                return str(workspace["package"].get("edition", "2015"))
        return "2015"
    return str(edition)


class CrateEdition(Rule):
    id = "rust.edition"
    intents = frozenset({"fix_ci", "fix_lint"})
    summary = "Crates should state their edition; without it Cargo silently uses 2015"
    hint = "Add `edition = \"2015\"` to keep behaviour, or migrate with `cargo fix --edition` and review."

    def check(self, ctx: RepoContext):
        findings = []
        for manifest in ctx.match("Cargo.toml", "*/Cargo.toml"):
            package = _manifest(ctx, manifest).get("package")
            if isinstance(package, dict) and "edition" not in package:
                findings.append(self.finding(manifest, "no `edition` in [package]; Cargo uses 2015. Set it "
                                                       "deliberately — changing editions can change behaviour"))
        return findings


class Rustfmt(Rule):
    id = "rust.rustfmt"
    intents = frozenset({"fix_lint"})
    summary = "Format Rust files with rustfmt, using each crate's edition and rustfmt.toml"

    def _unformatted(self, ctx: RepoContext) -> tuple[list[str], list[str]]:
        """(files needing formatting, problems) across every Cargo root."""
        profile = ctx.profile
        if not (profile.has_tool("cargo") and profile.has_tool("rustfmt")):
            return [], []
        files, problems = [], []
        for crate in profile.cargo_roots:
            result = ctx.executor.run([profile.tool_path("cargo"), "fmt", "--check", "--", "-l"],
                                      cwd=None if crate == "." else crate)
            found = []
            for line in result.stdout.splitlines():
                path = Path(line.strip())
                if path.suffix == ".rs" and path.is_absolute() and path.is_relative_to(ctx.root):
                    found.append(path.relative_to(ctx.root).as_posix())
            if not result.ok and not found:
                first = next((line for line in result.stderr.splitlines() if line.strip()), f"exit {result.exit_code}")
                problems.append(f"cargo fmt could not run in {crate}: {first}")
            files += found
        return files, problems

    def check(self, ctx: RepoContext):
        files, problems = self._unformatted(ctx)
        findings = [self.finding("Cargo.toml", problem) for problem in problems]
        return findings + [self.finding(f, "not rustfmt-formatted", fixable=True) for f in files]

    def fix(self, ctx: RepoContext):
        files, _ = self._unformatted(ctx)
        changes = {}
        for path in files:
            directory = crate_dir(ctx, path)
            if directory is None:
                continue
            argv = [ctx.profile.tool_path("rustfmt"), "--edition", crate_edition(ctx, directory), "--emit", "stdout"]
            if formatted := pipe_through(ctx, argv, path, cwd=None if directory == "." else directory):
                changes[path] = formatted
        return changes


RULES = [CrateEdition(), Rustfmt()]
