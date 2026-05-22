#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path


CODE_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    autopilot_dir = Path(os.environ.get("AUTOPILOT_DIR", "")).expanduser()
    if not autopilot_dir:
        print("AUTOPILOT_FAIL: AUTOPILOT_DIR is required", flush=True)
        return 2

    physical_tty0 = os.environ.get("AUTOPILOT_TTY0", "").strip()
    physical_tty1 = os.environ.get("AUTOPILOT_TTY1", "").strip()
    if not physical_tty0:
        print("AUTOPILOT_FAIL: AUTOPILOT_TTY0 is required", flush=True)
        return 2
    if not physical_tty1:
        print("AUTOPILOT_FAIL: AUTOPILOT_TTY1 is required", flush=True)
        return 2

    runtime_dir = autopilot_dir / "runtime" / "virtioso_mux_wrapper"
    status_path = runtime_dir / "status.json"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({
        "mode": "orin-uarti-per-run-mux-carrier",
        "physical_tty0": physical_tty0,
        "physical_tty1": physical_tty1,
        "effective_tty0": physical_tty0,
        "effective_tty1": physical_tty1,
        "mux_lifecycle": "per-run-chain",
        "started_at": time.time(),
    }, indent=2, sort_keys=True) + "\n")
    print(
        f"AUTOPILOT_INFO: UARTI_MUX_CARRIER physical_tty1={physical_tty1} "
        f"raw_ccplex_tty0={physical_tty0} mux_lifecycle=per-run-chain",
        flush=True,
    )

    child_env = os.environ.copy()
    child_env["AUTOPILOT_PHYSICAL_TTY0"] = physical_tty0
    child_env["AUTOPILOT_PHYSICAL_TTY1"] = physical_tty1
    child_env["AUTOPILOT_TTY0"] = physical_tty0
    child_env["AUTOPILOT_TTY1"] = physical_tty1
    child_env.pop("AUTOPILOT_CONSOLE_ROUTER_SESSIONS", None)
    child_env.pop("AUTOPILOT_CONSOLE_ROUTER_LOGS_DIR", None)
    child_env.pop("AUTOPILOT_CONSOLE_ROUTER_REGISTRY", None)

    autopilot_proc = subprocess.Popen(
        ["python3", str(CODE_ROOT / "orin_kernel_autopilot.py")],
        env=child_env,
    )

    def request_stop(_signum, _frame) -> None:
        if autopilot_proc.poll() is None:
            autopilot_proc.terminate()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        return autopilot_proc.wait()
    finally:
        if autopilot_proc.poll() is None:
            autopilot_proc.terminate()
            try:
                autopilot_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                autopilot_proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
