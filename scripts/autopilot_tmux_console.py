#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import select
import socket
import sys
import termios
import threading
import time
import tty
from pathlib import Path


def send_tx(control_socket: Path, source: str, payload: bytes) -> None:
    msg = {
        "type": "tx",
        "source": source,
        "data_b64": base64.b64encode(payload).decode("ascii"),
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(control_socket))
        sock.sendall(json.dumps(msg).encode("utf-8"))
        _ = sock.recv(65536)


def tail_live_file(stop: threading.Event, live_path: Path) -> None:
    offset = 0
    while not stop.is_set():
        if not live_path.exists():
            time.sleep(0.1)
            continue
        try:
            size = live_path.stat().st_size
            if size < offset:
                offset = 0
            if size == offset:
                time.sleep(0.05)
                continue
            with open(live_path, "rb") as f:
                f.seek(offset)
                data = f.read(8192)
            if data:
                os.write(sys.stdout.fileno(), data)
                offset += len(data)
        except Exception:
            time.sleep(0.1)


def main() -> int:
    parser = argparse.ArgumentParser(description="tmux pane client for Autopilot source console")
    parser.add_argument("--autopilot-dir", required=True)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()

    autopilot_dir = Path(args.autopilot_dir)
    source = args.source
    control_socket = autopilot_dir / "runtime" / "ui" / "control.sock"
    live_path = autopilot_dir / "runtime" / "ui" / "live" / f"{source}.log"

    stop = threading.Event()
    tail_thread = threading.Thread(target=tail_live_file, args=(stop, live_path), daemon=True)
    tail_thread.start()

    stdin_fd = sys.stdin.fileno()
    old = termios.tcgetattr(stdin_fd)
    try:
        tty.setraw(stdin_fd)
        while True:
            r, _, _ = select.select([stdin_fd], [], [], 0.1)
            if not r:
                continue
            data = os.read(stdin_fd, 1024)
            if not data:
                break
            # Exit helper pane on Ctrl-] to avoid trapping operators.
            if data == b"\x1d":
                break
            try:
                send_tx(control_socket, source, data)
            except Exception:
                time.sleep(0.1)
    finally:
        stop.set()
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
