# Changelog

## 2.0.0

- **Optional local AI** (`agent chat`, `ask <question>` in the interactive mode, a chat pane in `agent tui`): a model
  running in llama.cpp's `llama-server` on your machine explains findings and failing checks and proposes edits.
  - The model can only read, search, list findings, run checks and propose edits (exact snippet replacement,
    syntax-validated). No write, apply, commit or shell tool exists; replies are schema-constrained and validated.
  - Proposals are pending diffs labelled `ai.proposal`; you apply them through the existing path, which verifies
    against a recorded baseline and reverts on regression. Commit messages say which edits came from the model.
  - The engine starts only when used, on 127.0.0.1 with `--offline` and a clean environment, and stops with
    cloudy. Settings live only in the user config; `[ai]` in a repository is refused.
  - `python -m cloudy.ai.bench` measures load time, time to first token, tokens/s and RAM on your machine.
- Without `[ai]` configured, cloudy behaves exactly as 1.1; the core never imports the AI package.

## 1.1.0

- **Terminal UI:** `agent tui` (optional extra `cloudy[tui]`, Textual): findings with hints, checks, pending diffs,
  git status and an activity log; y/n confirmation before apply, revert and commit; work runs in a background
  worker so the first frame appears immediately. See `docs/TUI.md`.
- **Workbench:** the interactive mode's session bookkeeping moved to `cloudy/workbench.py`, shared by the
  interactive mode and the TUI. Behaviour of the interactive mode is unchanged.
- The CLI's startup and the core's dependencies are unchanged; Textual is imported only by `agent tui`.

## 1.0.0

First stable release of `cloudy`, an offline, deterministic coding agent (no language model, no network calls).

- **Agent loop:** Explorer, Planner, Editor, Executor and Verifier with explore → plan → edit → verify cycles,
  no-progress detection and automatic revert of a cycle that breaks a previously passing check.
- **Safe by default:** plan + diffs only; `--apply` writes atomically with backups; JSON session logs outside the
  repository; files created by the project's own tools reported separately (optionally removed).
- **Languages:** Python, JavaScript/TypeScript (npm, pnpm, yarn, bun), Go, Rust, Ruby, C/C++; JS/TS, Go and Rust
  monorepos handled per project directory.
- **Fixes via the project's own tools:** ruff, eslint, prettier, gofmt, rustfmt, rubocop (safe autocorrect),
  clang-format; plus deterministic CI, Dependabot, CODEOWNERS, README/license and clone-URL rules.
- **Python checks in the project's environment:** pytest and mypy run as `python -m` with the repository's
  virtualenv (`.venv`, `venv`, `env`) or `python3`, never with an isolated tool install that lacks the project's
  dependencies.
- **Reports for humans:** failing checks and ambiguous findings with `file:line` and a hint.
- **Interactive mode:** `plan`, `diff`, `apply` (exactly the shown diffs), `status`, `revert`, `commit`.
- **Git helpers:** stage/commit only cloudy's own edits with a deterministic message; never push; destructive git
  commands blocked.
- **Plugins:** rules, languages, tools and checks from `~/.config/cloudy/rules/`; repository plugins only with
  `--trust-repo-rules`.
- **Configuration:** `cloudy.toml` / `[tool.cloudy]` for timeouts, cycles, rule groups, tool paths, ignore
  patterns, extra checks and denied commands.
