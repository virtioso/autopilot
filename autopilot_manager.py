#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import DEFAULT_TTY0, DEFAULT_TTY1

DEFAULT_COMMAND = "python3 /home/hlyytine/autopilot/orin_kernel_autopilot.py"
DEFAULT_TMUX_SESSION = "autopilot"


def _runtime_dir(autopilot_dir: Path) -> Path:
    runtime = autopilot_dir / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


def _pid_path(runtime_dir: Path) -> Path:
    return runtime_dir / "autopilot.pid"


def _meta_path(runtime_dir: Path) -> Path:
    return runtime_dir / "autopilot.meta.json"


def _log_path(runtime_dir: Path) -> Path:
    return runtime_dir / "autopilot.log"


def _read_pid(pid_path: Path) -> Optional[int]:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except Exception:
        return None


def _read_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except Exception:
        return []
    if not raw:
        return []
    return [part.decode(errors="ignore") for part in raw.split(b"\x00") if part]


def _cmdline_matches(cmdline: list[str], marker: str) -> bool:
    if not cmdline:
        return False
    return any(marker in token for token in cmdline)


def _command_marker(command: str) -> str:
    tokens = shlex.split(command)
    for token in tokens:
        if token.endswith("orin_kernel_autopilot.py"):
            return "orin_kernel_autopilot.py"
    if tokens:
        return Path(tokens[0]).name
    return "orin_kernel_autopilot.py"


def _is_running(pid: int, marker: str) -> bool:
    if pid <= 0:
        return False
    cmdline = _read_cmdline(pid)
    return _cmdline_matches(cmdline, marker)


