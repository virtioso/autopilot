from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional


class TmuxUIState:
    """Persist Autopilot UI runtime state for tmux status and helpers."""

    def __init__(self, autopilot_dir: Path):
        self.autopilot_dir = autopilot_dir
        self.ui_dir = autopilot_dir / "runtime" / "ui"
        self.live_dir = self.ui_dir / "live"
        self.state_path = self.ui_dir / "state.json"
        self.control_socket_path = self.ui_dir / "control.sock"
        self._lock = threading.Lock()
        self._state = {
            "updated_at": None,
            "request_id": None,
            "profile": None,
            "chain": None,
            "subchain": None,
            "step": None,
            "elapsed_s": None,
            "source_map": {},
            "window_map": {},
        }
        self.ui_dir.mkdir(parents=True, exist_ok=True)
        self.live_dir.mkdir(parents=True, exist_ok=True)
        self._flush_locked()

    def set_request(self, request_id: str, profile: str, chain: str, subchain: str = "-") -> None:
        with self._lock:
            self._state["request_id"] = request_id
            self._state["profile"] = profile
            self._state["chain"] = chain
            self._state["subchain"] = subchain
            self._flush_locked()

    def clear_request(self) -> None:
        with self._lock:
            self._state["request_id"] = None
            self._state["profile"] = None
            self._state["chain"] = None
            self._state["subchain"] = None
            self._state["step"] = None
            self._state["elapsed_s"] = None
            self._flush_locked()

    def set_step(self, step: str, elapsed_s: int) -> None:
        with self._lock:
            self._state["step"] = step
            self._state["elapsed_s"] = elapsed_s
            self._flush_locked()

    def set_subchain(self, subchain: str) -> None:
        with self._lock:
            self._state["subchain"] = subchain
            self._flush_locked()

    def map_source(self, source: str, tty: str, log_path: str) -> None:
        with self._lock:
            self._state["source_map"][source] = {"tty": tty, "log_path": log_path}
            self._flush_locked()

    def map_window(self, window: int, source: str, title: Optional[str] = None) -> None:
        with self._lock:
            self._state["window_map"][str(window)] = {
                "source": source,
                "title": title or source,
            }
            self._flush_locked()

    def live_path_for_source(self, source: str) -> Path:
        return self.live_dir / f"{source}.log"

    def _flush_locked(self) -> None:
        self._state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.state_path.write_text(json.dumps(self._state, indent=2))


class TmuxControlServer:
    """Tiny JSON-over-UDS control endpoint for tmux helpers."""

    def __init__(
        self,
        socket_path: Path,
        on_abort: Callable[[], None],
        on_tx: Callable[[str, bytes], None],
    ):
        self.socket_path = socket_path
        self.on_abort = on_abort
        self.on_tx = on_tx
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.socket_path))
        self._sock.listen(8)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.connect(str(self.socket_path))
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except Exception:
                continue
            with conn:
                try:
                    raw = conn.recv(262144)
                    payload = json.loads(raw.decode("utf-8"))
                    result = self._handle(payload)
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}
                conn.sendall(json.dumps(result).encode("utf-8"))

    def _handle(self, payload: dict) -> dict:
        cmd = payload.get("type")
        if cmd == "abort":
            self.on_abort()
            return {"ok": True}
        if cmd == "tx":
            source = str(payload.get("source", ""))
            data = payload.get("data_b64", "")
            if not source:
                return {"ok": False, "error": "missing source"}
            if not data:
                return {"ok": False, "error": "missing data_b64"}
            import base64
            decoded = base64.b64decode(data)
            self.on_tx(source, decoded)
            return {"ok": True}
        return {"ok": False, "error": f"unknown command type: {cmd}"}


class TmuxWindowManager:
    """Session-local tmux window orchestration for source consoles."""

    def __init__(self, session: str):
        self.session = session

    def ensure_window(self, window: int, title: str, command: str) -> None:
        target = f"{self.session}:{window}"
        if not self._window_exists(window):
            subprocess.run(
                ["tmux", "new-window", "-t", f"{self.session}:", "-n", title],
                check=False,
            )
        subprocess.run(["tmux", "rename-window", "-t", target, title], check=False)
        subprocess.run(["tmux", "respawn-pane", "-k", "-t", target, command], check=False)

    def bind_status_command(self, command: str) -> None:
        subprocess.run(["tmux", "set-option", "-t", self.session, "status", "on"], check=False)
        subprocess.run(["tmux", "set-option", "-t", self.session, "status-right", command], check=False)

    def bind_abort_key(self, key: str, command: str) -> None:
        subprocess.run(
            ["tmux", "bind-key", "-T", "prefix", key, "run-shell", command],
            check=False,
        )

    def _window_exists(self, window: int) -> bool:
        target = f"{self.session}:{window}"
        proc = subprocess.run(
            ["tmux", "list-windows", "-t", self.session, "-F", "#{window_index}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return False
        return str(window) in set(line.strip() for line in proc.stdout.splitlines() if line.strip())


def detect_tmux_session() -> Optional[str]:
    tmux_env = os.environ.get("TMUX", "")
    if not tmux_env:
        return None
    proc = subprocess.run(
        ["tmux", "display-message", "-p", "#{session_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    name = proc.stdout.strip()
    return name or None
