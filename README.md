# CloudyMcCodeFace

Privacy-First AI Coding Assistant — `cloudy`, an offline coding agent with **no language model and no network
calls**. Its "intelligence" is deterministic: repository exploration, rules, parsers, and the project's own tools
(ruff, pytest, npm, go, cargo, …), run in an explicit plan → edit → verify loop.

- Starts in ~0.1 s, uses ~25 MB RAM (plus whatever the repo's own tools use). No GPU.
- Safe by default: plans and shows diffs; writes only with `--apply`, with backups and automatic revert of a
  cycle that breaks a previously passing check.
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
agent                                                             # interactive; :apply / :safe / :quit
agent --json "fix the CI" > session.json                          # machine-readable record
```

Exit codes: `0` done / clean / analyzed / proposed, `1` needs a human, `2` usage or config error, `130` interrupted.

Session logs (plan, commands, checks, diffs, file hashes) and backups are written to
`~/.local/state/cloudy/<repo>/` (or `$XDG_STATE_HOME`), never into the repository.

Optional per-repo config in `cloudy.toml` or `[tool.cloudy]`:

```toml
timeout = 300               # seconds per command
max_cycles = 3              # fix -> verify iterations
deny = ['\bterraform apply\b']
disabled_rules = ["ci.workflow-permissions"]
checks = { e2e = "make e2e" }
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, the rule catalogue and how to extend it.

## Development

```bash
pip install -e ".[dev]"
ruff check . && bandit -r . -ll && pytest
```

## License

Proprietary — all rights reserved. See [LICENSE](LICENSE).

## Security

See [SECURITY.md](SECURITY.md).
