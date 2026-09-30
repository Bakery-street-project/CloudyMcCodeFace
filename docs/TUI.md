# TUI

`agent tui` (or `agent --tui`) opens a full-screen terminal UI. It needs the optional Textual extra:

```bash
pip install "cloudy[tui]"
```

Without the extra, `agent tui` prints that install hint and exits with code 2; every other command works as before.
Textual is imported only when the TUI starts, so the CLI's startup time does not change.

## Layout

```
┌ cloudy — <repo> — <mode>: <status> · <n> pending edit(s) ──────────────────────────────────┐
│ ┌ Findings ────────────────────────────┐ ┌ Pending diffs (n) ─────────────────────────────┐ │
│ │ Location │ Rule │ Kind │ Message → hint│ │ path (rule)                                    │ │
│ └──────────────────────────────────────┘ │ unified diff                                   │ │
│ ┌ Checks ──────────────────────────────┐ └────────────────────────────────────────────────┘ │
│ │ Status │ Check │ Where │ Summary      │ ┌ git status ────────────────────────────────────┐ │
│ └──────────────────────────────────────┘ │ porcelain lines · applied this session          │ │
│                                          └────────────────────────────────────────────────┘ │
│ ┌ Activity ───────────────────────────────────────────────────────────────────────────────┐ │
│ │ progress from the engine, command results, cancellations                               │ │
│ └─────────────────────────────────────────────────────────────────────────────────────────┘ │
│ > command bar                                                                              │
│ ^q Quit  F1 Help  F5 Refresh  Esc Command bar                                              │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

A hidden `#chat` slot is reserved to the right of the main panels for the optional local-AI chat (v2.0).

## Commands and keys

The command bar accepts the same commands as the interactive mode: `analyze`, `plan <task>`, `diff`, `apply`,
`apply <task>`, `status`, `revert`, `commit`, `help`, `quit`. Anything else is treated as `plan <text>` and never
writes.

| Key | Action |
|---|---|
| Enter | run the command in the command bar |
| Ctrl+Q | quit |
| F1 | help in the activity log |
| F5 | refresh (`status`) |
| Esc | focus the command bar |
| y / n (Esc) | answer a confirmation |

## State machine

`cloudy/tui/state.py` holds all behaviour and does not import Textual:

```
idle ──submit(read-only)──► busy ──execute──► idle
idle ──submit(writes)─────► confirming ──y──► busy ──execute──► idle
                                       └─n──► idle   ("Cancelled … Nothing was written.")
```

- **Read-only** commands (`analyze`, `plan`, `diff`, `status`) run immediately.
- **Writing** commands (`apply`, `apply <task>`, `revert`, `commit`) first check their precondition (something
  pending / something applied) and are refused with a message if it fails. Otherwise they ask a yes/no question
  that says exactly what will be written.
- While a command runs or a question is open, new commands are refused ("Busy"). `quit` and `help` always work.

## Mapping to the engine

The TUI is a view. Session bookkeeping lives in `cloudy/workbench.py`, shared with the interactive mode, and all
work is done by the unchanged Orchestrator.

| Command | Workbench | Orchestrator |
|---|---|---|
| `analyze` | `analyze()` | `run("analyze")` |
| `plan <task>` | `plan(task)` | `run(task)` in safe mode |
| `apply` | `apply_pending()` | `apply_edits(plan)` |
| `apply <task>` | `apply_task(task)` | `run(task)` in apply mode |
| `revert` | `revert_last()` | `revert(session)` |
| `commit` | `commit_last()` | `commit_session(session)` |
| `status`, `diff` | `git_status()`, `pending_edits()` | — |

Commands run in one exclusive background worker (`@work(thread=True, exclusive=True)`); engine progress reaches
the activity log through an `EventReporter` via `call_from_thread`. The first frame is drawn before any work
starts; `analyze` then runs in the background.

## Tests

- `tests/test_tui_state.py`: parsing, preconditions, confirm/cancel, busy, apply → commit → revert, view contents.
  No terminal needed.
- `tests/test_tui_app.py`: headless Textual runs (`App.run_test`) of first frame + background analysis, the full
  plan → confirm → apply → revert flow, markup safety for bracketed text, and the install hint without Textual.
  Skipped when Textual is not installed.
