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

from config import get_code_root
from startup_queue import clear_startup_requests, read_startup_cleanup

DEFAULT_COMMAND = f"python3 {shlex.quote(str(get_code_root() / 'orin_kernel_autopilot.py'))}"
ORIN_VCMUXER_COMMAND = (
    f"python3 {shlex.quote(str(get_code_root() / 'tools' / 'orin_virtioso_mux_autopilot_wrapper.py'))}"
)
DEFAULT_TMUX_SESSION = "autopilot"
DEFAULT_PLATFORM = "orin-agx-uefi-netboot"
PLATFORM_DEFAULT_TTYS = {
    "orin-agx-uefi-netboot": ("/dev/ttyACM0", "/dev/ttyACM1"),
}


def platform_requires_ttys(platform: str) -> bool:
    normalized = (platform or "").strip()
    return normalized not in {"qemu-generic"}


def default_command_for_platform(platform: str) -> str:
    if (platform or "").strip() == "orin-agx-uefi-netboot":
        return ORIN_VCMUXER_COMMAND
    return DEFAULT_COMMAND


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


def _console_router_status_path(runtime_dir: Path) -> Path:
    return runtime_dir / "virtioso_mux_wrapper" / "status.json"


def _read_console_router_status(runtime_dir: Path) -> dict | None:
    try:
        return json.loads(_console_router_status_path(runtime_dir).read_text())
    except Exception:
        return None


def _append_runtime_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().isoformat() + "Z"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[manager {timestamp}] {message}\n")


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
        if token.endswith("orin_virtioso_mux_autopilot_wrapper.py"):
            return "orin_virtioso_mux_autopilot_wrapper.py"
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


def _tmux_pane_pids(session: str) -> list[int]:
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-t", session, "-F", "#{pane_pid}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except Exception:
            continue
    return pids


def _tmux_pane_pid(session: str) -> Optional[int]:
    pids = _tmux_pane_pids(session)
    if not pids:
        return None
    return pids[0]


def _tmux_worker_pane_and_pid(session: str, marker: str) -> tuple[Optional[int], Optional[int]]:
    for pane_pid in _tmux_pane_pids(session):
        if _is_running(pane_pid, marker):
            return pane_pid, pane_pid
        child_pid = _find_child_pid(pane_pid, marker)
        if child_pid:
            return pane_pid, child_pid
    return None, None


