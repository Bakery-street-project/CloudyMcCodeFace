"""Runs llama.cpp's `llama-server` as a child process: loopback only, offline, started lazily, always stopped."""

from __future__ import annotations

import http.client
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from ..executor import build_env
from .config import is_loopback

GB = 1024 ** 3


class EngineError(Exception):
    pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def health(base_url: str, timeout: float = 2.0) -> int | None:
    """HTTP status of GET /health (200 ready, 503 loading), or None when nothing answers."""
    parsed = urlparse(base_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    try:
        connection.request("GET", "/health")
        return connection.getresponse().status
    except OSError:
        return None
    finally:
        connection.close()


class Engine:
    """One llama-server process for one model. Use as a context manager or call start()/stop()."""

    def __init__(self, config: dict, *, deep: bool = False, log_path: Path | None = None) -> None:
        self.config = config
        self.model = config["deep_model"] if deep else config["model"]
        if deep and not self.model and not config["endpoint"]:
            raise EngineError("no deep_model configured in [ai]")
        self.log_path = log_path
        self.process: subprocess.Popen | None = None
        self.base_url: str | None = None

    @property
    def model_name(self) -> str:
        if self.config["endpoint"]:
            return f"llama-server at {urlparse(self.config['endpoint']).netloc}"
        return Path(self.model).name

    def argv(self, port: int) -> list[str]:
        """The exact command line. Loopback, offline, one slot; never a download option (-hf, --model-url)."""
        argv = [self.config["engine"], "-m", self.model, "--host", "127.0.0.1", "--port", str(port),
                "-c", str(self.config["context"]), "-ngl", str(self.config["gpu_layers"]), "-np", "1", "--offline"]
        if self.config["threads"]:
            argv += ["-t", str(self.config["threads"])]
        return argv

    def start(self) -> str:
        """Start (or attach to the configured loopback endpoint) and wait until the model is loaded."""
        if self.base_url:
            return self.base_url
        if self.config["endpoint"]:  # attach to an already running local server
            if not is_loopback(self.config["endpoint"]):
                raise EngineError("refusing a non-loopback endpoint")
            if health(self.config["endpoint"]) != 200:
                raise EngineError(f"no ready llama-server at {self.config['endpoint']}")
            self.base_url = self.config["endpoint"].rstrip("/")
            return self.base_url
        engine, model = Path(self.config["engine"]), Path(self.model)
        if not engine.is_file() or not os.access(engine, os.X_OK):
            raise EngineError(f"llama-server not found or not executable: {engine}")
        if not model.is_file():
            raise EngineError(f"model file not found: {model} (cloudy never downloads models; place the .gguf there)")
        size = model.stat().st_size
        if size > self.config["max_model_gb"] * GB:
            raise EngineError(f"{model.name} is {size / GB:.1f} GB, above max_model_gb = {self.config['max_model_gb']}")
        port = free_port()
        log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL  # noqa: SIM115 - closed in stop()
        # A clean environment: no LLAMA_ARG_* or HF_TOKEN from the caller can turn on downloads or change flags.
        self.process = subprocess.Popen(  # nosec B603 - argv built above from validated user config
            self.argv(port), env=build_env(), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0)
        self._log = log
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + self.config["load_timeout"]
        while time.monotonic() < deadline:
            code = self.process.poll()
            if code is not None:
                self.stop()
                raise EngineError(f"llama-server exited with code {code} while loading "
                                  f"{model.name}" + (f"; see {self.log_path}" if self.log_path else ""))
            if health(url) == 200:
                self.base_url = url
                return url
            time.sleep(0.2)
        self.stop()
        raise EngineError(f"{model.name} did not load within {self.config['load_timeout']:g} s")

    def stop(self) -> None:
        process, self.process, self.base_url = self.process, None, None
        if process is not None and process.poll() is None:
            try:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                process.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError):
                process.kill()
                process.wait(timeout=5)
        log = getattr(self, "_log", None)
        if log not in (None, subprocess.DEVNULL):
            log.close()
        self._log = None

    def peak_rss_mb(self) -> int | None:
        """Peak memory of the engine process (Linux); None where it cannot be read."""
        if self.process is None:
            return None
        try:
            with open(f"/proc/{self.process.pid}/status", encoding="utf-8") as status:
                return next(int(line.split()[1]) // 1024 for line in status if line.startswith("VmHWM"))
        except (OSError, StopIteration, ValueError):
            return None

    def __enter__(self) -> Engine:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
