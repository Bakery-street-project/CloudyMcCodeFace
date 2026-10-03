"""`[ai]` settings from the user's config file. Absent file or table means local AI is simply not configured."""

from __future__ import annotations

import tomllib
from pathlib import Path
from urllib.parse import urlparse

from ..plugins import user_rules_dir

AI_DEFAULTS: dict = {
    "engine": "",  # path to llama.cpp's llama-server executable
    "model": "",  # path to the default GGUF model (e.g. a 7B coding model, Q5_K_M)
    "deep_model": "",  # optional larger model for `--deep` (e.g. 14B, Q4_K_M)
    "endpoint": "",  # optional: an already running llama-server on loopback, e.g. http://127.0.0.1:8080
    "context": 8192,  # context window in tokens
    "gpu_layers": "auto",  # llama-server -ngl: an integer, "auto" or "all"
    "cpu_moe_layers": 0,  # llama-server -ncmoe: keep this many MoE expert layers on CPU; 0 = let the engine place them
    "threads": 0,  # CPU threads; 0 lets the engine decide
    "seed": 42,  # fixed seed; temperature is always 0
    "max_steps": 6,  # tool calls per question
    "max_tokens": 1024,  # tokens per model reply
    "load_timeout": 180.0,  # seconds to wait for the model to load
    "request_timeout": 300.0,  # seconds per model reply
    "max_model_gb": 16.0,  # refuse model files larger than this
}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class AIConfigError(Exception):
    pass


def ai_config_path() -> Path:
    return user_rules_dir().parent / "config.toml"


def is_loopback(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS and not parsed.username


def load_ai_config(path: Path | None = None) -> dict | None:
    """The validated `[ai]` table, or None when local AI is not configured."""
    path = path or ai_config_path()
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")).get("ai")
    except tomllib.TOMLDecodeError as exc:
        raise AIConfigError(f"{path}: invalid TOML: {exc}") from exc
    if data is None:
        return None
    if not isinstance(data, dict):
        raise AIConfigError(f"{path}: [ai] must be a table")
    unknown = sorted(set(data) - set(AI_DEFAULTS))
    if unknown:
        raise AIConfigError(f"{path}: unknown [ai] key(s) {', '.join(unknown)}; allowed: {', '.join(AI_DEFAULTS)}")
    config = AI_DEFAULTS | data

    def fail(message: str) -> None:
        raise AIConfigError(f"{path} [ai]: {message}")

    for key in ("engine", "model", "deep_model", "endpoint"):
        if not isinstance(config[key], str):
            fail(f"{key} must be a string")
    for key, low in (
        ("context", 512), ("threads", 0), ("cpu_moe_layers", 0), ("seed", 0),
        ("max_steps", 1), ("max_tokens", 16),
    ):
        if not isinstance(config[key], int) or isinstance(config[key], bool) or config[key] < low:
            fail(f"{key} must be an integer ≥ {low}")
    for key in ("load_timeout", "request_timeout", "max_model_gb"):
        if not isinstance(config[key], (int, float)) or isinstance(config[key], bool) or config[key] <= 0:
            fail(f"{key} must be a positive number")
    layers = config["gpu_layers"]
    if not (layers in ("auto", "all") or (isinstance(layers, int) and not isinstance(layers, bool) and layers >= 0)):
        fail('gpu_layers must be an integer ≥ 0, "auto" or "all"')
    if config["endpoint"] and not is_loopback(config["endpoint"]):
        fail("endpoint must be http://127.0.0.1, http://localhost or http://[::1] — cloudy never uses remote models")
    if not config["endpoint"] and not (config["engine"] and config["model"]):
        fail("set engine and model (or endpoint for an already running local llama-server)")
    for key in ("engine", "model", "deep_model"):
        if config[key]:
            config[key] = str(Path(config[key]).expanduser())
    return config
