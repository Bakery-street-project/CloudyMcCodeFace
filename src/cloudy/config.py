"""Optional per-repo configuration from `cloudy.toml`, `.cloudy.toml` or `[tool.cloudy]` in pyproject.toml."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "timeout": 300.0,  # seconds per command
    "max_cycles": 3,  # fix -> verify iterations in apply mode
    "deny": [],  # extra regexes for commands that must never run
    "disabled_rules": [],  # rule ids to skip
    "checks": {},  # extra checks: name = "shell command"
    "env_passthrough": [],  # extra environment variables passed to commands
}


class ConfigError(Exception):
    pass


def _source(root: Path) -> tuple[str, dict] | None:
    for name in ("cloudy.toml", ".cloudy.toml"):
        path = root / name
        if path.is_file():
            return name, tomllib.loads(path.read_text(encoding="utf-8"))
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        section = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("cloudy")
        if section is not None:
            return "pyproject.toml [tool.cloudy]", section
    return None


def load_config(root: str | Path) -> dict:
    try:
        found = _source(Path(root))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    config = {key: (value.copy() if isinstance(value, (list, dict)) else value) for key, value in DEFAULTS.items()}
    if not found:
        return config
    origin, data = found
    unknown = sorted(set(data) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"{origin}: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(DEFAULTS)}")
    config.update(data)

    def fail(message: str) -> None:
        raise ConfigError(f"{origin}: {message}")

    if not isinstance(config["timeout"], (int, float)) or isinstance(config["timeout"], bool) \
            or config["timeout"] <= 0:
        fail("timeout must be a positive number of seconds")
    if not isinstance(config["max_cycles"], int) or not 1 <= config["max_cycles"] <= 10:
        fail("max_cycles must be an integer between 1 and 10")
    for key in ("deny", "disabled_rules", "env_passthrough"):
        if not isinstance(config[key], list) or not all(isinstance(v, str) for v in config[key]):
            fail(f"{key} must be a list of strings")
    if not isinstance(config["checks"], dict) or not all(isinstance(v, str) for v in config["checks"].values()):
        fail("checks must be a table of name = \"command\"")
    for pattern in config["deny"]:
        try:
            re.compile(pattern)
        except re.error as exc:
            fail(f"deny pattern {pattern!r} is not a valid regex: {exc}")
    return config
