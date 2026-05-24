"""
Relay oracle: USB relay board control for board power cycling.

Uses the usbrelay_py library to control two relays on the board:
  - Relay 1: Recovery mode signal
  - Relay 2: Reset signal (momentary pulse = board reset / power cycle)

action="boot"          → normal boot (recovery=off, pulse reset, recovery=off)
action="boot_recovery" → recovery mode boot (recovery=on, pulse reset, recovery=off)
action="reset"         → pulse reset only (leave recovery relay unchanged)

The "boot" sequence matches BoardControl.boot(False) from the old system exactly.
usbrelay_py is a blocking C extension; relay operations run in a thread executor
so the asyncio event loop is not blocked.
"""

from __future__ import annotations

import asyncio
import structlog

from engine.oracle import Error, Matched, StreamContext, Verdict

log = structlog.get_logger()


class RelayOracle:
    def __init__(self, action: str = "boot") -> None:
        self._action = action

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        try:
            import usbrelay_py  # noqa: F401
        except ImportError:
            return Error("usbrelay_py_not_installed"), ctx

        loop = asyncio.get_running_loop()
        action = self._action
        try:
            await loop.run_in_executor(None, _run_relay, action)
        except Exception as exc:
            log.error("relay.error", action=action, error=str(exc))
            return Error(f"relay_error"), ctx

        log.info("relay.done", action=action)
        return Matched("ok"), ctx


def _run_relay(action: str) -> None:
    import time
    import usbrelay_py

    boards = usbrelay_py.board_details()
    if not boards:
        raise RuntimeError("no USB relay board found")
    board_id = boards[0][0]

    if action == "boot":
        usbrelay_py.board_control(board_id, 1, False)  # recovery=off
        time.sleep(0.1)
        usbrelay_py.board_control(board_id, 2, True)   # assert reset
        time.sleep(0.1)
        usbrelay_py.board_control(board_id, 2, False)  # deassert reset
        time.sleep(0.5)
        usbrelay_py.board_control(board_id, 1, False)  # recovery=off (again)
    elif action == "boot_recovery":
        usbrelay_py.board_control(board_id, 1, True)   # recovery=on
        time.sleep(0.1)
        usbrelay_py.board_control(board_id, 2, True)   # assert reset
        time.sleep(0.1)
        usbrelay_py.board_control(board_id, 2, False)  # deassert reset
        time.sleep(0.5)
        usbrelay_py.board_control(board_id, 1, False)  # recovery=off
    elif action == "reset":
        usbrelay_py.board_control(board_id, 2, True)
        time.sleep(0.1)
        usbrelay_py.board_control(board_id, 2, False)
    else:
        raise ValueError(f"unknown relay action: {action!r}")
