from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional


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
            "step": None,
            "elapsed_s": None,
            "status_text": "",
            "source_map": {},
            "window_map": {},
        }
        self.ui_dir.mkdir(parents=True, exist_ok=True)
        self.live_dir.mkdir(parents=True, exist_ok=True)
        self._flush_locked()

    def set_request(self, request_id: str, profile: str, chain: str) -> None:
        with self._lock:
            self._state["request_id"] = request_id
            self._state["profile"] = profile
            self._state["chain"] = chain
            self._flush_locked()

    def clear_request(self) -> None:
        with self._lock:
            self._state["request_id"] = None
            self._state["profile"] = None
            self._state["chain"] = None
            self._state["step"] = None
            self._state["elapsed_s"] = None
            self._flush_locked()

    def set_step(self, step: str, elapsed_s: int) -> None:
        with self._lock:
            self._state["step"] = step
            self._state["elapsed_s"] = elapsed_s
            self._flush_locked()

    def set_status_text(self, text: str) -> None:
        with self._lock:
            self._state["status_text"] = text
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
                ["tmux", "new-window", "-d", "-t", target, "-n", title],
                check=False,
            )
        subprocess.run(["tmux", "rename-window", "-t", target, title], check=False)
        subprocess.run(["tmux", "respawn-pane", "-k", "-t", target, command], check=False)

    def ensure_pane_window(
        self,
        window: int,
        title: str,
        panes: List[dict],
        layout: str = "tiled",
        status_rows: int = 0,
    ) -> None:
        if not panes:
            return
        target = f"{self.session}:{window}"
        if self._window_exists(window):
            subprocess.run(["tmux", "kill-window", "-t", target], check=False)
        subprocess.run(
            ["tmux", "new-window", "-d", "-t", target, "-n", title, panes[0]["command"]],
            check=False,
        )
        subprocess.run(["tmux", "rename-window", "-t", target, title], check=False)
        subprocess.run(["tmux", "select-pane", "-t", f"{target}.0", "-T", panes[0]["title"]], check=False)
        for index, pane in enumerate(panes[1:], start=1):
            subprocess.run(["tmux", "split-window", "-t", target, "-v", pane["command"]], check=False)
            subprocess.run(["tmux", "select-pane", "-t", f"{target}.{index}", "-T", pane["title"]], check=False)
        if layout == "status-top":
            subprocess.run(["tmux", "select-layout", "-t", target, "even-vertical"], check=False)
            if status_rows > 0:
                subprocess.run(["tmux", "resize-pane", "-t", f"{target}.0", "-y", str(status_rows)], check=False)
        else:
            subprocess.run(["tmux", "select-layout", "-t", target, "tiled"], check=False)
        subprocess.run(["tmux", "set-window-option", "-t", target, "pane-border-status", "top"], check=False)

    def bind_status_command(self, command: str) -> None:
        subprocess.run(["tmux", "set-option", "-t", self.session, "status", "on"], check=False)
        subprocess.run(["tmux", "set-option", "-t", self.session, "status-right", command], check=False)

    def bind_abort_key(self, key: str, command: str) -> None:
        subprocess.run(
            ["tmux", "bind-key", "-T", "prefix", key, "run-shell", command],
            check=False,
        )

    def clear_window_history(self, window: int) -> None:
        target = f"{self.session}:{window}"
        if not self._window_exists(window):
            return
        subprocess.run(["tmux", "clear-history", "-t", target], check=False)

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
    explicit_session = os.environ.get("AUTOPILOT_TMUX_SESSION", "").strip()
    if explicit_session:
        return explicit_session
    if os.environ.get("AUTOPILOT_ALLOW_INHERITED_TMUX", "").strip() != "1":
        return None
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


class TmuxUICompat:
    """Compatibility shim while chain runtime still expects a TUI-like object."""

    def __init__(self, state: TmuxUIState, windows: Optional[TmuxWindowManager] = None):
        self.state = state
        self.windows = windows
        self.enabled = True
        self.active_window = 1
        self.window_map: Dict[int, str] = {}
        self.interactive_enabled = False
        self.status_text = ""

    def _console_command(self, source: str, log_path: Optional[str] = None, read_only: bool = False) -> str:
        script = Path(__file__).resolve().parent / "scripts" / "autopilot_tmux_console.py"
        cmd = (
            f"python3 {shlex.quote(str(script))} "
            f"--autopilot-dir {shlex.quote(str(self.state.autopilot_dir))} "
            f"--source {shlex.quote(source)}"
        )
        if log_path:
            cmd += f" --log-path {shlex.quote(str(log_path))}"
        if read_only:
            cmd += " --read-only"
        return cmd

    def _status_command(self) -> str:
        script = Path(__file__).resolve().parent / "scripts" / "autopilot_tmux_status.py"
        log_path = self.state.autopilot_dir / "runtime" / "autopilot.log"
        autopilot_dir = shlex.quote(str(self.state.autopilot_dir))
        script_path = shlex.quote(str(script))
        log_path_arg = shlex.quote(str(log_path))
        return (
            "while true; do "
            "printf '\\033[2J\\033[H'; "
            f"python3 {script_path} --autopilot-dir {autopilot_dir}; "
            "printf '\\n\\n--- autopilot.log ---\\n'; "
            f"if test -f {log_path_arg}; then tail -n 18 {log_path_arg}; "
            "else printf 'autopilot.log not available\\n'; fi; "
            "sleep 1; "
            "done"
        )

    def start(self, event_queue) -> None:
        _ = event_queue

    def stop(self) -> None:
        return

    def set_input_handler(self, handler) -> None:
        _ = handler

    def emit_output(self, source: str, data: bytes) -> None:
        _ = source
        _ = data

    def handle_event(self, event) -> None:
        _ = event

    def bind_window(self, window: int, source: str, title: Optional[str] = None) -> None:
        # map_window is backward-compatible metadata + tmux window binding.
        self.window_map[window] = source
        self.state.map_window(window, source, title=title)
        if self.windows:
            try:
                self.windows.ensure_window(window, title or source, self._console_command(source))
            except Exception:
                # Preserve chain compatibility even if tmux window operations fail.
                pass

    def bind_source_panes(
        self,
        window: int,
        title: str,
        panes: List[dict],
        include_status_pane: bool = False,
        status_title: str = "Autopilot",
        layout: str = "tiled",
        status_rows: int = 0,
    ) -> None:
        self.state.map_window(window, "router_sessions", title=title)
        if not self.windows:
            return
        commands = []
        if include_status_pane:
            commands.append({
                "source": "autopilot",
                "title": status_title,
                "command": self._status_command(),
            })
        for pane in panes:
            source = str(pane.get("source", ""))
            if not source:
                continue
            commands.append({
                "source": source,
                "title": str(pane.get("title") or source),
                "command": self._console_command(
                    source,
                    log_path=pane.get("log_path"),
                    read_only=bool(pane.get("read_only", False)),
                ),
            })
        if not commands:
            return
        try:
            self.windows.ensure_pane_window(
                window,
                title,
                commands,
                layout=layout,
                status_rows=status_rows,
            )
        except Exception:
            pass

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.state.set_status_text(text)

    def clear_source_windows(self, sources: list[str]) -> None:
        if not self.windows:
            return
        wanted = set(str(source) for source in sources)
        if not wanted:
            return
        for window, source in list(self.window_map.items()):
            if source in wanted:
                try:
                    self.windows.clear_window_history(window)
                except Exception:
                    pass
