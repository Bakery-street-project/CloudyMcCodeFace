"""CI rules: workflows that cannot fail, missing token scoping, invalid Dependabot config."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ..editor import validate
from . import RepoContext, Rule

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

WORKFLOWS = (".github/workflows/*.yml", ".github/workflows/*.yaml")
MASK = re.compile(r"\s*\|\|\s*(?:true|:|exit\s+0)\s*$")
CHECK_TOOL = re.compile(
    r"^(?:python3?\s+-m\s+)?(?:pip3?|pytest|ruff|flake8|pylint|mypy|bandit|black|isort|eslint|prettier|tsc|npm|"
    r"npx|yarn|pnpm|bun|jest|vitest|go|gofmt|cargo|rustfmt|make|golangci-lint|shellcheck|pre-commit|bundle|rake|"
    r"rubocop|rspec|cmake|ctest|clang-format)\b"
)
PYTEST = re.compile(r"^(?:python3?\s+-m\s+)?pytest\b")
RUN_KEY = re.compile(r"^(\s*)(-\s+)?run:\s*(.*)$")


def run_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield (line index, command) for every shell command line in a workflow's `run:` keys."""
    block_indent: int | None = None
    for index, raw in enumerate(text.splitlines()):
        indent = len(raw) - len(raw.lstrip())
        if block_indent is not None:
            if not raw.strip():
                continue
            if indent > block_indent:
                yield index, raw.strip()
                continue
            block_indent = None
        match = RUN_KEY.match(raw)
        if not match:
            continue
        value = match.group(3).strip()
        if value.startswith(("|", ">")):
            block_indent = len(match.group(1)) + len(match.group(2) or "")
        elif value and not value.startswith("#"):
            yield index, value


class MaskedFailures(Rule):
    id = "ci.masked-failures"
    intents = frozenset({"fix_ci"})
    summary = "CI steps whose failures are swallowed by `|| true` can never fail the build"
    hint = "Decide whether the step may fail; if not, drop the mask. `continue-on-error` needs a stated reason."

    def check(self, ctx: RepoContext):
        findings = []
        for path in ctx.match(*WORKFLOWS):
            text = ctx.read(path) or ""
            for index, command in run_lines(text):
                if MASK.search(command):
                    core = MASK.sub("", command)
                    fixable = bool(CHECK_TOOL.match(core))
                    note = "" if fixable else " (not a known check/install tool; review manually)"
                    findings.append(self.finding(path, f"`{command}` can never fail the job{note}", index + 1, fixable))
            for index, raw in enumerate(text.splitlines()):
                if re.match(r"^\s*continue-on-error:\s*true\b", raw):
                    findings.append(self.finding(path, "`continue-on-error: true` hides step failures", index + 1))
        return findings

    def fix(self, ctx: RepoContext):
        changes = {}
        for path in ctx.match(*WORKFLOWS):
            text = ctx.read(path) or ""
            lines = text.splitlines(keepends=True)
            for index, command in run_lines(text):
                core = MASK.sub("", command)
                if core == command or not CHECK_TOOL.match(core):
                    continue
                body = lines[index].rstrip("\r\n")
                newline = lines[index][len(body):]
                # pytest exits 5 when it collects no tests; tolerate only that until tests exist.
                suffix = " || [ $? -eq 5 ]" if PYTEST.match(core) and not ctx.profile.test_files else ""
                lines[index] = MASK.sub("", body) + suffix + newline
            new = "".join(lines)
            if new != text:
                changes[path] = new
        return changes


WRITE_HINTS = re.compile(r"GITHUB_TOKEN|secrets\.|release|deploy|publish|pages|\bgh\s", re.I)


class WorkflowPermissions(Rule):
    id = "ci.workflow-permissions"
    intents = frozenset({"fix_ci"})
    summary = "Workflows without a `permissions:` block get the repository's default (often write) token"
    hint = "Add the narrowest `permissions:` the jobs need (e.g. `contents: write` only for release jobs)."

    def check(self, ctx: RepoContext):
        findings = []
        for path in ctx.match(*WORKFLOWS):
            text = ctx.read(path) or ""
            if re.search(r"^permissions:", text, re.M) or not re.search(r"^jobs:", text, re.M):
                continue
            fixable = not WRITE_HINTS.search(text)
            note = "" if fixable else " (workflow may need write access; review manually)"
            findings.append(self.finding(path, f"no top-level `permissions:`; token scope is implicit{note}",
                                         fixable=fixable))
        return findings

    def fix(self, ctx: RepoContext):
        changes = {}
        for finding in self.check(ctx):
            if finding.fixable:
                text = ctx.read(finding.path) or ""
                changes[finding.path] = re.sub(r"^jobs:", "permissions:\n  contents: read\n\njobs:", text,
                                               count=1, flags=re.M)
        return changes


