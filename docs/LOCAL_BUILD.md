# Local build and verification (for Claude Code on your own machine)

These steps finish what the cloud session could not do: it had no GPU, no real model and no Windows. Hand this file
to Claude Code running on the target machine (an HP OMEN 880 desktop, 64 GB RAM, NVIDIA GPU, Windows), or follow it
yourself. Each step says what to run, what to expect, and what to report.

## Ground rules for Claude Code

- **Keep the scope.** Measure, test and report. Change code only for a real bug you can reproduce, with a failing
  test first. Add no features, no new languages and no refactors.
- **Never skip, disable or loosen a test** to make it pass. Report a failure with its full output.
- **Never run `agent --apply` on the cloudy repository itself.** Use a throwaway copy (step 6).
- **No network use by cloudy.** Only two downloads are allowed, both done by you: llama.cpp and the GGUF model files.
  cloudy itself never downloads anything.
- **Git:** work on branch `claude/project-production-readiness-j70w2y`. Never force-push, rebase, amend or
  `--no-verify`. Do not open or merge pull requests. Push only when the user says so.
- **Report measured numbers exactly as measured.** Do not round them to match the estimates in the docs.

## 0. Hardware inventory (PowerShell)

```powershell
Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors
Get-CimInstance Win32_PhysicalMemory | Measure-Object Capacity -Sum | ForEach-Object { "{0} GB RAM" -f ($_.Sum / 1GB) }
Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, AdapterRAM
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
Get-PSDrive -PSProvider FileSystem | Select-Object Name, @{n='FreeGB';e={[math]::Round($_.Free/1GB)}}
wsl --status
```

Record the CPU, the RAM, the GPU name, VRAM and driver, free disk space, and whether WSL2 is installed.
`AdapterRAM` caps at 4 GB, so use VRAM from `nvidia-smi`. You need about 20 GB free for llama.cpp and both models.

## 1. Find the project

The repository is `Bakery-street-project/CloudyMcCodeFace` on GitHub. It is private, so you need git credentials,
for example the GitHub CLI's `gh auth login`.

```powershell
# If it is already cloned, look for it first:
Get-ChildItem -Path $HOME -Filter CloudyMcCodeFace -Directory -Recurse -Depth 3 -ErrorAction SilentlyContinue
# Otherwise:
git clone https://github.com/Bakery-street-project/CloudyMcCodeFace.git
cd CloudyMcCodeFace
git fetch origin
git checkout claude/project-production-readiness-j70w2y
git log --oneline -1          # expect: cloudy 2.0.0 (or a later commit on this branch)
```

The branch holds cloudy 2.0.0 (core, TUI, optional local AI). Pull request #26 carries the same code on branch
`feat/local-ai-v2.0`.

## 2. Install and run the test suite

The test suite's stand-in AI engine is a `#!/bin/sh` script, so **run the full suite on Linux: WSL2 Ubuntu**.
Running it natively on Windows is step 2b and is expected to show some POSIX-only failures. Those are findings to
report, not to fix by skipping.

