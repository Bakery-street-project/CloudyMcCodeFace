import shutil
import subprocess
from pathlib import Path

import pytest

from cloudy.editor import Editor
from cloudy.executor import Executor
from cloudy.explorer import explore
from cloudy.plugins import load_registry
from cloudy.rules import RepoContext

REMOTE = "https://github.com/Example-Org/Widget"

# The config/docs-only repository state this project started from (trimmed).
LEGACY_REPO = {
    ".github/workflows/ci.yml": """name: CI
on:
  push:
    branches: [main]
jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install dev tools
        run: pip install ruff pytest bandit || true
      - name: Lint (ruff)
        run: ruff check . || true
      - name: Tests (pytest)
        run: pytest --tb=short || true
""",
    ".github/dependabot.yml": """version: 2
updates:
  - package-ecosystem: "javascript"
    directory: "/"
    schedule:
      interval: "weekly"
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
""",
    ".github/CODEOWNERS": "# owners\nBoozeLee",
    "LICENSE": "PROPRIETARY LICENSE\nCopyright (c) 2026 Example\nALL RIGHTS RESERVED\n",
    "README.md": """# Widget

## Quick Start

```bash
git clone https://github.com/example-org/Widget.cd Widget
cd Widget
npm install
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file.

## Security

Mail security@example.com. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
""",
}


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def git_init(root: Path, remote: str | None = REMOTE) -> None:
    if not shutil.which("git"):
        pytest.skip("git not installed")
    run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    if remote:
        run("remote", "add", "origin", remote)
    run("add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "chore: init")


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path, monkeypatch):
    """Never load plugins from the developer's real ~/.config/cloudy during tests."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


@pytest.fixture
def make_repo(tmp_path):
    def factory(files: dict[str, str], git: bool = True, remote: str | None = REMOTE) -> Path:
        root = tmp_path / "repo"
        root.mkdir()
        write_files(root, files)
        if git:
            git_init(root, remote)
        return root
    return factory


@pytest.fixture
def context():
    def factory(root: Path) -> RepoContext:
        executor = Executor(root, timeout=60)
        return RepoContext(root, explore(root, executor, load_registry(root)), Editor(root), executor)
    return factory
