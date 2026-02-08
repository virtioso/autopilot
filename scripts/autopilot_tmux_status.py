#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Render tmux status for Autopilot")
    parser.add_argument("--autopilot-dir", required=True)
    args = parser.parse_args()

    state_path = Path(args.autopilot_dir) / "runtime" / "ui" / "state.json"
    if not state_path.exists():
        print("autopilot: idle", end="")
        return 0

    try:
        state = json.loads(state_path.read_text())
    except Exception:
        print("autopilot: state unreadable", end="")
        return 0

    req = state.get("request_id") or "-"
    profile = state.get("profile") or "-"
    step = state.get("step") or "-"
    elapsed = state.get("elapsed_s")
    elapsed_text = f"{elapsed}s" if isinstance(elapsed, int) else "-"
    print(f"autopilot req={req} profile={profile} step={step} elapsed={elapsed_text}", end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
