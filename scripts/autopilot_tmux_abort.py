#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Autopilot abort command")
    parser.add_argument("--autopilot-dir", required=True)
    args = parser.parse_args()

    socket_path = Path(args.autopilot_dir) / "runtime" / "ui" / "control.sock"
    if not socket_path.exists():
        return 0

    payload = {"type": "abort"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(socket_path))
            sock.sendall(json.dumps(payload).encode("utf-8"))
            _ = sock.recv(4096)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