def _child_pids(parent_pid: int) -> list[int]:
    result = subprocess.run(
        ["pgrep", "-P", str(parent_pid)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except Exception:
            continue
    return pids


def _find_child_pid(parent_pid: int, marker: str) -> Optional[int]:
    pending = list(_child_pids(parent_pid))
    seen: set[int] = set()
    while pending:
        child_pid = pending.pop(0)
        if child_pid in seen:
            continue
        seen.add(child_pid)
        cmdline = _read_cmdline(child_pid)
        if _cmdline_matches(cmdline, marker):
            return child_pid
        pending.extend(_child_pids(child_pid))
    return None


def _resolve_pid_from_tmux(session: str, marker: str) -> Optional[int]:
    _, worker_pid = _tmux_worker_pane_and_pid(session, marker)
    return worker_pid


def _tmux_session_status(session: str, marker: str) -> dict:
    exists = _tmux_has_session(session)
    pane_pid, worker_pid = _tmux_worker_pane_and_pid(session, marker) if exists else (None, None)
    if pane_pid is None and exists:
        pane_pid = _tmux_pane_pid(session)
    return {
        "name": session,
        "exists": exists,
        "pane_pid": pane_pid,
        "pane_cmdline": _read_cmdline(pane_pid) if pane_pid else [],
        "worker_pid": worker_pid,
        "worker_cmdline": _read_cmdline(worker_pid) if worker_pid else [],
    }


def _write_meta(meta_path: Path, data: dict) -> None:
    meta_path.write_text(json.dumps(data, indent=2))


def _read_meta(meta_path: Path) -> dict:
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text())
    except Exception:
        return {}


def _require_non_empty_tty(name: str, value: Optional[str], fallback: str) -> str:
    if value is None:
        candidate = fallback
    else:
        candidate = value.strip()
    if not candidate:
        raise ValueError(f"{name} must be non-empty")
    return candidate


def _resolve_ttys(platform: str, tty0: Optional[str], tty1: Optional[str]) -> tuple[str, str]:
    default_tty0, default_tty1 = PLATFORM_DEFAULT_TTYS.get(platform, ("", ""))
    return (
        _require_non_empty_tty("tty0", tty0, default_tty0),
        _require_non_empty_tty("tty1", tty1, default_tty1),
    )


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
    pid_running = bool(pid and _is_running(pid, marker))
    tmux_status = _tmux_session_status(session, marker) if use_tmux else {
        "name": None,
        "exists": False,
        "pane_pid": None,
        "pane_cmdline": [],
        "worker_pid": None,
        "worker_cmdline": [],
    }

    worker_pid = pid if pid_running else tmux_status.get("worker_pid")
    running = bool(worker_pid and _is_running(worker_pid, marker))
    api_health = {"status": "ok"}
    if use_tmux and tmux_status.get("exists") and not running:
        api_health = {
            "status": "inconsistent",
            "reason": "tmux_session_exists_but_worker_not_running",
        }

    return {
        "running": running,
        "pid": worker_pid,
        "cmdline": _read_cmdline(worker_pid) if worker_pid else [],
        "worker_process": {
            "pid": worker_pid,
            "running": running,
            "cmdline": _read_cmdline(worker_pid) if worker_pid else [],
        },
        "manager_process": {
            "pid_file_pid": pid,
            "pid_file_running": pid_running,
        },
        "tmux": tmux_status if use_tmux else None,
        "tmux_session": session if use_tmux else None,
        "platform": meta.get("platform"),
        "tty0": meta.get("tty0"),
        "tty1": meta.get("tty1"),
        "console_router": _read_console_router_status(runtime),
        "last_start_time": meta.get("start_time"),
        "startup_cleanup": read_startup_cleanup(str(base)),
        "api_health": api_health,
    }


def start_autopilot(
    autopilot_dir: str,
    command: Optional[str] = None,
    use_tmux: bool = True,
    tmux_session: Optional[str] = None,
    tty0: Optional[str] = None,
    tty1: Optional[str] = None,
    platform: Optional[str] = None,
) -> dict:
    base = Path(autopilot_dir)
    runtime = _runtime_dir(base)
    pid_file = _pid_path(runtime)
    meta_path = _meta_path(runtime)
    log_path = _log_path(runtime)

    resolved_platform = (platform or "").strip()
    if not resolved_platform:
        return {"status": "error", "error": "platform must be specified explicitly"}

    effective_command = command or default_command_for_platform(resolved_platform)
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

    stale_pid = _read_pid(pid_file)
    if stale_pid is not None and not _is_running(stale_pid, marker) and pid_file.exists():
        pid_file.unlink()

    startup_cleanup = clear_startup_requests(str(base), reason="manager_start")
    cleared = startup_cleanup["pending_cleared"] + startup_cleanup["processing_cleared"]
    if cleared:
        _append_runtime_log(
            log_path,
            "Cleared startup requests by policy "
            f"{startup_cleanup['policy']}: {', '.join(cleared)}",
        )
    elif stale_pid is not None and not _is_running(stale_pid, marker):
        _append_runtime_log(
            log_path,
            f"Detected stale daemon pid={stale_pid}; no pending/processing requests needed cleanup",
        )

    if use_tmux and shutil_which("tmux") is None:
        return {"status": "error", "error": "tmux not found in PATH"}

    env = os.environ.copy()
    env["AUTOPILOT_DIR"] = str(base)
    env["AUTOPILOT_TMUX_SESSION"] = session

    if platform_requires_ttys(resolved_platform):
        resolved_tty0, resolved_tty1 = _resolve_ttys(resolved_platform, tty0, tty1)
        env["AUTOPILOT_TTY0"] = resolved_tty0
        env["AUTOPILOT_TTY1"] = resolved_tty1
    else:
        resolved_tty0 = None
        resolved_tty1 = None
        env.pop("AUTOPILOT_TTY0", None)
        env.pop("AUTOPILOT_TTY1", None)
    env["AUTOPILOT_PLATFORM"] = resolved_platform

    if use_tmux:
        if _tmux_has_session(session):
            _tmux_kill_session(session)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session],
            check=True,
            env=env,
        )
        # Track desired environment inside tmux for observability, and launch
        # command with explicit env assignments so the existing shell receives
        # the variables even when tmux server state is stale.
        subprocess.run(["tmux", "set-environment", "-t", session, "AUTOPILOT_DIR", str(base)], check=False)
        subprocess.run(["tmux", "set-environment", "-t", session, "AUTOPILOT_TMUX_SESSION", session], check=False)
        if resolved_tty0 is not None:
            subprocess.run(["tmux", "set-environment", "-t", session, "AUTOPILOT_TTY0", env["AUTOPILOT_TTY0"]], check=False)
        else:
            subprocess.run(["tmux", "set-environment", "-t", session, "-u", "AUTOPILOT_TTY0"], check=False)
        if resolved_tty1 is not None:
            subprocess.run(["tmux", "set-environment", "-t", session, "AUTOPILOT_TTY1", env["AUTOPILOT_TTY1"]], check=False)
        else:
            subprocess.run(["tmux", "set-environment", "-t", session, "-u", "AUTOPILOT_TTY1"], check=False)
        subprocess.run(["tmux", "set-environment", "-t", session, "AUTOPILOT_PLATFORM", env["AUTOPILOT_PLATFORM"]], check=False)
        _configure_tmux_ui(session, base)
        env_prefix_parts = [
            f"AUTOPILOT_DIR={shlex.quote(str(base))}",
            f"AUTOPILOT_TMUX_SESSION={shlex.quote(session)}",
        ]
        if resolved_tty0 is not None:
            env_prefix_parts.append(f"AUTOPILOT_TTY0={shlex.quote(env['AUTOPILOT_TTY0'])}")
        if resolved_tty1 is not None:
            env_prefix_parts.append(f"AUTOPILOT_TTY1={shlex.quote(env['AUTOPILOT_TTY1'])}")
        env_prefix_parts.append(f"AUTOPILOT_PLATFORM={shlex.quote(env['AUTOPILOT_PLATFORM'])}")
        command_str = " ".join(
            env_prefix_parts + [shlex.join(shlex.split(effective_command))]
        )
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

    console_router = None
    if marker == "orin_virtioso_mux_autopilot_wrapper.py":
        deadline = time.time() + 3.0
        while time.time() < deadline:
            console_router = _read_console_router_status(runtime)
            if console_router:
                break
            time.sleep(0.1)

    meta = {
        "command": effective_command,
        "marker": marker,
        "tmux_session": session,
        "use_tmux": use_tmux,
        "platform": env.get("AUTOPILOT_PLATFORM"),
        "tty0": resolved_tty0,
        "tty1": resolved_tty1,
        "pid": pid,
        "start_time": datetime.utcnow().isoformat() + "Z",
        "log_path": str(log_path),
    }
    if console_router:
        meta["console_router"] = console_router
    _write_meta(meta_path, meta)

    return {
        "status": "started",
        "pid": pid,
        "tmux_session": session if use_tmux else None,
        "attach_hint": f"tmux attach -t {session}" if use_tmux else "",
        "log_path": str(log_path),
        "tty0": resolved_tty0,
        "tty1": resolved_tty1,
        "console_router": console_router,
        "startup_cleanup": startup_cleanup,
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
    tty0: Optional[str] = None,
    tty1: Optional[str] = None,
    platform: Optional[str] = None,
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
        tty0=tty0,
        tty1=tty1,
        platform=platform,
    )


def shutil_which(binary: str) -> Optional[str]:
    from shutil import which

    return which(binary)
