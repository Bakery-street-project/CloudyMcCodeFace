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
| `apply` writes exactly what `plan` showed | The REPL keeps the planned `Edit` objects; applying re-checks every file is unchanged (stale → nothing kept) instead of re-planning into different edits. |
| Git is local and conservative | cloudy stages only its own edits, refuses files that had user changes, refuses to commit foreign staged files, never amends/skips hooks/pushes; the deny-list blocks history rewriting. |
| Plugins need explicit trust | User plugins (`~/.config/cloudy/rules/`) load automatically; repository plugins run code inside cloudy even in analyze mode, so only `--trust-repo-rules` enables them — never repository config. |

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
| `executor.py` | Executor | argv or shell commands (bash; `cmd.exe` on Windows without bash) confined to the repo; timeouts kill the whole process tree; output clipped; minimal allow-listed environment; deny-list for destructive commands (`sudo`, `rm -rf /`, `curl \| sh`, and every git command that pushes, rewrites history or drops work). A guard rail, not a sandbox. |
| `verifier.py` | Verifier | Builds checks from the profile, per project directory: ruff, mypy, pytest; `<pm> lint`/`<pm> test`, eslint, prettier, tsc; gofmt, go vet, golangci-lint (if configured), go test; cargo fmt, cargo clippy, cargo test (`--offline`); make test; custom. Missing tool or uninstalled dependencies ⇒ `skip` with the reason, never a guess. Interprets output into `file:line` details (pytest, node:test/jest/vitest, tsc, eslint JSON, go test/vet, cargo panics and compile errors). |
| `rules/` | Rule system | Deterministic checks and fixes, grouped by intent. Each module exports `RULES`; Ruby and C/C++ are written purely against the plugin interface. |
| `plugins.py` | Registry | Loads built-in rule modules, user plugins and (trusted) repository plugins; validates them; merges their languages, tools, manifests, test patterns and checks; applies `disabled_rules` / `disabled_groups`. |
| `git.py` | Git helper | Read-only status and diff summaries; stages only cloudy's applied edits; local commit with a deterministic message. |
| `repl.py` | Interactive mode | `analyze`, `plan`, `diff`, `apply`, `apply <task>`, `status`, `revert`, `commit`, `quit`; free text is only ever planned. |
| `state.py` | Memory | Session JSON: task, intents, plan steps and statuses, every command with exit code and duration, every verification, findings, edits with before/after SHA-256 and diff; backups per session. |
| `orchestrator.py` | Orchestrator | The loop above; safe vs apply mode; `apply_edits` (apply a plan exactly), `revert`, `commit_session`; rollback on regression or stale files; ignore patterns; tool side effects (optionally removed); final status. |
| `report.py`, `cli.py` | UX | Rich terminal report or JSON; "needs a human" items with `file:line` and a hint; exit codes. |

## Directory structure

```
pyproject.toml
src/cloudy/
  cli.py            entry point: `agent` / `cloudy`
  repl.py           interactive mode
  orchestrator.py   plan → edit → verify loop, apply-plan, revert, commit
  plugins.py        plugin interface and registry
  git.py            safe git helpers
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
    __init__.py     Rule, RepoContext, pipe_through (stdin formatter helper)
    ci.py           masked failures, workflow permissions, Dependabot ecosystems
    repo.py         CODEOWNERS
    docs.py         license mismatch, clone URL, broken links, phantom commands, placeholders, duplicates
    python.py       ruff autofix, pytest src-layout pythonpath
    javascript.py   lockfiles, script tools, eslint autofix, prettier
    go.py           go.mod presence, gofmt
    rust.py         crate edition, rustfmt
    ruby.py         plugin: .ruby-version, rubocop safe autocorrect; rubocop/minitest/rspec/rake checks
    cpp.py          plugin: committed build output, clang-format; clang-format and CMake/ctest checks
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
| `ruby.rubocop` | fix_lint | Only when the project uses rubocop (`.rubocop.yml` or Gemfile). `rubocop -a --stdin` (safe autocorrect only; `bundle exec` when the Gemfile pins it). An offense counts as fixable only if `-a` really corrects it — `-A`-only cops are reported. |
| `ruby.version` | fix_tests, fix_ci | Report only: `.ruby-version` differs from the Ruby running the checks. |
| `cpp.clang-format` | fix_lint | Only files covered by a `.clang-format` (in their directory or an ancestor); `clang-format --assume-filename` on stdin. Without a config nothing is formatted — any style would be a guess. |
| `cpp.build-output` | fix_ci | Report only: `CMakeCache.txt`, `CMakeFiles/`, object files tracked by git. |

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

## Interactive mode and git

`agent` without a task starts the REPL (commands can also be piped in). `plan <task>` shows diffs and writes
nothing; `apply` writes exactly those diffs, verifies against the plan's baseline and rolls everything back if a
file changed in the meantime or a passing check breaks; `revert` undoes the last apply (only files nobody touched
since); `status` lists pending and applied files, what needs a human (with `file:line` and a hint) and `git status`;
`commit` stages cloudy's own files and commits locally. From the CLI, `--apply --stage` / `--apply --commit` do
the same. The commit message is deterministic — the task as subject (with `chore:` when the history uses
conventional commits) and one body line per rule with its files:

```
chore: fix formatting, lint and the CI

Applied by cloudy (deterministic rules, no model):

