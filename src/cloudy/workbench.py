"""Workbench: the session bookkeeping shared by the command loop (repl.py) and the TUI (tui/).

It remembers the last plan whose edits are not written yet, the applied sessions (for revert and commit) and the
last session of any kind. All real work is delegated to the Orchestrator; nothing here reads or edits files itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .executor import Executor
from .git import Git
from .models import Edit
from .orchestrator import Orchestrator
from .plugins import Registry, load_registry
from .report import Reporter
from .state import Session, default_state_dir


class AIUnavailable(Exception):
    """Local AI is not configured (or cannot start); the message says how to set it up."""

COMMANDS = ("analyze", "plan", "ask", "diff", "apply", "status", "revert", "commit", "help", "quit")
QUIT = ("quit", "exit", ":q", "q")


@dataclass(frozen=True)
class Command:
    name: str
    argument: str = ""

    @property
    def writes(self) -> bool:
        """Commands that change files or git state (the TUI asks for confirmation first)."""
        return self.name in ("apply", "revert", "commit")


def parse(line: str) -> Command | None:
    """Parse one line of input. Unknown text becomes `plan <text>`: free text never writes."""
    text = line.strip()
    if not text:
        return None
    word, _, rest = text.partition(" ")
    word, rest = word.lower(), rest.strip()
    if word in QUIT:
        return Command("quit")
    if word == "?":
        return Command("help")
    if word in COMMANDS:
        return Command(word, rest)
    return Command("plan", text)


class Workbench:
    def __init__(self, root: str | Path, config: dict, reporter: Reporter, *, state_dir: Path | None = None,
                 registry: Registry | None = None, trust_repo_rules: bool = False, deep: bool = False,
                 chat_factory=None) -> None:
        self.root = Path(root).resolve()
        self.config = config
        self.reporter = reporter
        self.state_dir = state_dir
        self.registry = registry or load_registry(self.root, trust_repo=trust_repo_rules)
        self.pending: Session | None = None  # last plan with edits not yet written
        self.last: Session | None = None  # last session of any kind
        self.applied: list[Session] = []  # applied sessions, newest last (for revert and commit)
        self.deep = deep  # use the [ai] deep_model
        self._chat_factory = chat_factory  # tests inject a fake; otherwise the local engine is started lazily
        self._chat = None

    def orchestrator(self, apply: bool = False) -> Orchestrator:
        return Orchestrator(self.root, apply=apply, config=self.config, reporter=self.reporter,
                            state_dir=self.state_dir, registry=self.registry)

    # ---- actions -----------------------------------------------------------------------------------------------------

    def analyze(self, task: str = "") -> Session:
        return self._remember(self.orchestrator().run(task or "analyze"))

    def plan(self, task: str) -> Session:
        """Safe mode: propose edits and keep them pending. Never writes."""
        session = self._remember(self.orchestrator().run(task))
        self.pending = session if self.has_pending(session) else None
        return session

    def apply_task(self, task: str) -> Session:
        """The full plan → apply → verify loop in one step."""
        session = self._remember(self.orchestrator(apply=True).run(task))
        self._track_applied(session)
        self.pending = None
        return session

    def apply_pending(self) -> Session | None:
        """Write exactly the pending diffs, then verify. None when nothing is pending."""
        if not self.has_pending():
            return None
        session = self._remember(self.orchestrator(apply=True).apply_edits(self.pending))
        self.pending = None
        self._track_applied(session)
        return session

    def revert_last(self) -> Session | None:
        if not self.applied:
            return None
        return self._remember(self.orchestrator().revert(self.applied.pop()))

    def commit_last(self) -> dict | None:
        """Stage and commit the last applied session's files locally. Never pushes."""
        if not self.applied:
            return None
        return self.orchestrator().commit_session(self.applied[-1])

    def ask(self, question: str, on_token=None, on_step=None):
        """Ask the local model (optional extra). Proposals become pending edits; nothing is written.

        The AI package is imported only here, so the deterministic core never loads it.
        """
        if self._chat is None:
            self._chat = self._open_chat()
        result = self._chat.ask(question, on_token, on_step)
        if result.session is not None and self.has_pending(result.session):
            self.pending = result.session
        return result

    def ai_configured(self) -> bool:
        if self._chat_factory is not None:
            return True
        from .ai.config import AIConfigError, load_ai_config
        try:
            return load_ai_config() is not None
        except AIConfigError:
            return True  # configured but invalid: `ask` reports the problem

    def close(self) -> None:
        """Stop the local engine if one was started."""
        if self._chat is not None:
            self._chat.close()
            self._chat = None

    def _open_chat(self):
        if self._chat_factory is not None:
            return self._chat_factory(self)
        from .ai.chat import Chat
        from .ai.client import Client
        from .ai.config import AIConfigError, ai_config_path, load_ai_config
        from .ai.engine import Engine, EngineError
        try:
            config = load_ai_config()
        except AIConfigError as exc:
            raise AIUnavailable(str(exc)) from None
        if config is None:
            raise AIUnavailable(f"local AI is not configured: add an [ai] table to {ai_config_path()} "
                                "(see docs/LOCAL_AI.md)")
        log_dir = self.state_dir or default_state_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            engine = Engine(config, deep=self.deep, log_path=log_dir / "llama-server.log")
            url = engine.start()
        except EngineError as exc:
            raise AIUnavailable(str(exc)) from None
        client = Client(url, timeout=config["request_timeout"], seed=config["seed"], max_tokens=config["max_tokens"])
        return Chat(self, client, model_name=engine.model_name, ai_config=config, on_close=engine.stop)

    # ---- views -------------------------------------------------------------------------------------------------------

    def has_pending(self, session: Session | None = None) -> bool:
        session = session if session is not None else self.pending
        return bool(session and session.data["mode"] == "safe" and any(not e.applied for e in session.edits))

    def pending_edits(self) -> list[Edit]:
        return [e for e in self.pending.edits if not e.applied] if self.has_pending() else []

    def applied_paths(self) -> list[str]:
        return sorted({e.path for s in self.applied for e in s.edits if e.applied})

    def needs_human(self) -> tuple[list[dict], list[dict]]:
        """(manual findings, failing checks) of the last session."""
        if not self.last:
            return [], []
        manual = [f for f in self.last.data["findings"] if not f["fixable"]]
        verifications = self.last.data["verifications"] or [{"checks": []}]
        failing = [c for c in verifications[-1]["checks"] if c["status"] in ("fail", "error")]
        return manual, failing

    def git_status(self) -> list[str] | None:
        """`git status --porcelain` lines, or None outside a git repository."""
        git = Git(Executor(self.root, timeout=30))
        return git.status() if git.available() else None

    # ---- helpers -----------------------------------------------------------------------------------------------------

    def _remember(self, session: Session) -> Session:
        self.last = session
        return session

    def _track_applied(self, session: Session) -> None:
        if any(e.applied for e in session.edits):
            self.applied.append(session)
