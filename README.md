# CloudyMcCodeFace

Privacy-First AI Coding Assistant — `cloudy`, an offline coding agent with **no language model and no network
calls**. Its "intelligence" is deterministic: repository exploration, rules, parsers, and the project's own tools
(ruff, pytest, npm, go, cargo, …), run in an explicit plan → edit → verify loop.

- Starts in ~0.1 s, uses ~25 MB RAM (plus whatever the repo's own tools use). No GPU.
- Safe by default: plans and shows diffs; writes only with `--apply`, with backups and automatic revert of a
  cycle that breaks a previously passing check. Files the project's own tools create while checks run (for
  example `Cargo.lock`) are reported separately.
- Languages: Python, JavaScript/TypeScript (npm, pnpm, yarn, bun), Go, Rust, Ruby and C/C++. JS/TS, Go and Rust
  monorepos run each project in its own directory. More languages via plugins — see below.
- Interactive mode: plan, inspect diffs, apply exactly what was shown, revert, commit locally.
- Smallest correct change: a rule only auto-fixes when the right edit is unambiguous; everything else is
  reported with its location for a human decision.

## Install

Requires Python 3.11+ and git.

```bash
git clone https://github.com/Bakery-street-project/CloudyMcCodeFace.git
cd CloudyMcCodeFace
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # add ".[dev]" for pytest, ruff, bandit
```

After installation it runs fully offline. Commands it runs get `GOPROXY=off`, `PIP_NO_INDEX=1`,
`npm_config_offline=true` and `CARGO_NET_OFFLINE=true`.

## Usage

```bash
agent "analyze this repo and tell me what tools and conventions it uses"
agent "fix the CI so it can actually fail"                        # safe mode: plan + diffs
agent --apply "update the README and config files so they match reality"
agent --apply -C ../service "run the test suite, fix the failing tests and lint"
agent --apply --commit "fix formatting"                           # stage + commit cloudy's own edits locally
agent --json "fix the CI" > session.json                          # machine-readable record
agent                                                             # interactive mode
```

Interactive commands: `analyze`, `plan <task>`, `diff`, `apply` (exactly the shown diffs), `apply <task>`,
`status`, `revert`, `commit`, `help`, `quit`. Free text is treated as `plan <text>` and never writes.

Git: `--stage` / `--commit` (with `--apply`) touch only files cloudy changed, skip files that already had your
uncommitted changes, refuse to commit anything else you staged, and never amend, skip hooks or push.

Exit codes: `0` done / clean / analyzed / proposed / reverted, `1` needs a human, `2` usage or config error,
`130` interrupted.

Session logs (plan, commands, checks, diffs, file hashes) and backups are written to
`~/.local/state/cloudy/<repo>/` (or `$XDG_STATE_HOME`), never into the repository.

Optional per-repo config in `cloudy.toml`, `.cloudy.toml` or `[tool.cloudy]` (zero config works):

```toml
timeout = 300                       # seconds per command
max_cycles = 3                      # fix -> verify iterations
deny = ['\bterraform apply\b']      # extra commands that must never run
disabled_rules = ["ci.workflow-permissions"]
disabled_groups = ["docs"]          # rule id prefix: ci, repo, docs, python, js, go, rust, ruby, cpp
ignore = ["vendor/*", "*.min.js"]   # never analysed or edited
checks = { e2e = "make e2e" }       # extra checks
clean_tool_files = false            # true: delete untracked files the project's tools created during a run

[tools]                             # explicit executables instead of PATH lookup
ruff = "~/.local/bin/ruff"
```

## Plugins

Drop a module into `~/.config/cloudy/rules/` to add rules, languages, tools and checks without changing cloudy.
Plugins in a repository's `.cloudy/rules/` load only with `--trust-repo-rules`, because loading them runs their
code. The interface and a minimal example are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#extending-without-models),
along with the design and the full rule catalogue.

## Development

```bash
pip install -e ".[dev]"
ruff check . && bandit -r . -ll && pytest
```

## License

Proprietary — all rights reserved. See [LICENSE](LICENSE).

## Security

See [SECURITY.md](SECURITY.md).