### 2a. WSL2 (the reference run)

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
cd ~ && git clone https://github.com/Bakery-street-project/CloudyMcCodeFace.git && cd CloudyMcCodeFace
git checkout claude/project-production-readiness-j70w2y
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # needs Python 3.11+; installs rich, PyYAML, textual, pytest, ruff, bandit
ruff check . && bandit -r src tests -ll && pytest -q
```

Expect `ruff` to be clean, `bandit` to report no medium or high issues, and `194 passed, 1 skipped`. The skipped
test is the real-model smoke test (step 4). Keep the WSL clone separate from the Windows clone; do not share one
checkout across both.

### 2b. Native Windows (PowerShell)

```powershell
py -3.12 -m venv .venv            # any Python 3.11+
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
ruff check .; bandit -r src tests -ll; pytest -q
agent --version                   # expect 2.0.0
agent "analyze this repo"         # read-only; expect a summary and exit code 0 or 1
```

Report every failing test with its output and say whether the cause is POSIX-only (for example `/bin/sh` or
`chmod`) or a real Windows bug in cloudy. Real Windows bugs in `executor.py` or `ai/engine.py` are in scope to fix,
each with a test.

## 3. Get the engine and the models (manual downloads)

**llama.cpp `llama-server`:**

- **Windows:** from https://github.com/ggml-org/llama.cpp/releases, download the latest Windows x64 **CUDA** zip and,
  if the release lists one, the matching `cudart` zip. Unzip both into `C:\llama.cpp\`. Then check it runs:
  `C:\llama.cpp\llama-server.exe --version`.
- **WSL2 (optional):** build it with CUDA using `cmake -B build -DGGML_CUDA=ON && cmake --build build -j`. That needs
  the CUDA toolkit for WSL, and the Windows NVIDIA driver provides the GPU.

**Models (GGUF):**

- a 7B coding-instruct model in `Q5_K_M`
- optionally, a 14B coding-instruct model in `Q4_K_M`

Qwen2.5-Coder-7B/14B-Instruct is the suggested first candidate. Check the licence. Look at the file list before
downloading, because some repositories split a quantisation into several `-0000N-of-0000M.gguf` parts. If so, keep
all the parts together and point cloudy at the first one. Put the files in `C:\models\`.

## 4. Configure cloudy and run the real-model smoke test

The user config is `%APPDATA%\cloudy\config.toml` on Windows and `~/.config/cloudy/config.toml` in WSL2. Use
single-quoted TOML strings so backslashes stay literal:

```toml
[ai]
engine = 'C:\llama.cpp\llama-server.exe'
model = 'C:\models\qwen2.5-coder-7b-instruct-q5_k_m.gguf'
deep_model = 'C:\models\qwen2.5-coder-14b-instruct-q4_k_m.gguf'
gpu_layers = "auto"
```

Smoke test. On Windows, run it with only this test selected:

```powershell
$env:CLOUDY_TEST_AI_ENGINE = 'C:\llama.cpp\llama-server.exe'
$env:CLOUDY_TEST_AI_MODEL  = 'C:\models\qwen2.5-coder-7b-instruct-q5_k_m.gguf'
pytest -q tests/test_ai.py -k real_model
```

Expect `1 passed`. If it fails, report the pytest output. The test and the benchmark do not keep the engine's log.
To capture it, run `agent chat` and ask one question. The log is then in `%LOCALAPPDATA%\cloudy\llama-server.log`
(on Linux, `~/.local/state/cloudy/llama-server.log`).

## 5. Benchmark: fill in the table in `docs/LOCAL_AI.md`

```powershell
python -m cloudy.ai.bench             # 7B, GPU offload (gpu_layers = "auto")
python -m cloudy.ai.bench --deep      # 14B, GPU offload
# then set gpu_layers = 0 in the config and run both again for the CPU-only rows
```

Each run prints one row: load time, time to first token, tokens per second and peak RAM. On Windows, peak RAM is not
measured automatically. Watch `llama-server.exe` in Task Manager (Details, "Memory (active private working set)")
and note its peak. Also record VRAM use from `nvidia-smi` during the GPU runs. Put the four measured rows into the
table in `docs/LOCAL_AI.md`, replacing "to measure", and add the machine's CPU, GPU and driver under the table.
Also record cloudy's own footprint with no AI: `agent "analyze this repo"` should take about 0.2 s to start and use
about 25 MB.

## 6. End-to-end on a throwaway copy

```powershell
git clone . $env:TEMP\cloudy-e2e
cd $env:TEMP\cloudy-e2e
agent chat
```

In the chat:

1. `what do the current findings mean?` should give an answer with no edits.
2. Ask for one small fix, then run `diff`. Review the proposal, which is labelled `ai.proposal`.
3. Run `apply`. The checks run, and the edit is reverted automatically if it breaks a passing check.
4. Run `revert`, then `quit`.
5. Check that no `llama-server` process is left running: `Get-Process llama-server -ErrorAction SilentlyContinue`
   should print nothing.

Repeat in `agent tui`: the chat pane appears; confirm that `apply` asks y/n and that the screen stays responsive
while the model answers. Record anything that is wrong, with the exact steps.

## 7. Report and commit

Commit on `claude/project-production-readiness-j70w2y`:

- the filled benchmark table in `docs/LOCAL_AI.md`
- replacing "it has not been tested on Windows yet" in its Limits section with what you actually verified
- any real bug fixes, each with its test

Use one commit per kind of change. Before pushing, run `ruff check .`, `bandit -r src tests -ll` and `pytest -q` (in WSL2),
and ask the user before you push.

Then give a short report:

- hardware
- the results of steps 2a, 2b and 4
- the benchmark rows
- the end-to-end findings
- the commit hashes
- anything you could not do, and why

## GitHub Actions on the repository — resolved

Workflow runs used to fail with `startup_failure` before any job started: the account is locked for
GitHub-hosted minutes by a billing problem (owner: Settings → Billing & plans; this cannot be fixed from the
CLI). Since self-hosted jobs are not billed, CI now runs on a self-hosted runner (`cloudy-desktop`) registered
on the development desktop; `runs-on: self-hosted` in `.github/workflows/ci.yml`.

Runner facts a future operator needs:

- Built-in `setup-python` has no CPython build for Arch Linux, so the workflow provisions a venv from the
  system Python into `RUNNER_TEMP` (outside the checkout, so `bandit -r .` never scans it).
- The runner's PATH must include node 22 (mise), matching the reference environment; newer node switches
  `node --test` output to the spec reporter (handled in the verifier since 2026-10).
- A `run:` line whose value starts with a double quote is invalid YAML in GitHub's parser (silently kills run
  creation and trigger registration) — use block scalars.
- Trigger registration quirk: `gh workflow run <file>` and push/PR event matching are resolved against the
  **default branch's** copy of the workflow; a branch whose `ci.yml` differs from main's can silently get
  zero runs (observed twice, fixed by aligning main). **All three working branches must carry `ci.yml`
  byte-identical** (check: `git rev-parse <ref>:.github/workflows/ci.yml` returns the same blob).

Once the runner listens, push/PR/dispatch runs execute `.github/workflows/ci.yml` (ruff, bandit, pytest
with coverage, pip-audit).

## CI solution: the self-hosted runner is permanent

The account cannot resolve the Actions billing lock (owner confirmed), so **hosted minutes are treated as
unavailable indefinitely and CI is designed to not need them.** The billing lock only stops GitHub-*hosted*
jobs; it does not touch a self-hosted runner or the free security features a public repository gets. Everything
this project's CI and compliance issues (#8, #18) require runs without hosted minutes:

| Capability | Where it runs | Hosted-minutes needed? | Verified state |
|---|---|---|---|
| ruff, bandit, pytest+coverage, pip-audit | self-hosted `ci.yml` job | No | green (push/PR/dispatch) |
| CodeQL Python analysis → code scanning | self-hosted workflow (SARIF upload) | No | probe run 37042326419 attempt 2: upload complete, 0 alerts |
| Secret scanning + push protection | GitHub-side, free on public repos | No | both **enabled**, 0 alerts |
| Dependabot security updates | GitHub-side | No | enabled |

The runner is a systemd **user** service (`actions-runner.service`, `Restart=always`, `StartLimitIntervalSec=0`,
enabled, Linger=yes) → it survives logout and reboot and self-heals from crashes, because on this machine it is
the whole CI. `gh api .../actions/runners` should always show `cloudy-desktop: online`. If CI stops moving, that
endpoint is the first thing to check, then `systemctl --user status actions-runner`.

## Appendix: returning to GitHub-hosted runners (only if billing is ever fixed)

Skip this entirely unless the account owner later resolves billing. Current verified state (2026-10-02): hosted
minutes are billing-locked. A throwaway `ubuntu-latest` probe (branch `ci/hosted-probe`, since deleted) produced
run **37051032502**: job `ping` failed with `The job was not started because your account is locked due to a
billing issue` and **0 steps executed**. Do not switch `ci.yml` to hosted before the probe in step 2 executes a
step — that is how CI silently dies (0 jobs per run).

1. **Unlock (owner, browser only — not possible via CLI or API).** As the account that pays (`BoozeLee`):
   github.com → Settings → Billing and plans → fix the payment problem or spending limit that put Actions in
   the locked state. Every hosted job across the account stays dead until this is done.
2. **Verify the unlock with a probe, not with production CI.** Recreate the throwaway probe: branch off any
   ref with a one-job workflow (`runs-on: ubuntu-latest`, `on: push: branches: [<probe>]`, one `echo` step,
   block scalars). Push; expect a run whose step actually **executes**. If it fails with the billing
   annotation again, stop here — nothing else in this runbook applies yet. Delete the probe branch after.
3. **Swap `ci.yml` to the hosted variant** below, then apply it to **main, the readiness branch and
   `feat/local-ai-v2.0` in one sitting** (cherry-picks are fine — byte-identical blobs, verify with
   `git rev-parse <ref>:.github/workflows/ci.yml` across all three). Main first: trigger registration reads
   the default branch.

   ```yaml
   name: CI — CloudyMcCodeFace
   on:
     push:
       branches: [main, master, claude/project-production-readiness-j70w2y]
     pull_request:
       branches: [main, master]
     workflow_dispatch:
   permissions:
     contents: read
   jobs:
     quality:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with:
             python-version: "3.12"
         # No RUNNER_TEMP venv here: a fresh hosted checkout has no in-repo .venv, so `bandit -r .` is already
         # clean and the setup-python interpreter is on PATH — tools are called bare. This is the only structural
         # difference from the self-hosted variant (which needs a venv outside the tree and node 22 on PATH).
         - name: Install dev tools
           if: hashFiles('pyproject.toml') != ''
           run: |
             python -m pip install --quiet -e ".[dev]"
         - name: Lint (ruff)
           if: hashFiles('pyproject.toml') != ''
           run: |
             ruff check .
         - name: Security scan (bandit)
           if: hashFiles('pyproject.toml') != ''
           run: |
             bandit -r . -ll
         - name: Tests (pytest)
           if: hashFiles('pyproject.toml') != ''
           # Exit code 5 means no tests were collected; treat as success until tests exist.
           # Coverage is reported for visibility (baseline 92%); no threshold gate yet.
           run: |
             pytest --tb=short --cov=cloudy --cov-report=term || [ $? -eq 5 ]
         - name: Dependency audit (pip-audit)
           if: hashFiles('pyproject.toml') != ''
           run: |
             pip freeze -q > "${RUNNER_TEMP}/requirements.txt"
             pip-audit --requirement "${RUNNER_TEMP}/requirements.txt"
   ```

   Pre-validated 2026-10-02 on a clean checkout export (no hosted runner needed for the command shape):
   ruff clean, `bandit -r . -ll` exit 0, bare `pytest --cov` 196 passed / 92%, pip-audit invocation exit 0
   (see Wave-2 local run). Node tests are `@needs_node` (skip unless node+npm+eslint+prettier are all on PATH);
   a bare hosted image has no global eslint/prettier, so those JS tests skip on hosted rather than fail — the
   Python suite, ruff, bandit and pip-audit still run. If you want them exercised, add an `npm i -g
   eslint prettier` step (the verifier already parses both TAP and spec-reporter output).
4. **Watch all three event types go green**: the main push run (skeleton guard must SKIP the steps, job
   succeeds), a readiness push run (all steps execute), a `pull_request` run on PR #26's head (all steps
   execute). Any of them showing 0 jobs means step 3's blob-parity rule was broken.
5. **Keep `cloudy-desktop` in standby** — do not deregister it. The systemd user service is `enabled` with
   lingering on, so it survives logout and reboot and takes jobs the moment `runs-on` says `self-hosted`
   again. Billing locks recur (account-level, prepaid-minute exhaustion); the standby runner is the 60-second
   escape hatch.
6. **Rollback** = revert the ci.yml swap on all three branches (the self-hosted variant is in git history on
   every branch; `git revert` the swap commit, don't hand-edit). Verify blob parity again, then one
   `workflow_dispatch` to confirm the runner picks work up.
