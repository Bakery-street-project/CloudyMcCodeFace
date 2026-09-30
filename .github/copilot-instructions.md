# CloudyMcCodeFace Development Instructions

This repository contains `cloudy`, an offline, deterministic coding agent written in Python 3.11+.
Read `README.md` for what it does and `docs/ARCHITECTURE.md` for the design and the plugin interface.

## Setup and checks

```bash
pip install -e ".[dev]"
ruff check . && bandit -r . -ll && pytest
```

The CI workflow (`.github/workflows/ci.yml`) runs exactly these steps. Some end-to-end tests use node, go,
cargo, rubocop, clang-format and cmake; they skip themselves when a toolchain is missing.

## Layout

```
src/cloudy/        agent package: cli, repl, orchestrator, planner, explorer, editor, executor, verifier,
                   git, plugins, state, config, report, models
src/cloudy/rules/  rule modules (each exports RULES); ruby.py and cpp.py use only the plugin interface
tests/             pytest suite, one file per agent/language plus end-to-end fixtures
docs/              ARCHITECTURE.md
```

## Constraints

- Pure Python; runtime dependencies are only `rich` and `PyYAML`.
- Fully offline and deterministic: never add language-model, network or API calls.
- Rules auto-fix only unambiguous problems, with the project's own tools; everything else is reported.
- Never add code that pushes, rewrites git history or runs destructive commands.
- Keep changes minimal and covered by tests; keep `ruff`, `bandit` and `pytest` green.
- Report security issues as described in `SECURITY.md`.
