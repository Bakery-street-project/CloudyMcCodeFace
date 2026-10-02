# Local AI (optional)

cloudy 2.0 can ask a language model running **on your own machine** to explain findings and failing checks and to
propose fixes. It is optional: without it, cloudy behaves exactly as 1.x. Nothing ever leaves your machine.

**The model proposes, you approve, cloudy verifies.** A model can only read, search, list findings, run the
project's checks and *propose* edits. Proposals are ordinary pending diffs: nothing is written until you type
`apply` (the TUI also asks y/n). Applying runs the project's checks and reverts automatically if a previously passing
check breaks. Model edits are labelled `ai.proposal` in diffs, session logs and commit messages.

## Setup

cloudy never downloads anything. You provide two files:

1. **The engine:** llama.cpp's `llama-server` — build it from https://github.com/ggml-org/llama.cpp or use one of
   its release binaries. For an NVIDIA GPU pick a CUDA build.
2. **A model** in GGUF format. Recommended for a 64 GB desktop:
   - default: a 7B coding-instruct model, `Q5_K_M` quantisation (roughly 5–6 GB of RAM)
   - optional deep review (`--deep`): a 14B coding-instruct model, `Q4_K_M` (roughly 9–10 GB)
   The Qwen2.5-Coder Instruct family is a good first candidate; check each model's licence before use. Sizes are
   estimates — measure on your machine (below).

Then tell cloudy where they are in your **user** config — `~/.config/cloudy/config.toml` on Linux/macOS/WSL2,
`%APPDATA%\cloudy\config.toml` on Windows. A repository cannot configure this: `[ai]` in a repository's
`cloudy.toml` is refused, so no repository can choose which program cloudy starts.

```toml
[ai]
engine = '~/llama.cpp/build/bin/llama-server'   # Windows: 'C:\llama.cpp\llama-server.exe'
model = '~/models/coder-7b-instruct-Q5_K_M.gguf'
deep_model = '~/models/coder-14b-instruct-Q4_K_M.gguf'   # optional, used with --deep
gpu_layers = "auto"        # -ngl: "auto", "all" or a number; 0 = CPU only
context = 8192             # tokens
threads = 0                # 0 = let the engine decide
seed = 42                  # temperature is always 0
max_steps = 6              # tool calls per question
max_model_gb = 16          # refuse bigger model files
# endpoint = "http://127.0.0.1:8080"   # instead of engine/model: attach to a llama-server you started yourself
```

## Use

```bash
agent chat                 # conversation; free text goes to the model, all other commands still work
agent chat --deep          # use deep_model
agent tui                  # a chat pane appears when [ai] is configured; type: ask <question>
```

In the interactive mode, the TUI and chat mode alike: `ask <question>`, then `diff` to review the proposals and
`apply` to write and verify them, `revert` to undo, `commit` to commit locally.

## What the model can do

Each model reply must be one JSON object `{"tool": …, "args": {…}}`; the engine constrains output to that schema
and cloudy validates it again. Anything else is rejected and reported.

| Tool | Effect |
|---|---|
| `read_file(path, start?, end?)` | up to 200 numbered lines of a repository file (not ignored, not outside the repo) |
| `search(pattern, glob?)` | regex search, at most 50 hits |
| `list_findings()` | cloudy's findings and failing checks with `file:line` |
| `run_checks()` | run the project's own lint/test checks |
| `propose_edit(path, find, replace, reason)` | a pending diff replacing one exact snippet; must match exactly once and keep the file's syntax valid |
| `answer(text)` | the reply shown to you |

There is no tool to write files, apply, commit, push or run a shell command.

## How it runs

- `llama-server` starts only when you first ask something, and stops when cloudy exits.
- It is started with `--host 127.0.0.1 --offline`, a fresh free port, one slot, and a clean environment (no
  `LLAMA_ARG_*` or `HF_TOKEN` variables can change its flags or enable downloads). Download options (`-hf`,
  `--model-url`) are never passed. Its log goes to `~/.local/state/cloudy/llama-server.log`.
- cloudy talks to it over loopback with the Python standard library (`/v1/chat/completions`, streaming,
  `response_format` with a JSON schema). Non-loopback endpoints are refused.
- Every chat session log records the model file, seed, temperature, context and GPU layers. Temperature 0 and a
  fixed seed make replies repeatable on the same machine and engine build; they are **not** guaranteed identical
  across different hardware or engine versions.

## Benchmark on your machine

```bash
python -m cloudy.ai.bench            # model
python -m cloudy.ai.bench --deep     # deep_model
```

It loads the model, runs three fixed prompts three times each (temperature 0, fixed seed) and prints one table row:
load time, median time to first token, median tokens/s (streamed chunks ≈ tokens) and the engine's peak RAM
(Linux; on Windows read it from Task Manager). Set `gpu_layers = 0` in the config for the CPU-only rows.

| Model | Quant | Device | RAM | Load time | First token | Tokens/s |
|---|---|---|---|---|---|---|
| 7B coding-instruct | Q5_K_M | CPU only | est. 5–6 GB, to measure | to measure | to measure | to measure |
| 7B coding-instruct | Q5_K_M | GPU offload | est. 5–6 GB, to measure | to measure | to measure | to measure |
| 14B coding-instruct | Q4_K_M | CPU only | est. 9–10 GB, to measure | to measure | to measure | to measure |
| 14B coding-instruct | Q4_K_M | GPU offload (partial) | est. 9–10 GB, to measure | to measure | to measure | to measure |

## Limits

- Quality depends on the model. Small local models make mistakes; that is why every proposal is a reviewed diff and
  every apply is verified and reverted on regression. Checks cannot catch every wrong change — read the diff.
- The engine integration is tested against a stand-in server with the same API (`tests/fake_llama_server.py`);
  a real-model smoke test runs when `CLOUDY_TEST_AI_ENGINE` and `CLOUDY_TEST_AI_MODEL` are set.
- Windows: the engine code handles `.exe` paths, process groups and shutdown, but it has not been tested on Windows yet.
