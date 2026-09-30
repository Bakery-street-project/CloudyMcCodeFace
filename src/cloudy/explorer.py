"""Explorer agent: maps the repository and probes which tools actually exist. It never assumes a tool."""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from .executor import Executor

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build",
    "target", ".tox", ".nox", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".next", ".cache", "vendor",
})
MAX_FILES = 20_000

LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust",
    ".sh": "Shell", ".bash": "Shell", ".lua": "Lua", ".rb": "Ruby", ".java": "Java", ".kt": "Kotlin",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cs": "C#", ".php": "PHP",
    ".md": "Markdown", ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML", ".json": "JSON",
}
DATA_LANGUAGES = frozenset({"Markdown", "YAML", "TOML", "JSON"})

MANIFESTS = {
    "pyproject.toml": "pip", "setup.py": "pip", "setup.cfg": "pip", "Pipfile": "pip",
    "package.json": "npm", "go.mod": "gomod", "Cargo.toml": "cargo", "Gemfile": "bundler",
    "composer.json": "composer", "Dockerfile": "docker", "Makefile": "make",
}

LINTER_FILES = {
    "ruff.toml": "ruff", ".ruff.toml": "ruff", ".flake8": "flake8", "mypy.ini": "mypy", ".mypy.ini": "mypy",
    ".pylintrc": "pylint", "tsconfig.json": "tsc", ".golangci.yml": "golangci-lint",
    ".golangci.yaml": "golangci-lint", "rustfmt.toml": "rustfmt", "clippy.toml": "clippy",
    ".pre-commit-config.yaml": "pre-commit", ".shellcheckrc": "shellcheck", ".editorconfig": "editorconfig",
}
LINTER_PREFIXES = {".eslintrc": "eslint", "eslint.config.": "eslint", ".prettierrc": "prettier"}
PYPROJECT_TOOLS = ("ruff", "mypy", "black", "isort", "pylint", "pytest", "coverage", "bandit")

CI_PATTERNS = (
    ".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml", ".circleci/config.yml",
    "Jenkinsfile", "azure-pipelines.yml", ".travis.yml",
)
TEST_PATTERNS = (
    "test_*.py", "*_test.py", "*.test.js", "*.test.ts", "*.test.tsx", "*.spec.js", "*.spec.ts", "*_test.go",
)

LANG_TOOLS = {
    "Python": ("python3", "pytest", "ruff", "mypy"),
    "JavaScript": ("node", "npm", "eslint"),
    "TypeScript": ("node", "npm", "tsc", "eslint"),
    "Go": ("go",),
    "Rust": ("cargo",),
    "Shell": ("shellcheck",),
}
KNOWN_TOOLS = frozenset({
    "python3", "pytest", "ruff", "mypy", "bandit", "black", "flake8", "pylint", "isort", "node", "npm",
    "yarn", "pnpm", "eslint", "prettier", "tsc", "go", "golangci-lint", "cargo", "make", "shellcheck",
    "pre-commit",
})

LICENSES = (  # ordered: more specific texts first
    ("MIT", "permission is hereby granted, free of charge"),
    ("Apache-2.0", "apache license"),
    ("AGPL-3.0", "gnu affero general public license"),
    ("LGPL", "gnu lesser general public license"),
    ("GPL", "gnu general public license"),
    ("MPL-2.0", "mozilla public license"),
    ("BSD", "redistribution and use in source and binary forms"),
    ("ISC", "permission to use, copy, modify, and/or distribute"),
    ("Unlicense", "this is free and unencumbered software"),
    ("Proprietary", "all rights reserved"),
)
CONVENTIONAL = re.compile(r"^(feat|fix|chore|docs|ci|test|refactor|perf|build|style|revert)(\([^)]*\))?!?: ")


@dataclass
class ToolInfo:
    name: str
    path: str | None
    version: str | None = None

    @property
    def available(self) -> bool:
        return self.path is not None


@dataclass
class RepoProfile:
    root: str
    files: list[str] = field(default_factory=list, repr=False)
    languages: dict[str, int] = field(default_factory=dict)
    manifests: list[str] = field(default_factory=list)
    ecosystems: list[str] = field(default_factory=list)
    ci_files: list[str] = field(default_factory=list)
    ci_tools: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    linters: dict[str, str] = field(default_factory=dict)
    scripts: dict[str, list[str]] = field(default_factory=dict)
    tools: dict[str, ToolInfo] = field(default_factory=dict)
    license: str | None = None
    docs: list[str] = field(default_factory=list)
    git: dict = field(default_factory=dict)
    truncated: bool = False

    @property
    def code_languages(self) -> list[str]:
        return [lang for lang in self.languages if lang not in DATA_LANGUAGES]

    def has_tool(self, name: str) -> bool:
        info = self.tools.get(name)
        return bool(info and info.available)

    def tool_path(self, name: str) -> str:
        info = self.tools.get(name)
        return info.path if info and info.path else name

    def to_dict(self) -> dict:
        data = {key: value for key, value in vars(self).items() if key not in ("files", "tools")}
        data["tools"] = {name: {"path": t.path, "version": t.version} for name, t in self.tools.items()}
        data["file_count"] = len(self.files)
        return data


def walk(root: Path) -> tuple[list[str], bool]:
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        rel_dir = PurePosixPath(Path(dirpath).relative_to(root).as_posix())
        for name in sorted(filenames):
            files.append(str(rel_dir / name) if str(rel_dir) != "." else name)
            if len(files) >= MAX_FILES:
                return files, True
    return files, False


