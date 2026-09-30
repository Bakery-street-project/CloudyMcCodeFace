"""Optional local AI (cloudy 2.0): a local model proposes, a human approves, the deterministic core verifies.

- `engine.py` runs llama.cpp's `llama-server` as a separate process on 127.0.0.1, offline, only while chat is used.
- `client.py` talks to it over loopback with the standard library (streaming, schema-constrained JSON).
- `tools.py` is the only thing the model can do: read, search, list findings, run checks, propose an edit, answer.
  There is no tool that writes, applies, commits or runs a shell command.
- `chat.py` runs the conversation and turns proposals into ordinary pending edits, which the user applies through
  the existing, confirmed apply → verify → auto-revert path.

Settings live in the user's config (`~/.config/cloudy/config.toml`, table `[ai]`), never in a repository, so a
repository cannot choose which binary cloudy starts. See docs/LOCAL_AI.md.
"""

from .config import AIConfigError, ai_config_path, load_ai_config

__all__ = ["AIConfigError", "ai_config_path", "load_ai_config"]
