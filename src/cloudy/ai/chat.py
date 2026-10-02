"""The conversation loop: question → tool calls → answer (+ pending edit proposals). It never writes a file."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..state import Session
from ..verifier import build_checks, categories_for, run_checks
from .tools import RESPONSE_SCHEMA, TOOLS, Toolbox, ToolError, parse

INTENTS = ["fix_ci", "sync_docs", "fix_lint", "fix_tests"]  # baseline covers static, lint and test checks
HISTORY_MESSAGES = 8
MAX_INVALID_REPLIES = 2

SYSTEM_PROMPT = f"""You are the optional local assistant inside cloudy, an offline coding tool.
You help a developer understand findings and failing checks in their repository and propose small, correct fixes.

Reply with exactly one JSON object per turn: {{"tool": <name>, "args": {{...}}}}. Tools:
- read_file(path, start?, end?): numbered lines of a file (max 200 lines per call)
- search(pattern, glob?): regex search across the repository
- list_findings(): cloudy's current findings and failing checks with file:line
- run_checks(): run the project's own lint/test checks now
- propose_edit(path, find, replace, reason): propose replacing one exact snippet (it must occur exactly once).
  This only creates a pending diff; the developer reviews it and decides whether to apply it.
- answer(text): your final reply to the developer. Always finish with answer.

Rules:
- Read the relevant code before proposing an edit. Quote `find` exactly as read_file shows it (without line numbers).
- Propose the smallest change that fixes the problem. Do not refactor, rename or add features.
- If you are not sure what the intended behaviour is, do not guess: explain the problem in answer instead.
- You cannot run shell commands, write files, apply, commit or push. Tools not listed above do not exist.
- In answer, say which edits you proposed and why, in plain language.
Available tools: {", ".join(TOOLS)}."""


@dataclass
class ChatResult:
    question: str
    answer: str
    session: Session | None  # safe-mode session holding pending proposals, if any
    steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    model: str = ""
    tokens: int = 0


def _brief(tool: str, args: dict) -> str:
    shown = {k: (v if len(str(v)) <= 40 else f"{str(v)[:37]}…") for k, v in args.items() if k != "replace"}
    return f"{tool}({', '.join(f'{k}={v!r}' for k, v in shown.items())})"


class Chat:
    def __init__(self, workbench, client, *, model_name: str, ai_config: dict,
                 on_close: Callable[[], None] | None = None) -> None:
        self.workbench = workbench
        self.client = client
        self.model_name = model_name
        self.config = ai_config
        self.on_close = on_close
        self.history: list[dict] = []

    def ask(self, question: str, on_token: Callable[[str], None] | None = None,
            on_step: Callable[[str], None] | None = None) -> ChatResult:
        wb = self.workbench
        session = Session(f"ask: {question}", wb.root, "safe", wb.state_dir)
        session.data["intents"] = INTENTS
        session.data["ai"] = {"model": self.model_name, "temperature": 0, "seed": self.config["seed"],
                              "context": self.config["context"], "gpu_layers": self.config["gpu_layers"],
                              "max_steps": self.config["max_steps"]}
        orchestrator = wb.orchestrator()
        ws = orchestrator.workspace(session, INTENTS)
        manual, failing = wb.needs_human()
        last_findings = wb.last.data["findings"] if wb.last else manual

        def checks_now() -> list[dict]:
            verification = run_checks(build_checks(ws.profile, wb.config["checks"], wb.registry), ws.executor,
                                      ws.profile, ws.editor.read, categories_for(set(INTENTS)))
            if not session.data["verifications"] and not toolbox.proposals:
                session.record_verification("baseline", verification.checks)  # disk state: reuse as baseline
            return [{"name": c.name, "status": c.status, "summary": c.summary, "details": c.details}
                    for c in verification.checks]

        toolbox = Toolbox(ws.profile, ws.editor, list(last_findings), list(failing), checks_now, self.model_name)
        languages = ", ".join(ws.profile.code_languages) or "none detected"
        context = (f"Repository languages: {languages}. Known findings: {len(last_findings)}; failing checks: "
                   f"{', '.join(c['name'] for c in failing) or 'none known yet'}.")
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *self.history,
                    {"role": "user", "content": f"{question}\n\n({context})"}]
        result = ChatResult(question, "", None, model=self.model_name)
        invalid = 0
        for _ in range(self.config["max_steps"]):
            completion = self.client.complete(messages, RESPONSE_SCHEMA, on_token)
            result.tokens += completion.chunks
            messages.append({"role": "assistant", "content": completion.text})
            try:
                tool, args = parse(completion.text)
            except ToolError as exc:
                result.errors.append(str(exc))
                invalid += 1
                if invalid >= MAX_INVALID_REPLIES:
                    result.answer = "The model did not produce a valid reply; nothing was changed."
                    break
                messages.append({"role": "user", "content": f"Error: {exc}"})
                continue
            step = _brief(tool, args)
            result.steps.append(step)
            if on_step:
                on_step(step)
            if tool == "answer":
                result.answer = args["text"]
                break
            try:
                output = toolbox.run(tool, args)
            except ToolError as exc:
                result.errors.append(str(exc))
                output = f"Error: {exc}"
            messages.append({"role": "user", "content": f"Result of {tool}:\n{output}"})
        else:
            result.answer = f"Stopped after {self.config['max_steps']} tool calls without a final answer."

        if toolbox.proposals:
            session.edits = list(toolbox.proposals)
            if not session.data["verifications"]:
                orchestrator.baseline(ws, session, INTENTS)
            session.finish("proposed", f"{len(session.edits)} edit(s) proposed by {self.model_name}; review them "
                                       "with diff, then apply")
            result.session = session
        else:
            session.finish("answered", "no edits proposed")
        session.data["ai"]["steps"], session.data["ai"]["errors"] = result.steps, result.errors
        session.data["ai"]["answer"] = result.answer
        session.save()
        self.history = (self.history + [{"role": "user", "content": question},
                                        {"role": "assistant", "content": result.answer}])[-HISTORY_MESSAGES:]
        return result

    def close(self) -> None:
        if self.on_close:
            self.on_close()
            self.on_close = None
