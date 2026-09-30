# Architecture

`cloudy` is a coding agent without a model. Every decision comes from code that can be read, tested and
reproduced: the same repository state and task always produce the same plan, edits and report.

## Decisions

| Decision | Rationale |
|---|---|
| Pure Python 3.11, deps: `rich`, `PyYAML` | `tomllib`, `ast`, `difflib`, `subprocess` cover the rest; ~25 MB RSS, ~0.1 s startup. |
| Own loop, no agent framework | The loop is ~150 lines in `orchestrator.py`; frameworks would add more code than they save. |
| Rules = `check()` + optional `fix()` | Findings are data; fixes are whole-file contents the Editor diffs, validates and applies. |
| Auto-fix only when unambiguous | Ambiguous problems (broken links, docs describing missing tooling) are reported, never guessed. |
| Delegate to the project's tools | Lint fixes come from `ruff --fix` on stdin, so the project's own config decides; nothing is reimplemented. |
| Whole-file edits validated by parsers | `ast` / `tomllib` / `json` / `yaml` reject an edit that would break syntax. libcst is not required. |
| JSON session files outside the repo | Inspectable with any editor, no DB, never pollutes `git status`. |
| Checks run per project directory | Each `package.json`, `go.mod` and top-level `Cargo.toml` is a project; its checks and fixes run in its directory, so monorepos are covered, not silently skipped. |
| Pinned JS tools only from the project | If package.json declares eslint/prettier/typescript, only the project's `node_modules` copy is used; a different global version could reformat everything. |
| Offline environment for commands | Package managers are forced offline, so a run never fetches anything. |

## Components

```
task ──► Planner.classify ──► intents
            │
            ▼
       Explorer ──► RepoProfile (languages, manifests, CI, tests, linters, scripts, probed tools, git, license)
            │
            ▼
       Verifier (baseline) ──► CheckResult[]  ─────────────┐
            │                                              │ rules may read check output
            ▼                                              ▼   (e.g. pytest ModuleNotFoundError)
   ┌──► Rules.check ──► Finding[] ──► Planner.next_fixes ──► fix Steps
   │                                                        │
   │       Editor.propose (diff, syntax check) ◄── Rule.fix ┘
   │       Editor.apply (--apply: backup, atomic write, stale check)
   │                    │
   │       Verifier ◄───┘  regression vs baseline? ──► revert cycle, stop
   └──────── next cycle (no-progress detection, max_cycles)
            │
            ▼
       Report (rich or --json) + Session JSON
```

| Module | Agent | Responsibility |
|---|---|---|
| `planner.py` | Orchestrator / Planner | Task → intents (regex rules). Findings → ordered fix steps; skips a rule whose findings did not change after its last attempt (re-planning without loops). |
| `explorer.py` | Explorer | Walks the tree (skips vendored/build dirs, 20k file cap), detects languages, manifests, CI, tests, linter configs, scripts, license, git state and commit style. Records projects: Node (package manager from `packageManager`/lockfile, dependencies incl. workspace root, scripts, eslint/prettier/tsc config), Go modules, Cargo roots. Probes every relevant tool (`--version`, or `VERSION_ARGS`); never assumes one exists. |
| `editor.py` | Editor | Proposes edits into an overlay (so dry-run proposals chain), renders unified diffs, validates syntax, applies atomically keeping file mode and newline style, refuses stale edits and paths outside the repo, reverts. |
| `executor.py` | Executor | argv or bash commands in the repo root only; timeouts kill the whole process group; output clipped; minimal allow-listed environment; deny-list for destructive commands (`git push`, `sudo`, `rm -rf /`, `curl | sh`, …). A guard rail, not a sandbox. |
| `verifier.py` | Verifier | Builds checks from the profile, per project directory: ruff, mypy, pytest; `<pm> lint`/`<pm> test`, eslint, prettier, tsc; gofmt, go vet, golangci-lint (if configured), go test; cargo fmt, cargo clippy, cargo test (`--offline`); make test; custom. Missing tool or uninstalled dependencies ⇒ `skip` with the reason, never a guess. Interprets output into `file:line` details (pytest, node:test/jest/vitest, tsc, eslint JSON, go test/vet, cargo panics and compile errors). |
| `rules/` | Rule system | Deterministic checks and fixes, grouped by intent. |
| `state.py` | Memory | Session JSON: task, intents, plan steps and statuses, every command with exit code and duration, every verification, findings, edits with before/after SHA-256 and diff; backups per session. |
| `orchestrator.py` | Orchestrator | The loop above; safe vs apply mode; rollback on regression; final status. |
| `report.py`, `cli.py` | UX | Rich terminal report or JSON; one-shot and interactive modes; exit codes. |

## Directory structure