def detect_license(text: str) -> str:
    lowered = " ".join(text.lower().split())
    for name, marker in LICENSES:
        if marker in lowered:
            return name
    return "Unknown"


def _read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _scripts(root: Path, files: list[str]) -> dict[str, list[str]]:
    scripts: dict[str, list[str]] = {}
    if "package.json" in files:
        try:
            scripts["package.json"] = sorted(json.loads(_read(root, "package.json")).get("scripts", {}))
        except (json.JSONDecodeError, AttributeError):
            scripts["package.json"] = []
    if "Makefile" in files:
        targets = re.findall(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", _read(root, "Makefile"), re.M)
        scripts["Makefile"] = sorted({t for t in targets if not t.startswith(".")})
    if "pyproject.toml" in files:
        try:
            scripts["pyproject.toml"] = sorted(tomllib.loads(_read(root, "pyproject.toml")).get("project", {})
                                               .get("scripts", {}))
        except tomllib.TOMLDecodeError:
            pass
    return scripts


def _linters(root: Path, files: list[str]) -> dict[str, str]:
    linters: dict[str, str] = {}
    for rel in files:
        name = PurePosixPath(rel).name
        tool = LINTER_FILES.get(name) or next(
            (tool for prefix, tool in LINTER_PREFIXES.items() if name.startswith(prefix)), None)
        if tool and "/" not in rel:
            linters.setdefault(tool, rel)
    if "pyproject.toml" in files:
        try:
            configured = tomllib.loads(_read(root, "pyproject.toml")).get("tool", {})
        except tomllib.TOMLDecodeError:
            configured = {}
        for tool in PYPROJECT_TOOLS:
            if tool in configured:
                linters.setdefault(tool, "pyproject.toml")
    return linters


def _git(executor: Executor) -> dict:
    if not executor.which("git"):
        return {}
    inside = executor.run(["git", "rev-parse", "--is-inside-work-tree"], timeout=15)
    if not inside.ok:
        return {}
    branch = executor.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=15).stdout.strip()
    status = executor.run(["git", "status", "--porcelain"], timeout=30).stdout.splitlines()
    log = executor.run(["git", "log", "-20", "--format=%h %s"], timeout=15).stdout.splitlines()
    remote = executor.run(["git", "remote", "get-url", "origin"], timeout=15)
    subjects = [line.split(" ", 1)[-1] for line in log]
    conventional = sum(bool(CONVENTIONAL.match(s)) for s in subjects)
    return {
        "branch": branch,
        "dirty": status,
        "recent": log[:5],
        "remote": remote.stdout.strip() if remote.ok else None,
        "commit_style": "conventional commits" if subjects and conventional / len(subjects) >= 0.6 else "free-form",
    }


def probe_tool(executor: Executor, name: str) -> ToolInfo:
    path = executor.which(name)
    if not path:
        return ToolInfo(name, None)
    result = executor.run([path, "--version"], timeout=15)
    text = (result.stdout or result.stderr).strip()
    return ToolInfo(name, path, text.splitlines()[0][:80] if result.ok and text else None)


def explore(root: str | Path, executor: Executor) -> RepoProfile:
    root = Path(root).resolve()
    files, truncated = walk(root)
    profile = RepoProfile(root=str(root), files=files, truncated=truncated)

    counts = Counter(LANGUAGES[suffix] for f in files if (suffix := PurePosixPath(f).suffix.lower()) in LANGUAGES)
    profile.languages = dict(counts.most_common())
    profile.manifests = [f for f in files if PurePosixPath(f).name in MANIFESTS
                         or fnmatch(PurePosixPath(f).name, "requirements*.txt")]
    profile.ecosystems = sorted({MANIFESTS.get(PurePosixPath(f).name, "pip") for f in profile.manifests})
    profile.ci_files = [f for f in files if any(fnmatch(f, p) for p in CI_PATTERNS)]
    profile.test_files = [f for f in files if any(fnmatch(PurePosixPath(f).name, p) for p in TEST_PATTERNS)
                          or fnmatch(f, "tests/*.rs")]
    profile.docs = [f for f in files if f.lower().endswith((".md", ".rst"))]
    profile.linters = _linters(root, files)
    profile.scripts = _scripts(root, files)

    ci_text = "\n".join(_read(root, f) for f in profile.ci_files)
    profile.ci_tools = sorted(t for t in KNOWN_TOOLS if re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", ci_text))

    license_file = next((f for f in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING") if f in files), None)
    profile.license = detect_license(_read(root, license_file)) if license_file else None

    wanted = {"git"} | set(profile.linters) & KNOWN_TOOLS | set(profile.ci_tools)
    for lang in profile.code_languages:
        wanted.update(LANG_TOOLS.get(lang, ()))
    if "pip" in profile.ecosystems:
        wanted.update(LANG_TOOLS["Python"])
    if "npm" in profile.ecosystems:
        wanted.update(("node", "npm"))
    if "Makefile" in files:
        wanted.add("make")
    with ThreadPoolExecutor(max_workers=8) as pool:
        infos = pool.map(lambda name: probe_tool(executor, name), sorted(wanted))
    profile.tools = {info.name: info for info in infos}
    profile.git = _git(executor)
    return profile
