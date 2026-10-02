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
from .repl import Repl
from .report import Reporter, RichReporter

EXIT = {"done": 0, "clean": 0, "analyzed": 0, "proposed": 0, "reverted": 0, "needs_human": 1, "interrupted": 130}
EXAMPLES = """examples:
  agent "analyze this repo and tell me what tools and conventions it uses"
  agent "fix the CI so it can actually fail"                 # safe mode: plan + diffs only
  agent --apply "update the README and config files so they match reality"
  agent --apply -C ../other-repo "run the test suite and fix failures"
  agent --apply --commit "fix formatting"                    # local commit of cloudy's own edits, never pushed
  agent                                                       # interactive: plan, diff, apply, status, revert
  agent tui                                                   # full-screen terminal UI (pip install "cloudy[tui]")
  agent chat                                                  # optional local AI chat (see docs/LOCAL_AI.md)
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent", description="Offline, model-free coding agent. Safe mode (default) never writes files.",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", nargs="*", help="what to do; omit for interactive mode")
    parser.add_argument("-C", "--repo", default=".", help="repository root (default: current directory)")
    parser.add_argument("--apply", action="store_true", help="write edits, verify, and loop on failures")
    parser.add_argument("--stage", action="store_true", help="with --apply: git-stage only the files cloudy changed")
    parser.add_argument("--commit", action="store_true",
                        help="with --apply: stage cloudy's files and commit them locally (never pushes)")
    parser.add_argument("--tui", action="store_true", help="full-screen terminal UI (same as `agent tui`)")
    parser.add_argument("--deep", action="store_true", help="local AI: use the [ai] deep_model (e.g. 14B)")
    parser.add_argument("--trust-repo-rules", action="store_true",
                        help="load rule plugins from the repository's .cloudy/rules/ (runs their code)")
    parser.add_argument("--max-cycles", type=int, metavar="N", help="fix/verify iterations (default: config or 3)")
    parser.add_argument("--json", action="store_true", help="print the session record as JSON instead of a report")
    parser.add_argument("--state-dir", type=Path, help="where session logs and backups go")
    parser.add_argument("-v", "--verbose", action="store_true", help="show all check details")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def run_task(task: str, args: argparse.Namespace, config: dict, console: Console, apply: bool) -> int:
    reporter = Reporter() if args.json else RichReporter(console, args.verbose)
    git_action = "commit" if args.commit else "stage" if args.stage else None
    orchestrator = Orchestrator(args.repo, apply=apply, config=config, reporter=reporter, state_dir=args.state_dir,
                                max_cycles=args.max_cycles, trust_repo_rules=args.trust_repo_rules,
                                git_action=git_action)
    session = orchestrator.run(task)
    data = session.to_dict()
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        reporter.summary(data)
    if session.save_error:
        console.print(f"[yellow]warning:[/] {session.save_error}")
    return EXIT.get(data["status"], 1)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console(stderr=args.json)
    if (args.stage or args.commit) and not args.apply:
        console.print("[red]error:[/] --stage and --commit need --apply (nothing is written in safe mode)")
        return 2
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
        if args.tui or args.task == ["tui"]:
            from .tui import run as run_tui
            return run_tui(repo, config, state_dir=args.state_dir, trust_repo_rules=args.trust_repo_rules,
                           deep=args.deep)
        if args.task == ["chat"]:
            return Repl(repo, config, console, state_dir=args.state_dir, trust_repo_rules=args.trust_repo_rules,
                        verbose=args.verbose, chat_mode=True, deep=args.deep).loop()
        if not args.task:
            if args.json:
                console.print("[red]error:[/] a task is required with --json")
                return 2
            return Repl(repo, config, console, state_dir=args.state_dir, trust_repo_rules=args.trust_repo_rules,
                        verbose=args.verbose).loop()
        return run_task(" ".join(args.task), args, config, console, args.apply)
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/]; see the session log for anything already applied")
        return 130


if __name__ == "__main__":
    sys.exit(main())
