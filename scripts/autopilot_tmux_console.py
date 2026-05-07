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


def terminal_safe_bytes(data: bytes, previous_was_cr: bool) -> tuple[bytes, bool]:
    out = bytearray()
    prev_cr = previous_was_cr
    for byte in data:
        if byte == 0x0a and not prev_cr:
            out.append(0x0d)
        out.append(byte)
        prev_cr = byte == 0x0d
    return bytes(out), prev_cr


def tail_live_file(stop: threading.Event, live_path: Path) -> None:
    offset = 0
    previous_was_cr = False
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
                display_data, previous_was_cr = terminal_safe_bytes(data, previous_was_cr)
                os.write(sys.stdout.fileno(), display_data)
                offset += len(data)
        except Exception:
            time.sleep(0.1)


def main() -> int:
    parser = argparse.ArgumentParser(description="tmux pane client for Autopilot source console")
    parser.add_argument("--autopilot-dir", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--log-path", default=None)
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()

    autopilot_dir = Path(args.autopilot_dir)
    source = args.source
    control_socket = autopilot_dir / "runtime" / "ui" / "control.sock"
    live_path = Path(args.log_path) if args.log_path else autopilot_dir / "runtime" / "ui" / "live" / f"{source}.log"

    stop = threading.Event()
    tail_thread = threading.Thread(target=tail_live_file, args=(stop, live_path), daemon=True)
    tail_thread.start()

    if args.read_only:
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            stop.set()
            return 0

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
