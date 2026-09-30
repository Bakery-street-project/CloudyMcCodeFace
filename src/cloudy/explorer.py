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
    ".golangci.yaml": "golangci-lint", ".golangci.toml": "golangci-lint", "rustfmt.toml": "rustfmt",
    ".rustfmt.toml": "rustfmt", "clippy.toml": "clippy",
    ".pre-commit-config.yaml": "pre-commit", ".shellcheckrc": "shellcheck", ".editorconfig": "editorconfig",
}
LINTER_PREFIXES = {".eslintrc": "eslint", "eslint.config.": "eslint", ".prettierrc": "prettier",
                   "prettier.config.": "prettier"}
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
    "JavaScript": ("node", "npm", "eslint", "prettier"),
    "TypeScript": ("node", "npm", "tsc", "eslint", "prettier"),
    "Go": ("go", "gofmt", "golangci-lint"),
    "Rust": ("cargo", "rustfmt", "cargo-clippy"),
    "Shell": ("shellcheck",),
}
KNOWN_TOOLS = frozenset({
    "python3", "pytest", "ruff", "mypy", "bandit", "black", "flake8", "pylint", "isort", "node", "npm",
    "yarn", "pnpm", "bun", "eslint", "prettier", "tsc", "jest", "vitest", "go", "gofmt", "golangci-lint",
    "cargo", "rustfmt", "cargo-clippy", "make", "shellcheck", "pre-commit",
})
VERSION_ARGS = {"go": ("version",), "gofmt": None}  # None: no version flag; presence is enough
LOCKFILES = {"pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "bun.lockb": "bun", "bun.lock": "bun",
             "package-lock.json": "npm"}
JS_TEST_RUNNERS = ("jest", "vitest", "mocha", "ava")

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
class NodeProject:
    dir: str  # repo-relative directory of package.json; "." for the root
    package_manager: str = "npm"
    deps: list[str] = field(default_factory=list)
    scripts: dict[str, str] = field(default_factory=dict)
    linters: dict[str, str] = field(default_factory=dict)  # eslint/prettier/tsc -> repo-relative config file

    @property
    def cwd(self) -> str | None:
        return None if self.dir == "." else self.dir

    def label(self, name: str) -> str:
        return name if self.dir == "." else f"{self.dir}: {name}"


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
    node_projects: list[NodeProject] = field(default_factory=list)
    go_modules: list[str] = field(default_factory=list)
    cargo_roots: list[str] = field(default_factory=list)
    test_runners: list[str] = field(default_factory=list)
    license: str | None = None
    docs: list[str] = field(default_factory=list)
    git: dict = field(default_factory=dict)
    ignore: list[str] = field(default_factory=list)
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

    def project_tool(self, project: NodeProject, name: str, package: str | None = None) -> str | None:
        """Path to a JS tool. If package.json pins it, only the project's own install counts, never a global one."""
        for base in (Path(self.root) / project.dir, Path(self.root)):  # workspaces hoist bins to the root
            local = base / "node_modules" / ".bin" / name
            if local.is_file():
                return str(local)
        if (package or name) in project.deps:
            return None
        return self.tools[name].path if self.has_tool(name) else None

    def to_dict(self) -> dict:
        data = {key: value for key, value in vars(self).items() if key not in ("files", "tools", "node_projects")}
        data["node_projects"] = [vars(p) for p in self.node_projects]
        data["tools"] = {name: {"path": t.path, "version": t.version} for name, t in self.tools.items()}
        data["file_count"] = len(self.files)
        return data


def is_ignored(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    """fnmatch-style: `*` also matches `/`, so `generated/*` covers everything below generated/."""
    return any(fnmatch(path, pattern) or fnmatch(f"{path}/", pattern) for pattern in patterns)


def walk(root: Path, ignore: list[str] | tuple[str, ...] = ()) -> tuple[list[str], bool]:
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = PurePosixPath(Path(dirpath).relative_to(root).as_posix())
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")
                             and not is_ignored(str(rel_dir / d), ignore))
        for name in sorted(filenames):
            rel = str(rel_dir / name) if str(rel_dir) != "." else name
            if is_ignored(rel, ignore):
                continue
            files.append(rel)
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


