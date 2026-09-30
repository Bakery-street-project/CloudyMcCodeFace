"""Documentation rules: docs must describe the repository as it actually is."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import PurePosixPath

from . import RepoContext, Rule

READMES = ("README.md", "README.MD", "Readme.md", "readme.md")
LICENSE_NAMES = {
    "MIT": r"\bMIT\b", "Apache-2.0": r"\bApache\b", "AGPL-3.0": r"\bAGPL", "LGPL": r"\bLGPL",
    "GPL": r"(?<![AL])\bGPL\b|GNU General Public", "MPL-2.0": r"\bMPL\b|Mozilla Public", "BSD": r"\bBSD\b",
    "ISC": r"\bISC\b", "Unlicense": r"\bUnlicense\b", "Proprietary": r"\bproprietary\b|all rights reserved",
}
LICENSE_LINES = {
    "MIT": "Licensed under the MIT License.", "Apache-2.0": "Licensed under the Apache License 2.0.",
    "Proprietary": "Proprietary — all rights reserved.",
}
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
FENCE = re.compile(r"^\s*(```|~~~)")
SHELL_LANGS = {"", "bash", "sh", "shell", "console", "zsh", "shell-session"}
# yarn/pnpm/bun subcommands that are not package.json scripts (`yarn build` runs the `build` script).
PM_BUILTINS = {"install", "i", "add", "remove", "rm", "dlx", "exec", "create", "init", "why", "up", "upgrade",
               "update", "info", "run", "test", "start", "link", "unlink", "outdated", "list", "ls", "audit",
               "publish", "pack", "config", "cache", "global", "import", "set", "workspace", "workspaces", "x"}


def section(lines: list[str], title: re.Pattern) -> tuple[int, int] | None:
    """Return (heading index, end index) of the first markdown section whose heading matches title."""
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if match and title.search(match.group(2)):
            level = len(match.group(1))
            end = index + 1
            while end < len(lines):
                other = HEADING.match(lines[end])
                if other and len(other.group(1)) <= level:
                    break
                end += 1
            return index, end
    return None


def markdown_files(ctx: RepoContext) -> list[str]:
    return [f for f in ctx.profile.docs if f.lower().endswith(".md")]


def shell_blocks(text: str, inline: bool = False):
    """Yield (line number, command) for commands in shell-like fenced blocks (and inline code if asked)."""
    in_block, shell = False, False
    for number, line in enumerate(text.splitlines(), 1):
        fence = FENCE.match(line)
        if fence:
            if in_block:
                in_block = False
            else:
                in_block, shell = True, line.strip().lstrip("`~").strip().lower() in SHELL_LANGS
            continue
        stripped = line.strip()
        if in_block and shell and stripped and not stripped.startswith("#"):
            yield number, stripped.removeprefix("$ ")
        elif inline and not in_block:
            for span in re.findall(r"`([^`]+)`", line):
                yield number, span.strip()


class LicenseMismatch(Rule):
    id = "docs.license-mismatch"
    intents = frozenset({"sync_docs"})
    summary = "README must name the license that LICENSE actually contains"

    def _problem(self, ctx: RepoContext):
        actual = ctx.profile.license
        if not actual or actual == "Unknown":
            return None
        for path in READMES:
            text = ctx.read(path)
            if text is None:
                continue
            lines = text.splitlines(keepends=True)
            span = section(lines, re.compile(r"licen[cs]e", re.I))
            scope = "".join(lines[span[0] + 1:span[1]]) if span else ""
            claimed = [name for name, pattern in LICENSE_NAMES.items()
                       if name != actual and re.search(pattern, scope, re.I)]
            if claimed and not re.search(LICENSE_NAMES[actual], scope, re.I):
                return path, lines, span, claimed, actual
        return None

    def check(self, ctx: RepoContext):
        problem = self._problem(ctx)
        if not problem:
            return []
        path, _, span, claimed, actual = problem
        return [self.finding(path, f"README says {'/'.join(claimed)} but LICENSE is {actual}", span[0] + 1,
                             fixable=actual in LICENSE_LINES)]

    def fix(self, ctx: RepoContext):
        problem = self._problem(ctx)
        if not problem or problem[4] not in LICENSE_LINES:
            return {}
        path, lines, (start, end), _, actual = problem
        trailing = "\n" if end < len(lines) else ""
        lines[start + 1:end] = [f"\n{LICENSE_LINES[actual]} See [LICENSE](LICENSE).\n{trailing}"]
        return {path: "".join(lines)}


def normalize_remote(url: str) -> str | None:
    """Map https/ssh GitHub-style remotes to `host/owner/repo`; None for local or proxied remotes."""
    url = url.strip()
    if match := re.match(r"^git@([\w.-]+):(.+)$", url):
        host, path = match.groups()
    elif match := re.match(r"^(?:https?|ssh)://(?:[^@/]+@)?([\w.-]+)(?::\d+)?/(.+)$", url):
        host, path = match.groups()
        if re.search(r":\d+/", url):
            return None
    else:
        return None
    if host in ("localhost", "127.0.0.1") or "." not in host:
        return None
    path = path.rstrip("/").removesuffix(".git")
    return f"{host}/{path}".lower() if path.count("/") == 1 else None


class CloneUrl(Rule):
    id = "docs.clone-url"
    intents = frozenset({"sync_docs"})
    summary = "`git clone` instructions must point at this repository's origin"

    def _scan(self, ctx: RepoContext):
        remote = normalize_remote(ctx.profile.git.get("remote") or "")
        if not remote:
            return
        for path in markdown_files(ctx):
            for number, command in shell_blocks(ctx.read(path) or "", inline=True):
                try:
                    tokens = shlex.split(command)
                except ValueError:
                    continue
                if tokens[:2] != ["git", "clone"]:
                    continue
                url = next((t for t in tokens[2:] if not t.startswith("-")), None)
                if url and normalize_remote(url) != remote:
                    yield path, number, url, f"https://{_case_preserving(ctx, remote)}.git"

    def check(self, ctx: RepoContext):
        return [self.finding(path, f"clone URL `{url}` does not match origin ({good})", number, True)
                for path, number, url, good in self._scan(ctx)]

    def fix(self, ctx: RepoContext):
        changes: dict[str, str] = {}
        for path, number, url, good in self._scan(ctx):
            lines = (changes.get(path) or ctx.read(path) or "").splitlines(keepends=True)
            lines[number - 1] = lines[number - 1].replace(url, good, 1)
            changes[path] = "".join(lines)
        return changes


def _case_preserving(ctx: RepoContext, normalized: str) -> str:
    raw = ctx.profile.git.get("remote") or ""
    match = re.search(re.escape(normalized.split("/", 1)[1]), raw, re.I)
    host = normalized.split("/", 1)[0]
    return f"{host}/{match.group(0)}" if match else normalized


class BrokenLinks(Rule):
    id = "docs.broken-links"
    intents = frozenset({"sync_docs"})
    summary = "Relative links in docs must point at files that exist inside the repository"
    hint = "Create the target, fix the path, or replace the link with an absolute URL."

    def check(self, ctx: RepoContext):
        findings = []
        for path in markdown_files(ctx):
            base = PurePosixPath(path).parent
            for number, line in enumerate((ctx.read(path) or "").splitlines(), 1):
                for target in LINK.findall(line):
                    if re.match(r"^([a-z][\w+.-]*:|#|/)", target, re.I):
                        continue
                    rel = target.split("#", 1)[0].split("?", 1)[0]
                    resolved = (ctx.root / base / rel).resolve()
                    if not resolved.is_relative_to(ctx.root):
                        findings.append(self.finding(path, f"link `{target}` leaves the repository; "
                                                           "use an absolute URL", number))
                    elif not resolved.exists():
                        findings.append(self.finding(path, f"link `{target}` points to a missing file", number))
        return findings


class PhantomCommands(Rule):
    id = "docs.phantom-commands"
    intents = frozenset({"sync_docs"})
    summary = "Commands shown in docs must be runnable in this repository"
    hint = "Update the docs to the commands the project really has, or add the missing script/file."

    def _problem(self, ctx: RepoContext, cwd: PurePosixPath, tokens: list[str]) -> str | None:
        def has(rel: str) -> bool:
            return ctx.exists(str(cwd / rel))

        tool, args = tokens[0], tokens[1:]
        if tool in ("npm", "yarn", "pnpm", "bun"):
            if not has("package.json"):
                return "no package.json"
            scripts = self._package_scripts(ctx, cwd)
            script = args[1] if args[:1] == ["run"] and len(args) > 1 else args[0] if args[:1] in (
                ["start"], ["test"]) else None
            if tool != "npm" and args and not args[0].startswith("-") and args[0] not in PM_BUILTINS:
                script = args[0]
            if script and script not in scripts and not (script == "start" and has("server.js")):
                return f"no `{script}` script in package.json"
        elif tool in ("pip", "pip3") and args[:1] == ["install"]:
            if "-r" in args and args.index("-r") + 1 < len(args) and not has(args[args.index("-r") + 1]):
                return f"{args[args.index('-r') + 1]} does not exist"
            if any(a in (".", "-e.") for a in args) or args[-2:] == ["-e", "."]:
                if not (has("pyproject.toml") or has("setup.py")):
                    return "no pyproject.toml or setup.py"
        elif tool == "go" and args[:1] and args[0] in ("build", "test", "run", "vet") and not has("go.mod"):
            return "no go.mod"
        elif tool == "cargo" and not has("Cargo.toml"):
            return "no Cargo.toml"
        elif tool == "make":
            if not has("Makefile"):
                return "no Makefile"
            targets = ctx.profile.scripts.get("Makefile", [])
            missing = [a for a in args if not a.startswith("-") and "=" not in a and a not in targets]
            if missing and cwd == PurePosixPath("."):
                return f"no Makefile target `{missing[0]}`"
        elif tool == "docker" and args[:1] == ["build"] and not has("Dockerfile"):
            return "no Dockerfile"
        elif tool == "bundle" and not has("Gemfile"):
            return "no Gemfile"
        elif tool == "rake" and not any(has(name) for name in ("Rakefile", "rakefile", "Rakefile.rb")):
            return "no Rakefile"
        elif tool == "cmake" and "-S" not in args and "--build" not in args and not has("CMakeLists.txt"):
            return "no CMakeLists.txt"
        elif tool in ("python", "python3") and args and args[0].endswith(".py") and not has(args[0]):
            return f"{args[0]} does not exist"
        return None

    @staticmethod
    def _package_scripts(ctx: RepoContext, cwd: PurePosixPath) -> set[str]:
        try:
            return set(json.loads(ctx.read(str(cwd / "package.json")) or "{}").get("scripts", {}))
        except (json.JSONDecodeError, AttributeError):
            return set()

    def check(self, ctx: RepoContext):
        findings = []
        for path in markdown_files(ctx):
            cwd, clone_dir = PurePosixPath("."), None
            for number, command in shell_blocks(ctx.read(path) or ""):
                for part in re.split(r"\s*(?:&&|;)\s*", command):
                    try:
                        tokens = shlex.split(part, comments=True)
                    except ValueError:
                        continue
                    if not tokens:
                        continue
                    if tokens[:2] == ["git", "clone"]:
                        rest = [t for t in tokens[2:] if not t.startswith("-")]
                        clone_dir = rest[1] if len(rest) > 1 else PurePosixPath(rest[0]).name.removesuffix(
                            ".git") if rest else None
                        continue
                    if tokens[0] == "cd" and len(tokens) > 1:
                        target = tokens[1]
                        cwd = PurePosixPath(".") if target in (clone_dir, ctx.root.name, "..") else cwd / target
                        continue
                    problem = self._problem(ctx, cwd, tokens)
                    if problem:
                        findings.append(self.finding(path, f"`{part}` cannot work here: {problem}", number))
        return findings


class PlaceholderContacts(Rule):
    id = "docs.placeholders"
    intents = frozenset({"sync_docs"})
    summary = "Docs and config must not ship placeholder contacts"
    hint = "Replace with a real, monitored address or remove the line."

    PATTERN = re.compile(r"[\w.+-]+@example\.(?:com|org|net)|https?://(?:www\.)?example\.(?:com|org|net)\S*")

    def check(self, ctx: RepoContext):
        findings = []
        for path in ctx.match("*.md", "*.yml", "*.yaml"):
            for number, line in enumerate((ctx.read(path) or "").splitlines(), 1):
                for hit in dict.fromkeys(self.PATTERN.findall(line)):
                    findings.append(self.finding(path, f"placeholder `{hit}`", number))
        return findings


class DuplicatePolicies(Rule):
    id = "docs.duplicate-policies"
    intents = frozenset({"sync_docs"})
    summary = "Community files present in several locations drift apart; GitHub displays only one"
    hint = "Merge the copies into one location and delete the others."

    NAMES = ("SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SUPPORT.md")

    def check(self, ctx: RepoContext):
        findings = []
        for name in self.NAMES:
            copies = [p for p in (f".github/{name}", name, f"docs/{name}") if ctx.exists(p)]
            texts = {ctx.read(p) for p in copies}
            if len(copies) > 1 and len(texts) > 1:
                findings.append(self.finding(copies[0], f"{name} exists in {', '.join(copies)} with different "
                                                        "content; keep one"))
        return findings


RULES = [LicenseMismatch(), CloneUrl(), BrokenLinks(), PhantomCommands(), PlaceholderContacts(),
         DuplicatePolicies()]
