"""Benchmark the configured local model on this machine and print one row for docs/LOCAL_AI.md.

    python -m cloudy.ai.bench            # [ai] model
    python -m cloudy.ai.bench --deep     # [ai] deep_model

Measures load time, time to first token, tokens/s (streamed chunks ≈ tokens) and the engine's peak RAM (Linux;
on Windows read it from Task Manager). Fixed prompts, temperature 0, fixed seed; median of --runs runs.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time

from .client import Client
from .config import ai_config_path, load_ai_config
from .engine import Engine

PROMPTS = (
    "In two sentences: what does a CI step ending in `|| true` do, and why is it risky?",
    "Here is a Python function: `def sub(a, b):\n    return a + b`. A test expects sub(5, 3) == 2. "
    "Explain the bug in one sentence and give the corrected line.",
    "List three things to check when a Go test fails with `Sub(5, 3) = 8, want 2`.",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudy.ai.bench", description=__doc__.splitlines()[0])
    parser.add_argument("--deep", action="store_true", help="benchmark deep_model instead of model")
    parser.add_argument("--runs", type=int, default=3, help="runs per prompt (default 3)")
    args = parser.parse_args(argv)
    config = load_ai_config()
    if config is None:
        print(f"local AI is not configured: add an [ai] table to {ai_config_path()}", file=sys.stderr)
        return 2
    engine = Engine(config, deep=args.deep)
    started = time.monotonic()
    url = engine.start()
    load_s = time.monotonic() - started
    try:
        client = Client(url, timeout=config["request_timeout"], seed=config["seed"], max_tokens=256)
        first, rates = [], []
        for prompt in PROMPTS:
            for _ in range(args.runs):
                completion = client.complete([{"role": "user", "content": prompt}])
                if completion.first_token_s is not None:
                    first.append(completion.first_token_s)
                    generating = completion.total_s - completion.first_token_s
                    if generating > 0 and completion.chunks > 1:
                        rates.append((completion.chunks - 1) / generating)
        ram = engine.peak_rss_mb()
    finally:
        engine.stop()
    if not (first and rates):
        print(f"{engine.model_name}: no tokens received", file=sys.stderr)
        return 1
    quant = re.search(r"(I?Q\d(?:_[A-Z0-9]+)*|F16|BF16)", engine.model_name.upper())
    device = "CPU only" if str(config["gpu_layers"]) == "0" else f"GPU offload (-ngl {config['gpu_layers']})"
    ram_text = f"{ram / 1024:.1f} GB" if ram else "see Task Manager"
    print("| Model | Quant | Device | RAM | Load time | First token | Tokens/s |")
    print("|---|---|---|---|---|---|---|")
    print(f"| {engine.model_name} | {quant.group(1) if quant else '?'} | {device} | {ram_text} | {load_s:.1f} s | "
          f"{statistics.median(first):.2f} s | {statistics.median(rates):.1f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
