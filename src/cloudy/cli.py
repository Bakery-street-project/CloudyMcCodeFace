"""Command line entry point: `agent "task"` for one run, `agent` alone for interactive mode."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console

from . import __version__
from .config import ConfigError, load_config
from .orchestrator import Orchestrator
from .report import Reporter, RichReporter

EXIT = {"done": 0, "clean": 0, "analyzed": 0, "proposed": 0, "needs_human": 1, "interrupted": 130}
EXAMPLES = """examples:
  agent "analyze this repo and tell me what tools and conventions it uses"
  agent "fix the CI so it can actually fail"                 # safe mode: plan + diffs only
  agent --apply "update the README and config files so they match reality"
  agent --apply -C ../other-repo "run the test suite and fix failures"
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent", description="Offline, model-free coding agent. Safe mode (default) never writes files.",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", nargs="*", help="what to do; omit for interactive mode")
    parser.add_argument("-C", "--repo", default=".", help="repository root (default: current directory)")
    parser.add_argument("--apply", action="store_true", help="write edits, verify, and loop on failures")
    parser.add_argument("--max-cycles", type=int, metavar="N", help="fix/verify iterations (default: config or 3)")
    parser.add_argument("--json", action="store_true", help="print the session record as JSON instead of a report")
    parser.add_argument("--state-dir", type=Path, help="where session logs and backups go")
    parser.add_argument("-v", "--verbose", action="store_true", help="show all check details")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def run_task(task: str, args: argparse.Namespace, config: dict, console: Console, apply: bool) -> int:
    reporter = Reporter() if args.json else RichReporter(console, args.verbose)
    orchestrator = Orchestrator(args.repo, apply=apply, config=config, reporter=reporter,
                                state_dir=args.state_dir, max_cycles=args.max_cycles)
    session = orchestrator.run(task)
    data = session.to_dict()
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        reporter.summary(data)
    if session.save_error:
        console.print(f"[yellow]warning:[/] {session.save_error}")
    return EXIT.get(data["status"], 1)


def interactive(args: argparse.Namespace, config: dict, console: Console) -> int:
    apply = args.apply
    console.print("Interactive mode. Type a task, [bold]:apply[/] / [bold]:safe[/] to switch mode, "
                  "[bold]:quit[/] to exit.")
    code = 0
    while True:
        try:
            line = input(f"[{'apply' if apply else 'safe'}] task> ").strip()
        except EOFError:
            console.print()
            return code
        if line in (":quit", ":q", "exit", "quit"):
            return code
        if line in (":apply", ":safe"):
            apply = line == ":apply"
            continue
        if line:
            code = run_task(line, args, config, console, apply)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console(stderr=args.json)
    if args.max_cycles is not None and not 1 <= args.max_cycles <= 10:
        console.print("[red]error:[/] --max-cycles must be between 1 and 10")
        return 2
    repo = Path(args.repo)
    if not repo.is_dir():
        console.print(f"[red]error:[/] repository path does not exist: {repo}")
        return 2
    try:
        config = load_config(repo)
    except ConfigError as exc:
        console.print(f"[red]config error:[/] {exc}")
        return 2
    try:
        if not args.task:
            if args.json or not sys.stdin.isatty():
                console.print("[red]error:[/] a task is required with --json or non-interactive input")
                return 2
            return interactive(args, config, console)
        return run_task(" ".join(args.task), args, config, console, args.apply)
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/]; see the session log for anything already applied")
        return 130


if __name__ == "__main__":
    sys.exit(main())
