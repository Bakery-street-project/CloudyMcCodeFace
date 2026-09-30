"""Editor agent: proposes whole-file edits, validates syntax, shows diffs, applies atomically with backups.

Pending (unapplied) edits are kept in an overlay so later rules build on earlier proposals in dry-run mode.
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
import tomllib
from pathlib import Path

from .models import Edit

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None


class EditError(Exception):
    """An edit could not be proposed, applied or reverted safely."""


def validate(path: str, text: str) -> str | None:
    """Return a syntax error message for known structured formats, or None when the text parses."""
    suffix = Path(path).suffix.lower()
    try:
        if suffix in (".py", ".pyi"):
            ast.parse(text, filename=path)
        elif suffix == ".json":
            json.loads(text)
        elif suffix == ".toml":
            tomllib.loads(text)
        elif suffix in (".yml", ".yaml") and yaml is not None:
            list(yaml.safe_load_all(text))
    except SyntaxError as exc:
        return f"line {exc.lineno}: {exc.msg}"
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        return str(exc).splitlines()[0]
    except Exception as exc:  # yaml.YAMLError without importing yaml types
        if yaml is not None and isinstance(exc, yaml.YAMLError):
            return str(exc).replace("\n", " ")
        raise
    return None


class Editor:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._overlay: dict[str, str] = {}

    def resolve(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root):
            raise EditError(f"path outside repository: {rel}")
        return path

    def read(self, rel: str) -> str | None:
        if rel in self._overlay:
            return self._overlay[rel]
        return self._read_disk(rel)

    def _read_disk(self, rel: str) -> str | None:
        path = self.resolve(rel)
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8", newline="") as handle:
                return handle.read()
        except (UnicodeDecodeError, OSError):
            return None

    def propose(self, rel: str, after: str, *, rule: str, reason: str) -> Edit | None:
        before = self.read(rel)
        if before is None:
            raise EditError(f"{rel}: not a readable text file")
        if before == after:
            return None
        before_error = validate(rel, before)
        error = validate(rel, after)
        if error and not before_error:
            raise EditError(f"{rel}: edit would break syntax ({error})")
        self._overlay[rel] = after
        return Edit(rel, before, after, rule, reason)

    def apply(self, edit: Edit, backup_dir: Path | None = None) -> None:
        current = self._read_disk(edit.path)
        if current != edit.before:
            raise EditError(f"{edit.path}: file changed since the edit was proposed")
        if backup_dir is not None:
            backup = backup_dir / edit.path
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                _write(backup, edit.before)
        _write(self.resolve(edit.path), edit.after)
        self._overlay.pop(edit.path, None)
        edit.applied = True

    def revert(self, edit: Edit) -> None:
        if not edit.applied:
            self._overlay.pop(edit.path, None)
            return
        if self._read_disk(edit.path) != edit.after:
            raise EditError(f"{edit.path}: changed after the edit was applied; not reverting")
        _write(self.resolve(edit.path), edit.before)
        edit.applied = False


def _write(path: Path, text: str) -> None:
    """Atomic write that keeps the original file mode and newline style."""
    mode = path.stat().st_mode if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
