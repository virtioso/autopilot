"""
Autopilot CLI entry point.

Commands:
  run     Execute a chain file and report the verdict
  list    List available chain files

Usage:
  python autopilot.py run --chain chains/sel4test.json [--timeout 600] [--platform orin-agx]
  python autopilot.py list

Exit codes (run command):
  0  Matched("pass") or Matched("ok")
  1  Matched with any other label (e.g. "fail")
  2  TimeoutVerdict
  3  Error or unhandled exception

Signal handling:
  SIGINT / SIGTERM cancel the running chain task. The oracle tree's finally
  blocks run ctx.cleanup() (kills processes, stops containers, etc.) before
  the process exits. The exit code is 3 (Error) on cancellation.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import structlog

log = structlog.get_logger()


def _configure_logging() -> None:
    """
    Configure structlog to use stdlib logging as the backend so per-run
    FileHandlers added in ChainRunner can capture all log output to run.log.
    """
    logging.basicConfig(format="%(message)s", level=logging.DEBUG, stream=sys.stderr)
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# run command
# ---------------------------------------------------------------------------

async def _cmd_run(args: argparse.Namespace) -> int:
    from engine.runtime import ChainRunner

    from model.projects import Ambiguous, find_chain
    try:
        chain = find_chain(str(args.chain))
    except (Ambiguous, FileNotFoundError) as exc:
        print(f"autopilot: {exc}", file=sys.stderr)
        return 2
    runner = ChainRunner(
        chain_path=chain,
        timeout=args.timeout,
        platform=args.platform,
        result_base=args.result_dir,
        run_id=args.run_id,
    )

    # Wire SIGINT/SIGTERM to cancel the chain task.
    # CancelledError propagates through the oracle tree; finally blocks
    # in each oracle call ctx.cleanup() before raising.
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def _signal_handler(sig: signal.Signals) -> None:
        log.warning(
            "autopilot.signal_received",
            signal=sig.name,
            action="cancelling chain",
        )
        if main_task and not main_task.done():
            main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler, signal.Signals(sig))

    try:
        verdict = await runner.run()
    except asyncio.CancelledError:
        log.warning("autopilot.cancelled", run_id=runner.run_id)
        return 3
    finally:
        # Remove signal handlers so the process exits cleanly on second Ctrl-C
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)

    print(
        f"verdict: {type(verdict).__name__}"
        + (f"({verdict.label!r})" if hasattr(verdict, "label") else ""),
        file=sys.stderr,
    )
    print(f"results: {runner.result_dir}", file=sys.stderr)
    return runner.exit_code()


# ---------------------------------------------------------------------------
# list command
# ---------------------------------------------------------------------------

def _cmd_list(args: argparse.Namespace) -> int:
    if args.chains_dir != "chains":
        chains_dir = Path(args.chains_dir)
        if not chains_dir.exists():
            print(f"Chains directory not found: {chains_dir}", file=sys.stderr)
            return 1
        for chain in sorted(chains_dir.glob("*.json")):
            print(chain.stem)
        return 0
    from model.projects import list_chains
    found = list_chains()
    if not found:
        print("No chain files in chains/ nor in any projects/<name>/chains/", file=sys.stderr)
        return 0
    for label, _ in found:
        print(label)
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autopilot",
        description="Autopilot chain execution engine",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # run
    run_p = sub.add_parser("run", help="Execute a chain file")
    run_p.add_argument(
        "--chain",
        required=True,
        type=Path,
        metavar="FILE",
        help="Chain: a path, or a name resolved in chains/ or a project's (projects/<name>/chains/)",
    )
    run_p.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        metavar="SECONDS",
        help="Chain execution timeout in seconds (default: 3600)",
    )
    run_p.add_argument(
        "--platform",
        default=None,
        metavar="NAME",
        help="Platform profile to load from platforms/<NAME>.yaml",
    )
    run_p.add_argument(
        "--result-dir",
        type=Path,
        default=Path("results"),
        metavar="DIR",
        help="Base directory for run results (default: results/)",
    )
    run_p.add_argument(
        "--run-id",
        default=None,
        metavar="ID",
        help="Run identifier (default: <chain>-<timestamp>)",
    )

    # list
    list_p = sub.add_parser("list", help="List available chains")
    list_p.add_argument(
        "--chains-dir",
        default="chains",
        metavar="DIR",
        help="Directory to search for chain files (default: chains/ and every projects/<name>/chains/)",
    )

    return parser


def main() -> None:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "run":
        exit_code = asyncio.run(_cmd_run(args))
    elif args.command == "list":
        exit_code = _cmd_list(args)
    else:
        parser.print_help()
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
