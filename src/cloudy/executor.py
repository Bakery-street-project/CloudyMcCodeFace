"""Executor agent: runs commands inside the repository with a deny-list, timeouts and an offline environment.

The deny-list is a guard rail against obviously destructive commands, not a sandbox. The agent only runs
commands it derived from the repository's own configuration or from the user's config file.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .models import CommandResult

DEFAULT_DENY = (
    r"\bsudo\b",
    r"\bsu\s+-?\w*\s*$",
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b",
    r"\brm\s+-\w*[rf]\w*\s+(/|~|\$HOME)(\s|$)",
    r"\b(curl|wget)\b[^|]*\|\s*(ba|z)?sh\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r">\s*/dev/(sd|nvme|disk)",
)

ENV_KEEP = (
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "SHELL",
    "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "GOPATH", "GOCACHE", "GOROOT", "CARGO_HOME",
    "RUSTUP_HOME", "NODE_PATH", "SSL_CERT_FILE", "SYSTEMROOT",
)

# Keep package managers from reaching the network; everything must work from what is installed.
OFFLINE_ENV = {
    "GOPROXY": "off",
    "CARGO_NET_OFFLINE": "true",
    "PIP_NO_INDEX": "1",
    "npm_config_offline": "true",
    "PYTHONDONTWRITEBYTECODE": "1",
    "NO_COLOR": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
}


class CommandBlocked(Exception):
    """Raised when a command matches the deny-list."""


def build_env(passthrough: Sequence[str] = ()) -> dict[str, str]:
    env = {key: os.environ[key] for key in (*ENV_KEEP, *passthrough) if key in os.environ}
    env.update(OFFLINE_ENV)
    return env


class Executor:
    def __init__(
        self,
        root: str | Path,
        *,
        timeout: float = 300.0,
        max_output: int = 40_000,
        deny: Sequence[str] = (),
        env_passthrough: Sequence[str] = (),
        on_result: Callable[[CommandResult], None] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.max_output = max_output
        self.on_result = on_result
        self._deny = [re.compile(p) for p in (*DEFAULT_DENY, *deny)]
        self._env = build_env(env_passthrough)

    def which(self, name: str) -> str | None:
        local = self.root / "node_modules" / ".bin" / name
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
        return shutil.which(name, path=self._env.get("PATH"))

    def ensure_allowed(self, command: str) -> None:
        for pattern in self._deny:
            if pattern.search(command):
                raise CommandBlocked(f"blocked by policy /{pattern.pattern}/: {command}")

    def run(
        self,
        cmd: str | Sequence[str],
        *,
        timeout: float | None = None,
        cwd: str | Path | None = None,
        stdin: str | None = None,
    ) -> CommandResult:
        """Run an argv list directly, or a string through bash. Never raises for command failures."""
        if isinstance(cmd, str):
            shell = self.which("bash") or "/bin/sh"
            argv, display = [shell, "-c", cmd], cmd
        else:
            argv = [str(part) for part in cmd]
            display = shlex.join(argv)
        self.ensure_allowed(display)
        workdir = self._workdir(cwd)
        limit = self.timeout if timeout is None else timeout
        start = time.monotonic()
        try:
            # argv is built by the agent from repo config, never interpolated into a shell string.
            proc = subprocess.Popen(  # nosec B603
                argv,
                cwd=workdir,
                env=self._env,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except OSError as exc:
            code = 127 if isinstance(exc, FileNotFoundError) else 126
            result = CommandResult(display, code, "", f"{argv[0]}: {exc.strerror or exc}")
        else:
            timed_out = False
            try:
                out, err = proc.communicate(stdin, timeout=limit)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill(proc)
                out, err = proc.communicate()
            result = CommandResult(
                display,
                124 if timed_out else proc.returncode,
                self._clip(out),
                self._clip(err) + (f"\n[timed out after {limit:g}s]" if timed_out else ""),
                round(time.monotonic() - start, 3),
                timed_out,
            )
        if self.on_result:
            self.on_result(result)
        return result

    def _workdir(self, cwd: str | Path | None) -> Path:
        path = (self.root / cwd).resolve() if cwd else self.root
        if not path.is_relative_to(self.root):
            raise CommandBlocked(f"working directory outside repository: {path}")
        return path

    def _clip(self, text: str) -> str:
        if len(text) <= self.max_output:
            return text
        half = self.max_output // 2
        return f"{text[:half]}\n[... {len(text) - self.max_output} characters omitted ...]\n{text[-half:]}"


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError):
        proc.kill()
