"""Terminal UI (`agent tui`). Textual is an optional extra: `pip install "cloudy[tui]"`.

Only `app.py` imports Textual, and only when the TUI starts, so the CLI's startup time is unaffected.
"""

from __future__ import annotations

from pathlib import Path


def run(root: str | Path, config: dict, *, state_dir: Path | None = None, trust_repo_rules: bool = False) -> int:
    try:
        from .app import CloudyApp
    except ImportError as exc:
        if "textual" not in str(exc):
            raise
        print('The TUI needs Textual: pip install "cloudy[tui]"  (the `agent` CLI works without it)')
        return 2
    CloudyApp(root, config, state_dir=state_dir, trust_repo_rules=trust_repo_rules).run()
    return 0
