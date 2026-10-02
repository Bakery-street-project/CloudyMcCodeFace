# cloudy

[![CI](https://github.com/Bakery-street-project/CloudyMcCodeFace/actions/workflows/ci.yml/badge.svg)](https://github.com/Bakery-street-project/CloudyMcCodeFace/actions/workflows/ci.yml)

**An offline, deterministic coding agent.** cloudy explores a repository, plans small fixes, shows you the diffs,
applies them only when you say so, re-runs the project's own checks and tells you exactly what is left for a human.
It is part of the CloudyMcCodeFace project.

- **What it is:** rules, parsers and the tools your project already uses (ruff, eslint, prettier, gofmt, rustfmt,
  rubocop, clang-format, pytest, go test, cargo test, …) in a plan → edit → verify loop. Same repo + same task =
  same result.
- **What it is not:** the core has no language model and no network call. It never invents code or guesses at
  logic. A failing test is located (`file:line`, expected vs actual) and handed to you, not "fixed".
- **Optional local AI (2.0):** a model running on your own machine can explain problems and *propose* diffs; you
  approve every one, and cloudy verifies it. Off unless you configure it; nothing leaves your machine.
- **Light:** starts in ~0.1 s, ~25 MB RAM of its own, no GPU, no daemon.

## Quick start

Requires Python 3.11+ and git.

```bash
pip install .                                   # from a checkout; adds the `agent` command
agent "analyze this repo"                       # what's here: languages, tools, CI, observations
agent "fix the CI, formatting and lint"         # safe mode: plan + diffs, nothing is written
agent --apply "fix the CI, formatting and lint" # write, re-verify, undo anything that breaks a passing check
```

Everything runs offline once installed; package managers are forced offline too. Exit codes: `0` done/clean/
proposed, `1` needs a human, `2` usage or config error.

## What gets fixed, and what gets reported

A rule fixes something only when the correct change is unambiguous, and always with the project's own tool and
config. Everything else is reported with its location and a hint.

| Area | Fixed automatically | Reported for you |
|---|---|---|
| CI / repo | `\|\| true` masking check steps, missing `permissions:`, invalid Dependabot entries, malformed CODEOWNERS | masked non-check commands, `continue-on-error`, workflows needing write access |
| Docs | README license vs LICENSE, wrong `git clone` URL | broken links, commands for scripts/files that don't exist, placeholder emails, duplicate policy files |
| Python | ruff safe fixes; pytest `pythonpath` for src layouts | test failures, remaining lint |
| JavaScript / TypeScript | eslint autofix, prettier (only if configured) | tsc errors, test failures, lockfile conflicts, scripts using undeclared tools |
| Go | gofmt | go vet, test failures, missing go.mod |
| Rust | rustfmt (crate edition, rustfmt.toml) | clippy, test failures, missing `edition` |
| Ruby | rubocop safe autocorrect (only if configured) | other offenses, test failures (minitest/rspec/rake), `.ruby-version` mismatch |
| C / C++ | clang-format (only under a `.clang-format`) | ctest failures, committed build output |

JS/TS, Go and Rust monorepos are handled per project directory. Files the project's tools create during checks
(e.g. `Cargo.lock`) are listed separately from cloudy's own edits.

## Interactive mode

Run `agent` with no task:

```
cloudy> plan fix formatting and lint     # shows diffs, writes nothing
cloudy> apply                            # writes exactly those diffs, then verifies
cloudy> status                           # pending/applied files, what needs a human, git status
cloudy> revert                           # undo the last apply
cloudy> commit                           # commit cloudy's files locally
```

Also `analyze`, `diff`, `apply <task>` (plan + apply in one step), `help`, `quit`. Anything else you type is
treated as `plan <text>` and never writes.

## Terminal UI

```bash
pip install ".[tui]"      # optional extra (Textual); the CLI does not need it
agent tui
```

A full-screen view with findings (with hints), checks, pending diffs, git status and an activity log. Type the same
commands in the command bar; `apply`, `revert` and `commit` ask for confirmation (y/n) first, and the screen stays
responsive while checks run. Details: [docs/TUI.md](docs/TUI.md).

## Local AI (optional)

Point cloudy at llama.cpp's `llama-server` and a GGUF model in your user config, then:

```bash
agent chat                 # free text goes to the local model; `diff` / `apply` / `revert` as usual
agent tui                  # adds a chat pane: ask <question>
```

The model can read, search and *propose* edits — never write, apply, commit or run commands. Proposals are pending
diffs you review; `apply` verifies them and reverts on regression. The engine runs offline on 127.0.0.1 and only
while you use it. Setup, recommended models for your RAM and a benchmark: [docs/LOCAL_AI.md](docs/LOCAL_AI.md).

## Git

`agent --apply --stage "…"` or `--apply --commit "…"` (and `commit` in interactive mode) stage only the files
cloudy changed and create one local commit with a deterministic message. Files that already had your uncommitted
changes are never staged, a commit is refused while anything else is staged, and cloudy never amends, skips hooks
or pushes. Destructive git commands (push, reset, rebase, clean, checkout --, …) are blocked outright.

## Configuration (optional)

`cloudy.toml`, `.cloudy.toml` or `[tool.cloudy]` in `pyproject.toml`:

```toml
timeout = 300                       # seconds per command
max_cycles = 3                      # fix → verify iterations in --apply mode
disabled_groups = ["docs"]          # ci, repo, docs, python, js, go, rust, ruby, cpp
disabled_rules = ["ci.workflow-permissions"]
ignore = ["vendor/*", "*.min.js"]   # never analysed or edited
checks = { e2e = "make e2e" }       # extra checks to run
deny = ['\bterraform apply\b']      # extra commands that must never run
clean_tool_files = false            # true: delete new untracked files the tools created

[tools]                             # explicit executables instead of PATH lookup
ruff = "~/.local/bin/ruff"
```

Session logs and backups go to `~/.local/state/cloudy/`, never into your repository.

## Plugins

Add a rule by dropping a Python module into `~/.config/cloudy/rules/`:

```python
from cloudy.rules import Rule


class NoTabs(Rule):
    id = "style.no-tabs"
    intents = frozenset({"fix_lint"})
    summary = "Indent Python files with spaces, not tabs"

    def check(self, ctx):
        return [self.finding(f, "tab indentation", fixable=True)
                for f in ctx.match("*.py") if "\t" in (ctx.read(f) or "")]

    def fix(self, ctx):
        return {f.path: ctx.read(f.path).replace("\t", "    ") for f in self.check(ctx)}


RULES = [NoTabs()]
```

Plugins can also declare languages, tools and checks — the built-in Ruby and C/C++ support is written exactly
this way. Plugins inside a repository (`.cloudy/rules/`) load only with `--trust-repo-rules`, because loading
them runs their code. The full interface is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#extending-without-models).

## Development

```bash
pip install -e ".[dev]"
ruff check . && bandit -r src tests -ll && pytest
```

Design, module responsibilities and the complete rule catalogue: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Release notes: [CHANGELOG.md](CHANGELOG.md).

## License

Proprietary — all rights reserved. See [LICENSE](LICENSE).

## Security

See [SECURITY.md](SECURITY.md).