- ci.masked-failures: .github/workflows/ci.yml
- cpp.clang-format: src/calc.c
- ruby.rubocop: lib/calc.rb
```

A file that already had uncommitted user changes is never staged (the commit would mix them in); a commit is
refused while other files are staged; nothing is ever pushed.

## Running on a lightweight desktop

- Any machine with Python 3.11+; no GPU, no background service. Idle cost is zero: it is a CLI, not a daemon.
- Memory is dominated by the checks the repo already has (pytest, tsc, cargo). Use `timeout` in `cloudy.toml`
  and `--max-cycles 1` to bound slow suites; add `deny` patterns for anything that must never run.
- Safe mode never edits files, but checks run the project's own tools, which may write build output
  (`target/`, `Cargo.lock`, caches). Anything they create or change in the git working tree is listed in the
  summary as a tool side effect, separate from cloudy's own edits; `clean_tool_files = true` deletes the new
  untracked *files* again (never directories, never files that existed before). CMake builds always go to a
  directory in the system temp dir, outside the repository.
- Offline use: install dev dependencies (`npm install`, `pip install -e .[dev]`, `go mod download`) once while
  online; checks whose dependencies are missing are reported as `skip` with the reason.
- Developed and tested on Linux; macOS should behave the same. Windows support is best effort and untested:
  string commands use bash (e.g. Git Bash) or fall back to `cmd.exe`, commands run in their own process group and
  a timeout kills the tree with `taskkill /T`, and project-local tools are found with `PATHEXT` (`.cmd` shims).

## Extending without models

Everything language-specific is a plugin: a Python module with `RULES` and optional extension points. The
built-in Ruby and C/C++ support (`rules/ruby.py`, `rules/cpp.py`) uses nothing but this interface; Python,
JavaScript/TypeScript, Go and Rust predate it and keep their checks in `verifier.py`.

| Name | Type | Purpose |
|---|---|---|
| `RULES` | `list[Rule]` (required) | Rule instances. `id` is `"group.name"` and unique; `intents` ⊆ {`fix_ci`, `sync_docs`, `fix_lint`, `fix_tests`}. |
| `LANGUAGES` | `dict[str, str]` | File extension → language (`{".rb": "Ruby"}`). |
| `LANG_TOOLS` | `dict[str, tuple[str, ...]]` | Language → tools the Explorer probes when that language is present. |
| `MANIFESTS` | `dict[str, str]` | Manifest file name → ecosystem. |
| `VERSION_ARGS` | `dict[str, tuple \| None]` | Tool → version arguments (`None`: presence is enough). |
| `TEST_PATTERNS` | `tuple[str, ...]` | File-name globs that mark test files. |
| `checks(profile)` | `-> list[Check]` | Verifier checks; build them with `verifier.command_check(profile, name, category, tool, args, interpret, cwd)`, interpret output with `verifier.match_output(result, name, category, patterns)`. |

A rule implements `check(ctx) -> list[Finding]` and, when a fix can be unambiguous, `fix(ctx) -> {path: new_text}`.
Mark `fixable=True` only for findings `fix()` really resolves; set a class-level `hint` for what a human should do
with the rest. Formatters follow the stdin pattern — list offenders with the tool's own check mode, then
`pipe_through(ctx, argv, path)` each file so the Editor can diff and validate before anything is written. A tool
the project pins (package.json, Gemfile) must run from the project's install: `profile.project_tool(...)`,
`bundle exec`.

Minimal plugin (`~/.config/cloudy/rules/foo.py`):

```python
from cloudy.rules import Rule, pipe_through
from cloudy.verifier import command_check

LANGUAGES = {".foo": "Foo"}
LANG_TOOLS = {"Foo": ("foofmt",)}


class FooFormat(Rule):
    id = "foo.format"
    intents = frozenset({"fix_lint"})
    summary = "Format .foo files with foofmt"

    def check(self, ctx):
        ...  # run `foofmt --check`, return self.finding(path, message, line, fixable=True)

    def fix(self, ctx):
        ...  # return {path: pipe_through(ctx, [ctx.profile.tool_path("foofmt"), "-"], path)}


RULES = [FooFormat()]


def checks(profile):
    return [command_check(profile, "foofmt", "lint", "foofmt", ["--check", "."])] if "Foo" in profile.languages else []
```

Loading and trust: built-ins, then `~/.config/cloudy/rules/*.py` (`$XDG_CONFIG_HOME`, `%APPDATA%` on Windows), then
`<repo>/.cloudy/rules/*.py` only with `--trust-repo-rules`. Files starting with `_` are skipped. A plugin that fails
to import, has the wrong shape or reuses a rule id is reported as a warning and skipped; the run continues.
Rule groups (the id prefix) can be switched off per repository with `disabled_groups`.

Tests for a new language: detection, one safe fix, and one logic bug that must end as `needs_human`.

- **Structured edits**: for refactors beyond whole-file rewrites, a rule can use `libcst` (Python) or
  `ts-morph` via `node` (TypeScript) inside `fix()`; the Editor still validates, diffs and backs up the result.
- **New intents**: add a regex to `planner.INTENT_PATTERNS`, a category mapping in
  `verifier.INTENT_CATEGORIES`, and tag rules with the intent.
- **Known limit**: plugin checks decide their own working directory; the built-in Ruby and C/C++ checks cover a
  project at the repository root (their formatters and rubocop still cover every file).