def _tmux_has_session(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _tmux_kill_session(session: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _configure_tmux_ui(session: str, autopilot_dir: Path) -> None:
    code_dir = Path(__file__).resolve().parent
    status_script = code_dir / "scripts" / "autopilot_tmux_status.py"
    abort_script = code_dir / "scripts" / "autopilot_tmux_abort.py"
    quoted_dir = shlex.quote(str(autopilot_dir))
    status_cmd = f"#(python3 {shlex.quote(str(status_script))} --autopilot-dir {quoted_dir})"
    abort_cmd = f"python3 {shlex.quote(str(abort_script))} --autopilot-dir {quoted_dir}"

    subprocess.run(["tmux", "set-option", "-t", session, "status", "on"], check=False)
    subprocess.run(["tmux", "set-option", "-t", session, "status-interval", "1"], check=False)
    subprocess.run(["tmux", "set-option", "-t", session, "status-right", status_cmd], check=False)
    subprocess.run(
        [
            "tmux",
            "bind-key",
            "-T",
            "prefix",
            "r",
            "if-shell",
            "-F",
            f"#{{==:#{{session_name}},{session}}}",
            f"run-shell {shlex.quote(abort_cmd)}",
            "send-keys r",
        ],
        check=False,
    )


def _tmux_pane_pid(session: str) -> Optional[int]:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return int(lines[0])
    except Exception:
        return None


def _find_child_pid(parent_pid: int, marker: str) -> Optional[int]:
    result = subprocess.run(
        ["pgrep", "-P", str(parent_pid)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        try:
            child_pid = int(line.strip())
        except Exception:
            continue
        cmdline = _read_cmdline(child_pid)
        if _cmdline_matches(cmdline, marker):
            return child_pid
    return None


def _resolve_pid_from_tmux(session: str, marker: str) -> Optional[int]:
    pane_pid = _tmux_pane_pid(session)
    if pane_pid is None:
        return None
    if _is_running(pane_pid, marker):
        return pane_pid
    child_pid = _find_child_pid(pane_pid, marker)
    if child_pid:
        return child_pid
    return pane_pid


def _write_meta(meta_path: Path, data: dict) -> None:
    meta_path.write_text(json.dumps(data, indent=2))


def _read_meta(meta_path: Path) -> dict:
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text())
    except Exception:
        return {}


def status_autopilot(
    autopilot_dir: str,
    command: Optional[str] = None,
    tmux_session: Optional[str] = None,
) -> dict:
    base = Path(autopilot_dir)
    runtime = _runtime_dir(base)
    pid_file = _pid_path(runtime)
    meta_path = _meta_path(runtime)
    meta = _read_meta(meta_path)

    effective_command = command or meta.get("command") or DEFAULT_COMMAND
    marker = meta.get("marker") or _command_marker(effective_command)
    session = tmux_session or meta.get("tmux_session") or DEFAULT_TMUX_SESSION
    use_tmux = bool(meta.get("use_tmux", True))

    pid = _read_pid(pid_file)
    running = bool(pid and _is_running(pid, marker))

    if not running and use_tmux and _tmux_has_session(session):
        tmux_pid = _resolve_pid_from_tmux(session, marker)
        if tmux_pid and _is_running(tmux_pid, marker):
            pid = tmux_pid
            running = True

    return {
        "running": running,
        "pid": pid,
        "cmdline": _read_cmdline(pid) if pid else [],
        "tmux_session": session if use_tmux else None,
        "last_start_time": meta.get("start_time"),
    }


def start_autopilot(
    autopilot_dir: str,
    command: Optional[str] = None,
    use_tmux: bool = True,
    tmux_session: Optional[str] = None,
) -> dict:
    base = Path(autopilot_dir)
    runtime = _runtime_dir(base)
    pid_file = _pid_path(runtime)
    meta_path = _meta_path(runtime)
    log_path = _log_path(runtime)

    effective_command = command or DEFAULT_COMMAND
    marker = _command_marker(effective_command)
    session = tmux_session or DEFAULT_TMUX_SESSION

    current = status_autopilot(autopilot_dir, effective_command, session)
    if current["running"]:
        return {
            "status": "already_running",
            "pid": current["pid"],
            "tmux_session": current["tmux_session"],
            "attach_hint": f"tmux attach -t {current['tmux_session']}" if use_tmux else "",
            "log_path": str(log_path),
        }

    if use_tmux and shutil_which("tmux") is None:
        return {"status": "error", "error": "tmux not found in PATH"}

    env = os.environ.copy()
    env["AUTOPILOT_DIR"] = str(base)
    # Orin AGX-specific defaults. Replace for other platforms.
    env.setdefault("AUTOPILOT_TTY0", DEFAULT_TTY0)
    env.setdefault("AUTOPILOT_TTY1", DEFAULT_TTY1)

    if use_tmux:
        if _tmux_has_session(session):
            _tmux_kill_session(session)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session],
            check=True,
            env=env,
        )
        # Ensure session has required environment (tmux sessions do not inherit
        # the client environment by default).
        subprocess.run(["tmux", "set-environment", "-t", session, "AUTOPILOT_DIR", str(base)], check=False)
        subprocess.run(["tmux", "set-environment", "-t", session, "AUTOPILOT_TTY0", env["AUTOPILOT_TTY0"]], check=False)
        subprocess.run(["tmux", "set-environment", "-t", session, "AUTOPILOT_TTY1", env["AUTOPILOT_TTY1"]], check=False)
        _configure_tmux_ui(session, base)
        command_str = " ".join(shlex.quote(part) for part in shlex.split(effective_command))
        subprocess.run(
            ["tmux", "send-keys", "-t", session, command_str, "C-m"],
            check=True,
            env=env,
        )
        subprocess.run(
            ["tmux", "pipe-pane", "-t", session, "-o", f"cat >> {shlex.quote(str(log_path))}"],
            check=False,
            env=env,
        )
        pid = None
        for _ in range(10):
            pid = _resolve_pid_from_tmux(session, marker)
            if pid:
                break
            time.sleep(0.2)
    else:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("a")
        proc = subprocess.Popen(
            shlex.split(effective_command),
            stdout=log_file,
            stderr=log_file,
            env=env,
            start_new_session=True,
        )
        pid = proc.pid

    if pid:
        pid_file.write_text(str(pid))

    meta = {
        "command": effective_command,
        "marker": marker,
        "tmux_session": session,
        "use_tmux": use_tmux,
        "pid": pid,
        "start_time": datetime.utcnow().isoformat() + "Z",
        "log_path": str(log_path),
    }
    _write_meta(meta_path, meta)

    return {
        "status": "started",
        "pid": pid,
        "tmux_session": session if use_tmux else None,
        "attach_hint": f"tmux attach -t {session}" if use_tmux else "",
        "log_path": str(log_path),
    }


def stop_autopilot(
    autopilot_dir: str,
    force: bool = False,
    command: Optional[str] = None,
    tmux_session: Optional[str] = None,
) -> dict:
    base = Path(autopilot_dir)
    runtime = _runtime_dir(base)
    pid_file = _pid_path(runtime)
    meta_path = _meta_path(runtime)
    meta = _read_meta(meta_path)

    effective_command = command or meta.get("command") or DEFAULT_COMMAND
    marker = meta.get("marker") or _command_marker(effective_command)
    session = tmux_session or meta.get("tmux_session") or DEFAULT_TMUX_SESSION
    use_tmux = bool(meta.get("use_tmux", True))

    pid = _read_pid(pid_file)
    running = bool(pid and _is_running(pid, marker))

    if not running and use_tmux and _tmux_has_session(session):
        tmux_pid = _resolve_pid_from_tmux(session, marker)
        if tmux_pid and _is_running(tmux_pid, marker):
            pid = tmux_pid
            running = True

    if not running:
        if use_tmux and _tmux_has_session(session):
            _tmux_kill_session(session)
        if pid_file.exists():
            pid_file.unlink()
        return {"status": "not_running", "pid": pid}

    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not _is_running(pid, marker):
            running = False
            break
        time.sleep(0.1)

    if running and force:
        os.kill(pid, signal.SIGKILL)
        for _ in range(20):
            if not _is_running(pid, marker):
                running = False
                break
            time.sleep(0.1)

    if use_tmux and _tmux_has_session(session):
        _tmux_kill_session(session)

    if pid_file.exists():
        pid_file.unlink()

    return {
        "status": "stopped" if not running else "still_running",
        "pid": pid,
    }


def restart_autopilot(
    autopilot_dir: str,
    command: Optional[str] = None,
    use_tmux: bool = True,
    tmux_session: Optional[str] = None,
    force: bool = False,
) -> dict:
    stop_autopilot(
        autopilot_dir=autopilot_dir,
        force=force,
        command=command,
        tmux_session=tmux_session,
    )
    return start_autopilot(
        autopilot_dir=autopilot_dir,
        command=command,
        use_tmux=use_tmux,
        tmux_session=tmux_session,
    )


def shutil_which(binary: str) -> Optional[str]:
    from shutil import which

    return which(binary)