def read_package(root: Path, rel: str = "package.json") -> dict:
    """Parsed package.json, or {} when it is missing or invalid."""
    try:
        data = json.loads((root / rel).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _package_manager(package: dict, files: list[str]) -> str:
    declared = str(package.get("packageManager", "")).split("@", 1)[0]
    if declared in ("npm", "pnpm", "yarn", "bun"):
        return declared
    return next((pm for lock, pm in LOCKFILES.items() if lock in files), "npm")


def _dirs_with(files: list[str], name: str) -> list[str]:
    return [str(PurePosixPath(f).parent) for f in files if PurePosixPath(f).name == name]


def _config_in(files: set[str], directory: str, names: tuple[str, ...], prefixes: tuple[str, ...] = (),
               inherit: bool = True) -> str | None:
    """Config file for a tool in `directory`, or (if the tool searches upward) in one of its ancestors."""
    here = PurePosixPath(directory)
    for base in (here, *here.parents) if inherit else (here,):
        for f in sorted(files):
            path = PurePosixPath(f)
            if path.parent == base and (path.name in names or path.name.startswith(prefixes)):
                return f
    return None


def _node_projects(root: Path, files: list[str]) -> list[NodeProject]:
    present = set(files)
    root_package = read_package(root)
    workspace = "workspaces" in root_package or "pnpm-workspace.yaml" in present
    projects = []
    for directory in _dirs_with(files, "package.json"):
        package = read_package(root, str(PurePosixPath(directory) / "package.json"))
        local = [str(PurePosixPath(directory) / name) for name in LOCKFILES]
        scripts = package.get("scripts", {})
        project = NodeProject(
            dir=directory,
            package_manager=_package_manager(package, [Path(p).name for p in local if p in present]
                                             or [name for name in LOCKFILES if name in present]),
            deps=sorted({name for source in ((package, root_package) if workspace else (package,))
                         for key in ("dependencies", "devDependencies")
                         if isinstance(source.get(key), dict) for name in source[key]}),
            scripts={k: str(v) for k, v in scripts.items()} if isinstance(scripts, dict) else {},
        )
        for tool, names, prefixes, key in (("eslint", (), (".eslintrc", "eslint.config."), "eslintConfig"),
                                           ("prettier", (), (".prettierrc", "prettier.config."), "prettier")):
            if key in package:
                project.linters[tool] = str(PurePosixPath(directory) / "package.json")
            elif config := _config_in(present, directory, names, prefixes):
                project.linters[tool] = config
        if config := _config_in(present, directory, ("tsconfig.json",), inherit=False):
            project.linters["tsc"] = config
        projects.append(project)
    return projects


def _test_runners(profile: RepoProfile) -> list[str]:
    runners = []
    for project in profile.node_projects:
        runners += [project.label(name) for name in JS_TEST_RUNNERS if name in project.deps]
        if "node --test" in project.scripts.get("test", ""):
            runners.append(project.label("node:test"))
    if any(f.endswith(".py") for f in profile.test_files):
        runners.append("pytest")
    runners += [f"go test ({d})" if d != "." else "go test" for d in profile.go_modules]
    runners += [f"cargo test ({d})" if d != "." else "cargo test" for d in profile.cargo_roots]
    return runners


def _scripts(root: Path, files: list[str]) -> dict[str, list[str]]:
    scripts: dict[str, list[str]] = {}
    if "package.json" in files:
        declared = read_package(root).get("scripts", {})
        scripts["package.json"] = sorted(declared) if isinstance(declared, dict) else []
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
    package = read_package(root)
    for key, tool in (("eslintConfig", "eslint"), ("prettier", "prettier")):
        if key in package:
            linters.setdefault(tool, "package.json")
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


def probe_tool(executor: Executor, name: str, version_args: dict | None = None, path: str | None = None) -> ToolInfo:
    path = path or executor.which(name)
    if not path:
        return ToolInfo(name, None)
    args = (version_args or VERSION_ARGS).get(name, ("--version",))
    if args is None:
        return ToolInfo(name, path)
    result = executor.run([path, *args], timeout=15)
    text = (result.stdout or result.stderr).strip()
    return ToolInfo(name, path, text.splitlines()[0][:80] if result.ok and text else None)


def _resolve_tool(root: Path, value: str) -> str | None:
    """A configured tool path (absolute, ~, or repo-relative) if it is an executable file, else None."""
    path = Path(value).expanduser()
    path = path if path.is_absolute() else root / path
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def explore(root: str | Path, executor: Executor, registry=None, *, ignore: list[str] | tuple[str, ...] = (),
            tool_paths: dict[str, str] | None = None) -> RepoProfile:
    """Map the repository. `registry` adds plugin languages/tools; `tool_paths` pins tools to explicit paths."""
    root = Path(root).resolve()
    languages = LANGUAGES | (registry.merged("languages") if registry else {})
    manifests = MANIFESTS | (registry.merged("manifests") if registry else {})
    lang_tools = LANG_TOOLS | (registry.merged("lang_tools") if registry else {})
    version_args = VERSION_ARGS | (registry.merged("version_args") if registry else {})
    files, truncated = walk(root, ignore)
    profile = RepoProfile(root=str(root), files=files, truncated=truncated, ignore=list(ignore))

    counts = Counter(languages[suffix] for f in files if (suffix := PurePosixPath(f).suffix.lower()) in languages)
    profile.languages = dict(counts.most_common())
    profile.manifests = [f for f in files if PurePosixPath(f).name in manifests
                         or fnmatch(PurePosixPath(f).name, "requirements*.txt")]
    profile.ecosystems = sorted({manifests.get(PurePosixPath(f).name, "pip") for f in profile.manifests})
    profile.ci_files = [f for f in files if any(fnmatch(f, p) for p in CI_PATTERNS)]
    test_patterns = TEST_PATTERNS + (registry.test_patterns() if registry else ())
    profile.test_files = [f for f in files if any(fnmatch(PurePosixPath(f).name, p) for p in test_patterns)
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
        wanted.update(lang_tools.get(lang, ()))
    if "pip" in profile.ecosystems:
        wanted.update(LANG_TOOLS["Python"])
    if "Makefile" in files:
        wanted.add("make")
    profile.node_projects = _node_projects(root, files)
    for project in profile.node_projects:
        wanted.update(("node", project.package_manager, *project.linters))
        for tool, config in project.linters.items():
            profile.linters.setdefault(tool, config)
    profile.go_modules = _dirs_with(files, "go.mod")
    cargo_dirs = _dirs_with(files, "Cargo.toml")
    profile.cargo_roots = [d for d in cargo_dirs
                           if not any(str(parent) in cargo_dirs for parent in PurePosixPath(d).parents)]
    if profile.go_modules:
        wanted.update(LANG_TOOLS["Go"])
    if profile.cargo_roots:
        wanted.update(LANG_TOOLS["Rust"])
    profile.test_runners = _test_runners(profile)
    pinned = {name: _resolve_tool(root, value) for name, value in (tool_paths or {}).items()}
    wanted.update(pinned)
    with ThreadPoolExecutor(max_workers=8) as pool:
        infos = pool.map(lambda name: probe_tool(executor, name, version_args, pinned.get(name)), sorted(wanted))
    profile.tools = {info.name: info for info in infos}
    for name, path in pinned.items():
        if path is None:
            profile.tools[name] = ToolInfo(name, None)
    profile.git = _git(executor)
    return profile
