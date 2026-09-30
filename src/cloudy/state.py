"""Session memory: one JSON file per run plus file backups, stored outside the repository."""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict
from pathlib import Path

from .models import CommandResult, Edit, Finding, sha256


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or (os.environ.get("LOCALAPPDATA") if os.name == "nt" else None)
    return (Path(base) if base else Path.home() / ".local" / "state") / "cloudy"


class Session:
    def __init__(self, task: str, root: Path, mode: str, state_dir: Path | None = None) -> None:
        self.id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        repo_dir = (state_dir or default_state_dir()) / f"{root.name}-{sha256(str(root))[:8]}"
        self.path = repo_dir / "sessions" / f"{self.id}.json"
        self.backup_dir = repo_dir / "backups" / self.id
        self.edits: list[Edit] = []
        self.save_error: str | None = None
        self.data: dict = {
            "id": self.id, "task": task, "root": str(root), "mode": mode, "started": time.time(),
            "intents": [], "profile": None, "plan": [], "commands": [], "verifications": [], "findings": [],
            "status": "running", "message": "",
        }

    def record_command(self, result: CommandResult) -> None:
        self.data["commands"].append({"cmd": result.cmd, "exit_code": result.exit_code,
                                      "duration": result.duration, "timed_out": result.timed_out})

    def record_verification(self, label: str, checks: list) -> None:
        self.data["verifications"].append({"label": label, "checks": [asdict(c) | {"output": ""} for c in checks]})

    def set_findings(self, findings: list[Finding]) -> None:
        self.data["findings"] = [asdict(f) for f in findings]

    def finish(self, status: str, message: str) -> None:
        self.data["status"], self.data["message"] = status, message
        self.data["finished"] = time.time()

    def to_dict(self) -> dict:
        edits = [{"path": e.path, "rule": e.rule, "reason": e.reason, "applied": e.applied,
                  "before_sha256": sha256(e.before), "after_sha256": sha256(e.after), "diff": e.diff}
                 for e in self.edits]
        plan = [asdict(step) for step in self.data["plan"]]
        return self.data | {"plan": plan, "edits": edits, "session_file": str(self.path),
                            "backup_dir": str(self.backup_dir) if any(e.applied for e in self.edits) else None}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            self.save_error = f"could not save session to {self.path}: {exc}"