DEPENDABOT = (".github/dependabot.yml", ".github/dependabot.yaml")
VALID_ECOSYSTEMS = frozenset({
    "bun", "bundler", "cargo", "composer", "devcontainers", "docker", "docker-compose", "dotnet-sdk", "elm",
    "github-actions", "gitsubmodule", "gomod", "gradle", "helm", "maven", "mix", "npm", "nuget", "pip",
    "pub", "swift", "terraform", "uv",
})
ALIASES = {
    "javascript": "npm", "js": "npm", "node": "npm", "nodejs": "npm", "typescript": "npm", "yarn": "npm",
    "pnpm": "npm", "python": "pip", "pipenv": "pip", "poetry": "pip", "go": "gomod", "golang": "gomod",
    "rust": "cargo", "ruby": "bundler", "php": "composer", "actions": "github-actions",
    "github_actions": "github-actions",
}
MANIFEST_GLOBS = {
    "npm": ("package.json",), "bun": ("package.json",), "pip": ("requirements*.txt", "pyproject.toml", "setup.py",
    "setup.cfg", "Pipfile"), "uv": ("uv.lock", "pyproject.toml"), "gomod": ("go.mod",), "cargo": ("Cargo.toml",),
    "bundler": ("Gemfile",), "composer": ("composer.json",), "docker": ("Dockerfile", "*.Dockerfile"),
    "github-actions": (".github/workflows/*.yml", ".github/workflows/*.yaml", "action.yml", "action.yaml"),
}
ENTRY = re.compile(r"^(\s*)-\s+package-ecosystem:\s*([\"']?)([\w.-]+)\2\s*(?:#.*)?$")
DIRECTORY = re.compile(r"^\s*directory:\s*[\"']?([^\"'#\s]+)")


def has_manifest(root: Path, directory: str, ecosystem: str) -> bool | None:
    globs = MANIFEST_GLOBS.get(ecosystem)
    if globs is None:
        return None
    base = root / directory.strip("/")
    return any(next(base.glob(pattern), None) for pattern in globs)


def dependabot_entries(lines: list[str]) -> list[tuple[int, int, str, str]]:
    """Return (start, end, ecosystem, directory) for each `- package-ecosystem:` block."""
    starts = [i for i, line in enumerate(lines) if ENTRY.match(line)]
    entries = []
    for number, start in enumerate(starts):
        match = ENTRY.match(lines[start])
        dash = len(match.group(1))
        limit = starts[number + 1] if number + 1 < len(starts) else len(lines)
        end, directory = start + 1, "/"
        while end < limit:
            line = lines[end]
            if line.strip() and len(line) - len(line.lstrip()) <= dash:
                break
            if found := DIRECTORY.match(line):
                directory = found.group(1)
            end += 1
        entries.append((start, end, match.group(3), directory))
    return entries


class DependabotEcosystems(Rule):
    id = "ci.dependabot-ecosystems"
    intents = frozenset({"fix_ci"})
    summary = "Dependabot entries must use valid ecosystem names and point at existing manifests"
    hint = "Use a documented package-ecosystem name, or remove the entry."

    def _plan(self, ctx: RepoContext, path: str):
        """Yield (entry, finding message, replacement ecosystem or None to drop, fixable)."""
        lines = (ctx.read(path) or "").splitlines(keepends=True)
        for entry in dependabot_entries(lines):
            _, _, name, directory = entry
            canonical = name if name in VALID_ECOSYSTEMS else ALIASES.get(name.lower())
            if canonical is None:
                yield entry, f"unknown package-ecosystem `{name}`", None, False
                continue
            present = has_manifest(ctx.root, directory, canonical)
            if present is False:
                yield entry, f"`{name}` has no manifest in `{directory}`; Dependabot will error", None, True
            elif canonical != name:
                yield entry, f"invalid package-ecosystem `{name}`; did you mean `{canonical}`?", canonical, True

    def check(self, ctx: RepoContext):
        return [self.finding(path, message, entry[0] + 1, fixable)
                for path in DEPENDABOT if ctx.read(path) is not None
                for entry, message, _, fixable in self._plan(ctx, path)]

    def fix(self, ctx: RepoContext):
        changes = {}
        for path in DEPENDABOT:
            text = ctx.read(path)
            if text is None:
                continue
            lines = text.splitlines(keepends=True)
            for (start, end, name, _), _, replacement, fixable in reversed(list(self._plan(ctx, path))):
                if not fixable:
                    continue
                if replacement:
                    lines[start] = lines[start].replace(name, replacement, 1)
                else:
                    del lines[start:end]
            new = "".join(lines)
            if new != text and not validate(path, new) and self._has_updates(new):
                changes[path] = new
        return changes

    @staticmethod
    def _has_updates(text: str) -> bool:
        if yaml is None:
            return any(ENTRY.match(line) for line in text.splitlines())
        data = yaml.safe_load(text) or {}
        return bool(data.get("updates"))


RULES = [MaskedFailures(), WorkflowPermissions(), DependabotEcosystems()]
