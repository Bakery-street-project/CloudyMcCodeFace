"""Plugin registry: built-in rule modules, the user's own plugins, and (only when trusted) repository plugins.

A plugin is a Python module with this interface (every name optional except RULES):

    RULES: list[Rule]                              rule instances; ids must be unique ("group.name")
    LANGUAGES: dict[str, str]                      file extension -> language, e.g. {".rb": "Ruby"}
    LANG_TOOLS: dict[str, tuple[str, ...]]         language -> tools the Explorer probes
    MANIFESTS: dict[str, str]                      manifest file name -> ecosystem, e.g. {"Gemfile": "bundler"}
    VERSION_ARGS: dict[str, tuple[str, ...] | None]  tool -> version arguments (None: presence is enough)
    def checks(profile: RepoProfile) -> list[Check]  verifier checks (see verifier.command_check)

Loading order: built-ins, then ~/.config/cloudy/rules/*.py, then <repo>/.cloudy/rules/*.py. Repository plugins run
code inside cloudy even in analyze mode, so they load only when the caller passes trust_repo=True
(`--trust-repo-rules`); a repository's own config can never turn that on.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from .models import sha256
from .rules import Rule

FIX_INTENTS = frozenset({"fix_ci", "sync_docs", "fix_lint", "fix_tests"})
BUILTIN_MODULES = ("ci", "repo", "docs", "python", "javascript", "go", "rust")


class PluginError(Exception):
    pass


@dataclass
class Plugin:
    name: str
    source: str  # "builtin", "user" or "repo"
    rules: list[Rule] = field(default_factory=list)
    languages: dict[str, str] = field(default_factory=dict)
    lang_tools: dict[str, tuple[str, ...]] = field(default_factory=dict)
    manifests: dict[str, str] = field(default_factory=dict)
    version_args: dict[str, tuple[str, ...] | None] = field(default_factory=dict)
    checks: Callable | None = None

    @classmethod
    def from_module(cls, name: str, source: str, module: ModuleType) -> Plugin:
        rules = getattr(module, "RULES", None)
        if not isinstance(rules, (list, tuple)):
            raise PluginError("must define RULES as a list of Rule instances")
        for rule in rules:
            if not isinstance(rule, Rule):
                raise PluginError(f"RULES contains {rule!r}, which is not a cloudy.rules.Rule instance")
            if not rule.id or "." not in rule.id:
                raise PluginError(f"{type(rule).__name__}.id must look like 'group.name'")
            if not rule.intents or not set(rule.intents) <= FIX_INTENTS:
                raise PluginError(f"{rule.id}: intents must be a non-empty subset of {sorted(FIX_INTENTS)}")
        plugin = cls(name, source, list(rules))
        for attr, target, check in (
            ("LANGUAGES", "languages", lambda k, v: k.startswith(".") and isinstance(v, str)),
            ("LANG_TOOLS", "lang_tools", lambda k, v: isinstance(v, tuple) and all(isinstance(t, str) for t in v)),
            ("MANIFESTS", "manifests", lambda k, v: isinstance(v, str)),
            ("VERSION_ARGS", "version_args", lambda k, v: v is None or isinstance(v, tuple)),
        ):
            value = getattr(module, attr, {})
            if not isinstance(value, dict) or not all(isinstance(k, str) and check(k, v) for k, v in value.items()):
                raise PluginError(f"{attr} has the wrong shape; see cloudy.plugins for the interface")
            setattr(plugin, target, dict(value))
        checks = getattr(module, "checks", None)
        if checks is not None and not callable(checks):
            raise PluginError("checks must be a function taking the RepoProfile")
        plugin.checks = checks
        return plugin


@dataclass
class Registry:
    plugins: list[Plugin] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def rules(self, disabled_rules=(), disabled_groups=()) -> list[Rule]:
        seen: dict[str, str] = {}
        rules = []
        for plugin in self.plugins:
            for rule in plugin.rules:
                if rule.id in seen:
                    self.warnings.append(f"{plugin.name}: rule {rule.id} already defined by {seen[rule.id]}; ignored")
                    continue
                seen[rule.id] = plugin.name
                if rule.id not in disabled_rules and rule.id.split(".", 1)[0] not in disabled_groups:
                    rules.append(rule)
        return rules

    def merged(self, attr: str) -> dict:
        result: dict = {}
        for plugin in self.plugins:
            result.update(getattr(plugin, attr))
        return result

    def checks(self, profile) -> list:
        found = []
        for plugin in self.plugins:
            if plugin.checks is None:
                continue
            try:
                found.extend(plugin.checks(profile))
            except Exception as exc:  # a broken plugin must not take the run down
                self.warnings.append(f"{plugin.name}: checks() failed: {type(exc).__name__}: {exc}")
        return found


def user_rules_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (os.environ.get("APPDATA") if os.name == "nt" else None)
    return (Path(base) if base else Path.home() / ".config") / "cloudy" / "rules"


def _load_file(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"cloudy_plugin_{sha256(str(path))[:12]}", path)
    if spec is None or spec.loader is None:
        raise PluginError("cannot be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def builtin_plugins() -> list[Plugin]:
    return [Plugin.from_module(name, "builtin", importlib.import_module(f"cloudy.rules.{name}"))
            for name in BUILTIN_MODULES]


def load_registry(root: str | Path, *, trust_repo: bool = False, user_dir: Path | None = None) -> Registry:
    registry = Registry(builtin_plugins())
    sources = [("user", user_dir or user_rules_dir())]
    repo_dir = Path(root) / ".cloudy" / "rules"
    repo_files = sorted(repo_dir.glob("*.py")) if repo_dir.is_dir() else []
    if trust_repo:
        sources.append(("repo", repo_dir))
    elif repo_files:
        registry.warnings.append(f"{len(repo_files)} repository rule module(s) in .cloudy/rules/ not loaded; "
                                 "pass --trust-repo-rules to run them")
    for source, directory in sources:
        for path in sorted(directory.glob("*.py")) if directory.is_dir() else []:
            if path.name.startswith("_"):
                continue
            try:
                registry.plugins.append(Plugin.from_module(f"{source}:{path.name}", source, _load_file(path)))
            except Exception as exc:  # report and continue; never crash on a third-party module
                registry.warnings.append(f"{source} plugin {path}: {type(exc).__name__}: {exc}")
    return registry