```
pyproject.toml
src/cloudy/
  cli.py            entry point: `agent` / `cloudy`
  orchestrator.py   plan → edit → verify loop
  planner.py        intents and fix steps
  explorer.py       repository profile and tool probes
  editor.py         diffs, validation, atomic apply/revert
  executor.py       controlled command execution
  verifier.py       checks and result interpretation
  state.py          session JSON and backups
  config.py         cloudy.toml / [tool.cloudy]
  report.py         terminal reporting
  models.py         shared dataclasses
  rules/
    __init__.py     Rule, RepoContext, registry (order = fix order)
    ci.py           masked failures, workflow permissions, Dependabot ecosystems
    repo.py         CODEOWNERS
    docs.py         license mismatch, clone URL, broken links, phantom commands, placeholders, duplicates
    python.py       ruff autofix, pytest src-layout pythonpath
    javascript.py   lockfiles, script tools, eslint autofix, prettier
    go.py           go.mod presence, gofmt
    rust.py         crate edition, rustfmt
tests/              unit tests per agent and language + end-to-end runs on fixture repos (incl. a polyglot monorepo)
```

## Rule catalogue

| Rule | Intent | Auto-fix |
|---|---|---|
| `ci.masked-failures` | fix_ci | Removes `\|\| true` / `\|\| :` / `\|\| exit 0` after known check/install tools; pytest keeps only exit 5 tolerated while no tests exist. Other masked commands (e.g. `grep … \|\| true`) are reported. `continue-on-error: true` is reported. |
| `ci.workflow-permissions` | fix_ci | Adds `permissions: contents: read` unless the workflow looks like it needs write access (secrets, release, deploy, publish, pages, `gh`). |
| `ci.dependabot-ecosystems` | fix_ci | Renames aliases (`javascript`→`npm`, `python`→`pip`, `go`→`gomod`, …), drops entries without a manifest, never leaves `updates` empty. Unknown names are reported. |
| `repo.codeowners` | fix_ci | `owner` alone → `* @owner`; bare owner names get `@`. A lone token that is an existing path is a valid pattern and left alone. |
| `docs.license-mismatch` | sync_docs | Rewrites the README License section to match LICENSE (MIT, Apache-2.0, proprietary). |
| `docs.clone-url` | sync_docs | Points `git clone` URLs (code blocks and inline code) at the origin remote. |
| `docs.broken-links` | sync_docs | Report only: missing targets, links escaping the repo. |
| `docs.phantom-commands` | sync_docs | Report only: npm/pip/go/cargo/make/docker/python commands whose manifest, script, target or file is missing. |
| `docs.placeholders`, `docs.duplicate-policies` | sync_docs | Report only. |
| `python.ruff-autofix` | fix_lint | ruff's safe fixes via stdin, per file, shown as diffs first. |
| `python.pytest-pythonpath` | fix_tests | When pytest fails with `ModuleNotFoundError` for a package under `src/`, adds `pythonpath = ["src"]` to `[tool.pytest.ini_options]`. |
| `js.eslint-autofix` | fix_lint | The project's eslint (`--fix-dry-run --stdin`, JSON `output`), only when an eslint config exists. |
| `js.prettier` | fix_lint | The project's Prettier (`--stdin-filepath`), only when a Prettier config exists. |
| `js.lockfiles` | fix_ci, fix_tests | Report only: several lockfiles, `packageManager` vs lockfile, `npm ci` / frozen installs without the lockfile. |
| `js.script-tools` | fix_tests, fix_lint | Report only: scripts calling eslint/tsc/jest/vitest/… not declared as dependencies; npm placeholder `test` script while test files exist. |
| `go.gofmt` | fix_lint | `gofmt` via stdin (no configuration exists, so the result is unambiguous); skips vendor/ and testdata/; syntax errors are reported. |
| `go.module` | fix_ci, fix_tests, fix_lint | Report only: Go files without any go.mod. |
| `rust.rustfmt` | fix_lint | Files listed by `cargo fmt --check -- -l`, piped through `rustfmt --edition <crate edition> --emit stdout` in the crate directory (so rustfmt.toml applies). |
| `rust.edition` | fix_ci, fix_lint | Report only: `[package]` without `edition` (Cargo falls back to 2015; changing it can change semantics). |

Test failures that no recipe covers (real logic bugs) are localised (`FAILED node — reason`, `at file:line in fn`)
and left for a human. Without a model the agent cannot know intended behaviour, and it does not pretend to.

## Example: the config/docs-only repository this project started from

Running on commit `e4cca4d` (before the manual fixes), safe mode:

```
$ agent "Update the README and config files so they match reality, and fix the CI so it can actually fail"
Explore (fix_ci, sync_docs)   Languages Markdown (5), YAML (3) · CI uses bandit, pytest, ruff · License Proprietary
Verify (baseline)             pass config-syntax
Fix (cycle 1)                 ci.masked-failures (4) · ci.workflow-permissions (1) · ci.dependabot-ecosystems (3)
                              repo.codeowners (1) · docs.license-mismatch (1) · docs.clone-url (2)
                              … unified diffs for each proposal …
Needs a human decision        missing CODE_OF_CONDUCT.md links · `npm install/start/test/run build` with no
                              package.json · placeholder security email · duplicate SECURITY.md
Status: proposed — re-run with --apply to write them
```

With `--apply` the same run writes the five files, re-verifies (`done`), and a second run reports `clean` with no
edits. On a small Python repo with a src-layout import error, an unused import and a real logic bug, one
`--apply` cycle adds the pytest `pythonpath`, applies ruff's fix, re-runs pytest and stops with `needs_human`,
pointing at the failing assertion instead of guessing a code change.

### Polyglot monorepo (JS in `web/`, Go in `svc/`, a Rust crate at the root)

Each language has an unformatted file, a lint issue and a genuine logic bug (`sub` returns `a + b`); CI masks
failures with `|| true`. `agent --apply "fix the CI, docs, formatting and the failing tests"`:

```
edits   ci.masked-failures, ci.workflow-permissions   .github/workflows/ci.yml
        js.eslint-autofix   web/src/greeting.js   var greeting → const greeting
        js.prettier         web/src/calc.js       formatted
        go.gofmt            svc/calc.go           formatted
        rust.rustfmt        src/lib.rs            formatted
checks  pass web: eslint, web: prettier, gofmt, svc: go vet, cargo fmt, cargo clippy
        fail web: npm test   not ok 2 - sub · web/test/calc.test.js:9:1
        fail svc: go test    calc_test.go:7: Sub(5, 3) = 8, want 2
        fail cargo test      tests::subtracts · src/lib.rs:15:9 · left: 8 / right: 2
manual  `npm ci` fails without package-lock.json · README `npm run build`: no `build` script in web/package.json
Status: needs_human
```

The three `sub` bugs are located, not "fixed". A second run makes no edits. The earlier safe-mode run reported
`Cargo.lock` as created by cargo itself.

## Running on a lightweight desktop

- Any machine with Python 3.11+; no GPU, no background service. Idle cost is zero: it is a CLI, not a daemon.
- Memory is dominated by the checks the repo already has (pytest, tsc, cargo). Use `timeout` in `cloudy.toml`
  and `--max-cycles 1` to bound slow suites; add `deny` patterns for anything that must never run.
- Safe mode never edits files, but checks run the project's own tools, which may write build output
  (`target/`, `Cargo.lock`, caches). Anything they create or change in the git working tree is listed in the
  summary as a tool side effect, separate from cloudy's own edits.
- Offline use: install dev dependencies (`npm install`, `pip install -e .[dev]`, `go mod download`) once while
  online; checks whose dependencies are missing are reported as `skip` with the reason.
- Developed and tested on Linux; macOS should behave the same. Windows is untested (string commands need bash,
  and timeouts there would kill only the direct child process).

## Extending without models

- **New rule**: subclass `Rule` in `rules/`, set `id`, `intents`, `summary`, implement `check()` returning
  `Finding`s (mark `fixable=True` only for unambiguous cases) and `fix()` returning `{path: new_text}`.
  Register it in `all_rules()`; its position there is its fix order. Rules can read `ctx.checks` to react to
  verifier output.
- **New language / verifier**: a language is four small pieces, no new architecture:
  1. *Explorer*: extensions in `LANGUAGES`, tools in `LANG_TOOLS` (plus `VERSION_ARGS` when a tool has no
     `--version`), manifest names in `MANIFESTS`, config files in `LINTER_FILES`.
  2. *Verifier*: a `tool_check(...)` line in `verifier.build_checks` per real project command, with an
     interpreter that turns output into `details` (`file:line` locations; absolute repo paths are made relative
     automatically).
  3. *Rules*: a `rules/<language>.py` module registered in `all_rules()`. Formatters follow the stdin pattern:
     detect with the tool's own check/list mode, fix by piping each file through the tool on stdin and handing
     stdout to the Editor, so the diff is shown before anything is written. Use `project_tool()` so a tool the
     project pins in its manifest is only run from the project's install, never from a different global version.
  4. *Tests*: detection, one safe fix, and one logic bug that must end as `needs_human`.

  Per-repo checks need no code: `checks = { name = "command" }`.
- **Structured edits**: for refactors beyond whole-file rewrites, a rule can use `libcst` (Python) or
  `ts-morph` via `node` (TypeScript) inside `fix()`; the Editor still validates, diffs and backs up the result.
- **New intents**: add a regex to `planner.INTENT_PATTERNS`, a category mapping in
  `verifier.INTENT_CATEGORIES`, and tag rules with the intent.
