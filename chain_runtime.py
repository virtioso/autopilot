import json
import os
import queue
import re
import shlex
import signal
import subprocess
import threading
import time
import shutil
import hashlib
import fnmatch
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import serial

from console_sessions import load_profile
from tty_match import AnsiCsiStripper, normalize_tty_bytes, normalize_tty_text, to_raw_offset


class ChainValidationError(Exception):
    pass


class AbortRun(Exception):
    pass


class CancelRun(Exception):
    pass


@dataclass
class OutcomeMatch:
    label: str
    next_step: str
    pattern: Optional[str]
    source: Optional[str]
    log_path: Optional[str]
    log_offset: Optional[int]


@dataclass
class StepResult:
    step: str
    status: str
    outcome: Optional[OutcomeMatch]
    error_code: Optional[str]
    error_message: Optional[str]
    started_at: float
    finished_at: float
    chain_name: str
    chain_stack: List[str]
    source_ranges: Dict[str, dict]


class ChainRecorder:
    def __init__(self, result_dir: Path, filename: str = "chain.json"):
        self.result_dir = result_dir
        self.steps: List[dict] = []
        self.parallel_groups: Dict[str, dict] = {}
        self.overall_status: Optional[str] = None
        self.test_verdict: Optional[str] = None
        self.workflow_state: str = "running"
        self.abort_reason: Optional[str] = None
        self.path = result_dir / filename
        self._lock = threading.Lock()

    def record_step(self, result: StepResult) -> None:
        with self._lock:
            entry = {
                "step": result.step,
                "status": result.status,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "error_code": result.error_code,
                "error_message": result.error_message,
                "chain_name": result.chain_name,
                "chain_stack": result.chain_stack,
            }
            if result.outcome:
                entry.update({
                    "outcome_label": result.outcome.label,
                    "next_step": result.outcome.next_step,
                    "pattern": result.outcome.pattern,
                    "source": result.outcome.source,
                    "log_path": result.outcome.log_path,
                    "log_offset": result.outcome.log_offset,
                })
            entry["source_ranges"] = result.source_ranges
            self.steps.append(entry)
            self._flush_unlocked()

    def record_parallel_group(self, name: str, state: dict) -> None:
        with self._lock:
            self.parallel_groups[name] = {
                "updated_at": time.time(),
                **state,
            }
            self._flush_unlocked()

    def finalize(self, status: str, abort_reason: Optional[str] = None) -> None:
        with self._lock:
            self.overall_status = status
            if self.test_verdict is None and status in ("pass", "fail"):
                self.test_verdict = status
            if status in ("pass", "fail"):
                self.workflow_state = "completed"
            else:
                self.workflow_state = "failed"
            self.abort_reason = abort_reason
            self._flush_unlocked()

    def set_test_verdict(self, verdict: str) -> None:
        with self._lock:
            self.test_verdict = verdict
            self._flush_unlocked()

    def _flush_unlocked(self) -> None:
        payload = {
            "overall_status": self.overall_status,
            "test_verdict": self.test_verdict,
            "workflow_state": self.workflow_state,
            "abort_reason": self.abort_reason,
            "steps": self.steps,
            "parallel_groups": self.parallel_groups,
        }
        self.path.write_text(json.dumps(payload, indent=2))


class NoopChainRecorder:
    def record_step(self, result: StepResult) -> None:
        return

    def record_parallel_group(self, name: str, state: dict) -> None:
        return

    def finalize(self, status: str, abort_reason: Optional[str] = None) -> None:
        return

    def set_test_verdict(self, verdict: str) -> None:
        return


class Event:
    def __init__(self, kind: str, payload: Optional[dict] = None):
        self.kind = kind
        self.payload = payload or {}


FORBIDDEN_PREPARE_LIFECYCLE_PATTERNS = (
    "task_spawn task=prepare_next_run",
    "task_spawn chain=prepare_next_run_task",
    "signal_set signal=prepare_next_run_go",
    "task_join tasks contains prepare_next_run",
)

QEMU_RND_HELPER_FAIL_PATTERN = re.compile(
    r"(?m)^\s*AUTOPILOT_FAIL: QEMU_RND_HELPER_EXIT_NONZERO rc=[0-9]+\s*$"
)


class SourceBinding:
    def __init__(
        self,
        source: str,
        tty: Optional[str],
        log_path: Path,
        analysis_log_path: Optional[Path] = None,
        live_log_path: Optional[Path] = None,
        baud: int = 115200,
        command: Optional[List[str]] = None,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        emit=None,
        on_data=None,
        own_process_group: bool = True,
    ):
        self.source = source
        self.tty = tty
        self.command = list(command) if command else None
        self.cwd = cwd
        self.env = dict(env) if env else None
        self.on_data = on_data
        self.own_process_group = own_process_group
        self.log_path = log_path
        self.analysis_log_path = analysis_log_path
        self.live_log_path = live_log_path
        self.baud = baud
        self.emit = emit
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._buffer = bytearray()
        self._base_offset = 0
        self._total_bytes = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._analysis_sanitizer = AnsiCsiStripper()
        self._serial = None
        self._proc = None
        self._proc_stdin = None
        self._proc_stdout = None
        self._write_fd = None
        self._write_tty_path = None
        self._write_path_resolver = None
        self._mirror_lock = threading.Lock()
        self._mirror_path: Optional[Path] = None
        if self.tty:
            self._serial = serial.Serial(self.tty, baudrate=self.baud, timeout=0.1)
            # Always start from an empty UART state for deterministic pattern matching.
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        elif self.command:
            self._proc = subprocess.Popen(
                self.command,
                cwd=str(self.cwd) if self.cwd else None,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                start_new_session=self.own_process_group,
            )
            self._proc_stdin = self._proc.stdin
            self._proc_stdout = self._proc.stdout
        else:
            raise ValueError("source binding requires tty or command")
        self._thread.start()

    def set_write_path_resolver(self, resolver) -> None:
        self._write_path_resolver = resolver

    def set_mirror_path(self, mirror_path: Optional[Path]) -> None:
        with self._mirror_lock:
            self._mirror_path = mirror_path

    def _write_mirror(self, data: bytes) -> None:
        with self._mirror_lock:
            mirror_path = self._mirror_path
        if not mirror_path:
            return
        try:
            with mirror_path.open("ab", buffering=0) as mirror_file:
                mirror_file.write(data)
        except Exception:
            return

    def _ensure_write_endpoint(self) -> None:
        if self._serial is not None or self._write_fd is not None:
            return
        if self._write_path_resolver is None:
            return
        tty_path = self._write_path_resolver()
        if not tty_path:
            return
        fd = os.open(str(tty_path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self._write_fd = fd
        self._write_tty_path = str(tty_path)

    def _run(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.analysis_log_path:
            self.analysis_log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.live_log_path:
            self.live_log_path.parent.mkdir(parents=True, exist_ok=True)
        live_ctx = open(self.live_log_path, "ab", buffering=0) if self.live_log_path else nullcontext()
        analysis_ctx = open(self.analysis_log_path, "ab", buffering=0) if self.analysis_log_path else nullcontext()
        with open(self.log_path, "ab", buffering=0) as f, analysis_ctx as analysis_file, live_ctx as live_file:
            while not self._stop.is_set():
                try:
                    if self._serial is not None:
                        data = self._serial.read(1024)
                    else:
                        if self._proc_stdout is None:
                            break
                        data = self._proc_stdout.read(1024)
                except Exception:
                    time.sleep(0.1)
                    continue
                if not data:
                    if self._proc is not None and self._proc.poll() is not None:
                        break
                    continue
                if self.emit:
                    self.emit(self.source, data)
                if self.on_data:
                    self.on_data(data)
                f.write(data)
                self._write_mirror(data)
                if analysis_file:
                    analysis_file.write(self._analysis_sanitizer.sanitize(data))
                if live_file:
                    live_file.write(data)
                with self._lock:
                    self._buffer.extend(data)
                    self._total_bytes += len(data)
                    max_buf = 1024 * 1024
                    if len(self._buffer) > max_buf:
                        trim = len(self._buffer) - max_buf
                        del self._buffer[:trim]
                        self._base_offset += trim
            if self._proc is not None:
                rc = self._proc.poll()
                if rc is None:
                    try:
                        rc = self._proc.wait(timeout=1.0)
                    except Exception:
                        rc = None
                if rc not in (None, 0) and not self._stop.is_set():
                    marker = f"\nAUTOPILOT_FAIL: PROCESS_EXIT_NONZERO rc={rc}\n".encode("utf-8")
                    if self.emit:
                        self.emit(self.source, marker)
                    f.write(marker)
                    self._write_mirror(marker)
                    if analysis_file:
                        analysis_file.write(self._analysis_sanitizer.sanitize(marker))
                    if live_file:
                        live_file.write(marker)
                    with self._lock:
                        self._buffer.extend(marker)
                        self._total_bytes += len(marker)

    def write(self, text: str, chunk_size: int = 0, chunk_delay_s: float = 0.0) -> None:
        self.write_bytes(text.encode("utf-8", errors="ignore"), chunk_size=chunk_size, chunk_delay_s=chunk_delay_s)

    def _write_bytes_once(self, payload: bytes) -> None:
        if self._serial is not None:
            self._serial.write(payload)
            return
        self._ensure_write_endpoint()
        if self._write_fd is not None:
            os.write(self._write_fd, payload)
            return
        if self._proc_stdin is None:
            raise RuntimeError("process stdin not available")
        self._proc_stdin.write(payload)
        self._proc_stdin.flush()

    def write_bytes(self, payload: bytes, chunk_size: int = 0, chunk_delay_s: float = 0.0) -> None:
        with self._lock:
            if chunk_size <= 0 or len(payload) <= chunk_size:
                self._write_bytes_once(payload)
                return
            for offset in range(0, len(payload), chunk_size):
                self._write_bytes_once(payload[offset : offset + chunk_size])
                if chunk_delay_s > 0 and offset + chunk_size < len(payload):
                    time.sleep(chunk_delay_s)

    def read_since(self, offset: int) -> Tuple[bytes, int]:
        with self._lock:
            if offset < self._base_offset:
                offset = self._base_offset
            rel = offset - self._base_offset
            data = bytes(self._buffer[rel:])
            new_offset = self._base_offset + len(self._buffer)
        return data, new_offset

    def current_offset(self) -> int:
        with self._lock:
            return self._base_offset + len(self._buffer)

    def stop(self) -> None:
        self._stop.set()
        trailing = self._analysis_sanitizer.flush()
        if trailing and self.analysis_log_path:
            try:
                with open(self.analysis_log_path, "ab", buffering=0) as analysis_file:
                    analysis_file.write(trailing)
            except Exception:
                pass
        try:
            if self._serial is not None:
                self._serial.close()
        except Exception:
            pass
        if self._write_fd is not None:
            try:
                os.close(self._write_fd)
            except Exception:
                pass
            self._write_fd = None
        if self._proc is not None:
            try:
                if self._proc_stdin:
                    self._proc_stdin.close()
            except Exception:
                pass
            try:
                if self._proc.poll() is None:
                    if self.own_process_group:
                        os.killpg(self._proc.pid, signal.SIGTERM)
                    else:
                        self._proc.terminate()
                    self._proc.wait(timeout=3.0)
            except Exception:
                try:
                    if self._proc.poll() is None:
                        if self.own_process_group:
                            os.killpg(self._proc.pid, signal.SIGKILL)
                        else:
                            self._proc.kill()
                except Exception:
                    pass
            try:
                if self._proc_stdout:
                    self._proc_stdout.close()
            except Exception:
                pass
        if threading.current_thread() is not self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def purge(self) -> None:
        with self._lock:
            if self._serial is not None:
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()
            self._buffer = bytearray()
            self._base_offset = 0
            self._total_bytes = 0


class SourceManager:
    def __init__(self, result_dir: Path, ui=None, on_fail_marker=None):
        self.result_dir = result_dir
        self.ui = ui
        self.on_fail_marker = on_fail_marker
        self.sources: Dict[str, SourceBinding] = {}
        self.tty_to_source: Dict[str, str] = {}
        self._marker_windows: Dict[str, str] = {}
        self._reported_fail_markers: set[Tuple[str, str]] = set()
        self._marker_window_max = 8192
        self._router_session_cache: Dict[str, dict] = {}
        self._router_sessions_lock = threading.Lock()
        self._per_result_mirrors: Dict[str, Path] = {}

    def set_result_dir(self, result_dir: Path) -> None:
        self.result_dir = result_dir
        self._marker_windows.clear()
        self._reported_fail_markers.clear()
        self._router_session_cache.clear()
        self._configure_per_result_mirrors()
        self._link_external_router_runtime()

    def _configure_per_result_mirrors(self) -> None:
        previous = set(self._per_result_mirrors)
        self._per_result_mirrors.clear()
        for source in previous:
            binding = self.sources.get(source)
            if binding:
                binding.set_mirror_path(None)
        if not os.environ.get("AUTOPILOT_PHYSICAL_TTY0"):
            return
        mirror_path = self.result_dir / "console" / "raw_ccplex.txt"
        binding = self.sources.get("tty0")
        try:
            mirror_path.parent.mkdir(parents=True, exist_ok=True)
            if mirror_path.is_symlink():
                mirror_path.unlink()
            if mirror_path.exists() and mirror_path.is_dir():
                shutil.rmtree(mirror_path)
            mirror_path.write_bytes(b"")
            if binding:
                binding.set_mirror_path(mirror_path)
            self._per_result_mirrors["tty0"] = mirror_path
        except Exception:
            if binding:
                binding.set_mirror_path(None)
            self._per_result_mirrors.pop("tty0", None)

    def _ensure_symlink(self, link_path: Path, target_path: Path) -> None:
        try:
            link_path.parent.mkdir(parents=True, exist_ok=True)
            if link_path.is_symlink():
                if os.readlink(link_path) == str(target_path):
                    return
                link_path.unlink()
            if link_path.exists():
                if link_path.is_dir():
                    shutil.rmtree(link_path)
                else:
                    link_path.unlink()
            link_path.symlink_to(target_path, target_is_directory=target_path.is_dir())
        except Exception:
            return

    def _link_external_router_runtime(self) -> None:
        sessions_path = os.environ.get("AUTOPILOT_CONSOLE_ROUTER_SESSIONS", "").strip()
        logs_dir = os.environ.get("AUTOPILOT_CONSOLE_ROUTER_LOGS_DIR", "").strip()
        raw_log = os.environ.get("AUTOPILOT_CONSOLE_ROUTER_RAW_LOG", "").strip()
        registry_path = os.environ.get("AUTOPILOT_CONSOLE_ROUTER_REGISTRY", "").strip()
        if not sessions_path and not logs_dir and not raw_log and not registry_path:
            return

        runtime_dir = self.result_dir / "console" / "console-runtime"
        if sessions_path:
            self._ensure_symlink(runtime_dir / "sessions.json", Path(sessions_path))
        if logs_dir:
            self._ensure_symlink(runtime_dir / "tcu_muxer_logs", Path(logs_dir))
        if raw_log:
            self._ensure_symlink(runtime_dir / "tcu_muxer.raw.log", Path(raw_log))
        else:
            stale_raw_log = runtime_dir / "tcu_muxer.raw.log"
            try:
                if stale_raw_log.is_symlink():
                    stale_raw_log.unlink()
            except Exception:
                return
        if registry_path:
            self._ensure_symlink(runtime_dir / "console-stream-registry.json", Path(registry_path))

    def map_source(self, source: str, tty: str, log_rel: str, baud: int = 115200) -> None:
        self.unmap_source(source)
        log_path = self.result_dir / log_rel
        analysis_log_path = log_path.with_suffix(".ansi.log")
        live_log_path = None
        if self.ui and hasattr(self.ui, "state"):
            live_log_path = self.ui.state.live_path_for_source(source)
        binding = SourceBinding(
            source,
            tty,
            log_path,
            analysis_log_path=analysis_log_path,
            live_log_path=live_log_path,
            baud=baud,
            emit=self._emit,
        )
        self.sources[source] = binding
        self.tty_to_source[tty] = source
        if source == "tty0":
            self._configure_per_result_mirrors()

    def unmap_source(self, source: str) -> None:
        binding = self.sources.get(source)
        if not binding:
            return
        binding.set_mirror_path(None)
        binding.stop()
        del self.sources[source]
        if binding.tty:
            self.tty_to_source.pop(binding.tty, None)

    def map_command_source(
        self,
        source: str,
        command: List[str],
        log_rel: str,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.unmap_source(source)
        log_path = self.result_dir / log_rel
        analysis_log_path = log_path.with_suffix(".ansi.log")
        live_log_path = None
        if self.ui and hasattr(self.ui, "state"):
            live_log_path = self.ui.state.live_path_for_source(source)
        binding = SourceBinding(
            source,
            None,
            log_path,
            analysis_log_path=analysis_log_path,
            live_log_path=live_log_path,
            command=command,
            cwd=cwd,
            env=env,
            emit=self._emit,
        )
        binding.set_write_path_resolver(lambda source_name=source: self._resolve_router_write_tty(source_name))
        self.sources[source] = binding

    def map_tcu_mux_source(
        self,
        source: str,
        tty: str,
        log_rel: str,
        *,
        tcu_muxer_path: str,
        outer_mode: str = "raw",
        outer_tag: str = "CCPLEX",
        replace_sources: Optional[List[str]] = None,
    ) -> None:
        for replace_source in replace_sources or []:
            self.unmap_source(replace_source)
        if replace_sources:
            time.sleep(0.25)

        console_dir = self.result_dir / "console"
        runtime_dir = console_dir / "console-runtime"
        logs_dir = runtime_dir / "tcu_muxer_logs"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_log = self.result_dir / log_rel
        raw_log = runtime_dir / "tcu_muxer.raw.log"
        sessions_path = runtime_dir / "sessions.json"
        registry_copy_path = runtime_dir / "console-stream-registry.json"
        sessions: Dict[str, dict] = {}
        self._write_router_sessions(sessions_path, sessions)

        pending_mapping_text = ""

        def seed_sessions_from_registry(registry: dict) -> None:
            current = self._read_router_sessions(sessions_path)
            for stream in registry.get("streams", []) or []:
                component = stream.get("component")
                if not component:
                    continue
                name = str(component)
                direction = str(stream.get("direction") or "")
                can_input = direction in ("input", "bidirectional")
                current[name] = {
                    **current.get(name, {}),
                    "session_id": name,
                    "name": name,
                    "kind": "camkes_component_declared",
                    "stream_id": stream.get("stream_id"),
                    "component": name,
                    "component_type": stream.get("type"),
                    "direction": direction,
                    "interfaces": stream.get("interfaces", []),
                    "interactive": stream.get("stream_id") is not None and int(stream.get("stream_id")) >= 0,
                    "can_input": can_input,
                    "read_only": direction == "output",
                    "log_path": str(logs_dir / f"{name}.txt"),
                    "events_path": None,
                    "pty_path": current.get(name, {}).get("pty_path"),
                }
            self._write_router_sessions(sessions_path, current)

        def handle_mapping(data: bytes) -> None:
            nonlocal pending_mapping_text
            text = pending_mapping_text + data.decode("utf-8", errors="replace")
            if "\n" in text:
                *lines, pending_mapping_text = text.split("\n")
            else:
                pending_mapping_text = text
                lines = []
            updated = False
            with self._router_sessions_lock:
                current = self._read_router_sessions(sessions_path)
                for line in lines:
                    line = line.rstrip("\r")
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("event") == "stream_registry":
                        try:
                            registry = json.loads(str(record.get("registry_json") or "{}"))
                        except json.JSONDecodeError:
                            continue
                        registry_copy_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")
                        seed_sessions_from_registry(registry)
                        current = self._read_router_sessions(sessions_path)
                        updated = True
                        continue
                    if record.get("event") != "session_open":
                        continue
                    pty_path = str(record.get("pty_path") or "")
                    name = str(record.get("name") or "")
                    if not pty_path or not name:
                        continue
                    log_path = Path(record.get("log_path") or logs_dir / f"{name}.txt")
                    existing = current.get(name, {})
                    direction = str(existing.get("direction") or "")
                    current[name] = {
                        **existing,
                        "session_id": name,
                        "name": name,
                        "kind": str(record.get("kind") or "unknown"),
                        "stream_id": record.get("stream_id"),
                        "interactive": bool(existing.get("interactive", True)),
                        "can_input": bool(existing.get("can_input", True)),
                        "read_only": bool(existing.get("read_only", direction == "output")),
                        "log_path": str(log_path),
                        "events_path": None,
                        "pty_path": pty_path,
                    }
                    updated = True
                if updated:
                    self._write_router_sessions(sessions_path, current)
                    self._router_session_cache.clear()

        command = [
            tcu_muxer_path,
            "-A",
            "-O",
            outer_mode,
            "-d",
            tty,
            "-s",
            str(logs_dir),
            "-l",
            str(raw_log),
            "-L",
            "-w",
        ]
        if outer_mode == "nvidia-tcu":
            command.extend(["-C", outer_tag])
        self.unmap_source(source)
        binding = SourceBinding(
            source,
            None,
            stdout_log,
            analysis_log_path=stdout_log.with_suffix(".ansi.log"),
            command=command,
            emit=self._emit,
            on_data=handle_mapping,
            own_process_group=False,
        )
        self.sources[source] = binding

    def _read_router_sessions(self, sessions_path: Path) -> Dict[str, dict]:
        try:
            payload = json.loads(sessions_path.read_text())
        except Exception:
            return {}
        return {
            str(sess.get("name")): sess
            for sess in payload.get("sessions", [])
            if sess.get("name")
        }

    def _write_router_sessions(self, sessions_path: Path, sessions: Dict[str, dict]) -> None:
        sessions_path.write_text(json.dumps({
            "version": 1,
            "sessions": [sessions[name] for name in sorted(sessions)],
        }, indent=2) + "\n")

    def _load_router_sessions(self) -> Dict[str, dict]:
        console_dir = self.result_dir / "console"
        sessions_path = console_dir / "console-runtime" / "sessions.json"
        if not sessions_path.exists():
            external_sessions = os.environ.get("AUTOPILOT_CONSOLE_ROUTER_SESSIONS", "").strip()
            if external_sessions:
                sessions_path = Path(external_sessions)
        if not sessions_path.exists():
            return {}
        try:
            sessions = json.loads(sessions_path.read_text())
        except Exception:
            return {}

        by_name = {sess.get("name"): sess for sess in sessions.get("sessions", [])}
        resolved: Dict[str, dict] = {
            str(name): sess
            for name, sess in by_name.items()
            if name
        }
        return resolved

    def router_session_for_source(self, source: str) -> Optional[dict]:
        cached = self._router_session_cache.get(source)
        if cached is not None:
            return cached
        sessions = self._load_router_sessions()
        if sessions:
            self._router_session_cache = sessions
            cached = sessions.get(source)
            if cached is not None:
                return cached
        return self._router_session_cache.get(source)

    def router_sessions(self) -> Dict[str, dict]:
        return self._load_router_sessions()

    def _resolve_router_write_tty(self, source: str) -> Optional[str]:
        sess = self.router_session_for_source(source)
        if not sess:
            return None
        pty_path = sess.get("pty_path")
        if pty_path and os.path.exists(str(pty_path)):
            return str(pty_path)
        return None

    def read_router_since(self, source: str, offset: int) -> Tuple[bytes, int, Optional[str]]:
        sess = self.router_session_for_source(source)
        if not sess:
            return b"", offset, None
        log_path = Path(str(sess.get("log_path", "")))
        if not log_path.exists():
            return b"", offset, str(log_path)
        size = log_path.stat().st_size
        read_offset = min(offset, size)
        with open(log_path, "rb") as f:
            f.seek(read_offset)
            data = f.read()
        return data, read_offset + len(data), str(log_path)

    def write_router_source(
        self,
        source: str,
        payload: bytes,
        chunk_size: int = 0,
        chunk_delay_s: float = 0.0,
    ) -> None:
        router_tty = self._resolve_router_write_tty(source)
        if not router_tty:
            raise ValueError(f"unknown source {source}")
        fd = os.open(router_tty, os.O_RDWR | os.O_NOCTTY)
        try:
            if chunk_size <= 0 or len(payload) <= chunk_size:
                os.write(fd, payload)
            else:
                for offset in range(0, len(payload), chunk_size):
                    os.write(fd, payload[offset : offset + chunk_size])
                    if chunk_delay_s > 0 and offset + chunk_size < len(payload):
                        time.sleep(chunk_delay_s)
        finally:
            os.close(fd)

    def _emit(self, source: str, data: bytes) -> None:
        if self.ui:
            self.ui.emit_output(source, data)
        if not self.on_fail_marker:
            return
        text = normalize_tty_text(data.decode("utf-8", errors="ignore"))
        if not text:
            return
        window = self._marker_windows.get(source, "") + text
        if len(window) > self._marker_window_max:
            window = window[-self._marker_window_max:]
        self._marker_windows[source] = window
        for match in QEMU_RND_HELPER_FAIL_PATTERN.finditer(window):
            marker = match.group(0).strip()
            if not marker:
                continue
            marker_key = (source, marker)
            if marker_key in self._reported_fail_markers:
                continue
            self._reported_fail_markers.add(marker_key)
            try:
                self.on_fail_marker(source, marker)
            except Exception:
                continue

    def get(self, source: str) -> Optional[SourceBinding]:
        return self.sources.get(source)

    def analysis_log_for_source(self, source: str) -> Optional[str]:
        binding = self.sources.get(source)
        if binding and binding.analysis_log_path:
            return str(binding.analysis_log_path)
        router_session = self.router_session_for_source(source)
        if router_session and router_session.get("log_path"):
            return str(router_session["log_path"])
        return None

    def analysis_log_paths(self) -> Dict[str, str]:
        paths: Dict[str, str] = {}
        for source, binding in self.sources.items():
            if binding.analysis_log_path:
                paths[source] = str(binding.analysis_log_path)
        for source, mirror_path in self._per_result_mirrors.items():
            paths[f"{source}_raw_ccplex"] = str(mirror_path)
        for name, session in self._load_router_sessions().items():
            log_path = session.get("log_path")
            if log_path:
                paths[str(name)] = str(log_path)
        return paths

    def snapshot_offsets(self) -> Dict[str, Tuple[str, int]]:
        snapshot: Dict[str, Tuple[str, int]] = {}
        for source, binding in list(self.sources.items()):
            snapshot[source] = (str(binding.log_path), binding.current_offset())
        return snapshot

    def stop_all(self) -> None:
        for binding in list(self.sources.values()):
            binding.stop()
        self.sources.clear()


class RouterSourceAdapter:
    def __init__(self, sources: SourceManager, source: str):
        self.sources = sources
        self.source = source
        self._base_offset = 0

    def read_since(self, offset: int) -> Tuple[bytes, int]:
        data, new_offset, _ = self.sources.read_router_since(self.source, offset)
        return data, new_offset

    def write(self, text: str, chunk_size: int = 0, chunk_delay_s: float = 0.0) -> None:
        self.write_bytes(text.encode("utf-8", errors="ignore"), chunk_size=chunk_size, chunk_delay_s=chunk_delay_s)

    def write_bytes(self, payload: bytes, chunk_size: int = 0, chunk_delay_s: float = 0.0) -> None:
        self.sources.write_router_source(
            self.source,
            payload,
            chunk_size=chunk_size,
            chunk_delay_s=chunk_delay_s,
        )

    def current_offset(self) -> int:
        sess = self.sources.router_session_for_source(self.source)
        if not sess:
            return 0
        log_path = Path(str(sess.get("log_path", "")))
        if not log_path.exists():
            return 0
        return log_path.stat().st_size


def validate_chain(chain: dict) -> None:
    if "entry" not in chain:
        raise ChainValidationError("chain entry missing")
    if "steps" not in chain:
        raise ChainValidationError("chain steps missing")
    steps = chain["steps"]
    if chain["entry"] not in steps:
        raise ChainValidationError("entry step not found in steps")
    split_groups = set()
    for name, step in steps.items():
        if "type" not in step:
            raise ChainValidationError(f"step {name} missing type")
        if "on_timeout" not in step and step["type"] not in ("pass", "fail"):
            raise ChainValidationError(f"step {name} missing on_timeout")
        if "outcomes" in step:
            for outcome in step["outcomes"]:
                if "next" not in outcome:
                    raise ChainValidationError(f"step {name} outcome missing next")
                if outcome["next"] not in steps:
                    raise ChainValidationError(f"step {name} outcome target missing: {outcome['next']}")
        if "on_timeout" in step and step["on_timeout"] not in steps:
            raise ChainValidationError(f"step {name} on_timeout target missing")
        if step.get("type") == "fork":
            raise ChainValidationError(
                f"step {name} uses deprecated type=fork; use task_spawn/task_join or split/join"
            )
        if step.get("type") in ("parallel_split", "parallel_join"):
            raise ChainValidationError(
                f"step {name} uses deprecated type={step.get('type')}; use split/join"
            )
        if step.get("type") == "call_chain":
            labels = {outcome.get("label") for outcome in step.get("outcomes", [])}
            if "pass" not in labels or "fail" not in labels:
                raise ChainValidationError(
                    f"step {name} call_chain requires outcomes for labels 'pass' and 'fail'"
                )
        if step.get("type") == "map_tcu_mux_source":
            if not step.get("source"):
                raise ChainValidationError(f"step {name} map_tcu_mux_source requires source")
            if not step.get("tty"):
                raise ChainValidationError(f"step {name} map_tcu_mux_source requires tty")
            if not step.get("tcu_muxer_path"):
                raise ChainValidationError(f"step {name} map_tcu_mux_source requires tcu_muxer_path")
        if step.get("type") == "wait_router_session":
            if not step.get("source"):
                raise ChainValidationError(f"step {name} wait_router_session requires source")
        if step.get("type") == "map_router_session_panes":
            if not step.get("window"):
                raise ChainValidationError(f"step {name} map_router_session_panes requires window")
        if step.get("type") == "task_spawn":
            task_name = str(step.get("task", "")).strip()
            chain_name = str(step.get("chain", "")).strip()
            if not task_name:
                raise ChainValidationError(f"step {name} task_spawn requires non-empty task")
            if not chain_name:
                raise ChainValidationError(f"step {name} task_spawn requires non-empty chain")
            if task_name == "prepare_next_run" or chain_name == "prepare_next_run_task":
                raise ChainValidationError(
                    f"step {name} reintroduces forbidden prepare lifecycle pattern; "
                    f"forbidden={FORBIDDEN_PREPARE_LIFECYCLE_PATTERNS}"
                )
        if step.get("type") == "task_join":
            tasks = step.get("tasks")
            if not isinstance(tasks, list) or not tasks:
                raise ChainValidationError(f"step {name} task_join requires non-empty tasks list")
            for task_name in tasks:
                if not isinstance(task_name, str) or not task_name.strip():
                    raise ChainValidationError(f"step {name} task_join has invalid task name: {task_name}")
                if task_name.strip() == "prepare_next_run":
                    raise ChainValidationError(
                        f"step {name} reintroduces forbidden prepare lifecycle pattern; "
                        f"forbidden={FORBIDDEN_PREPARE_LIFECYCLE_PATTERNS}"
                    )
            reduce_mode = str(step.get("reduce", "")).strip()
            if reduce_mode not in ("any_pass", "all_pass"):
                raise ChainValidationError(f"step {name} task_join requires reduce=any_pass|all_pass")
            labels = {outcome.get("label") for outcome in step.get("outcomes", [])}
            if "pass" not in labels or "fail" not in labels:
                raise ChainValidationError(
                    f"step {name} task_join requires outcomes for labels 'pass' and 'fail'"
                )
        if step.get("type") == "signal_set":
            signal_name = str(step.get("signal", "")).strip()
            if not signal_name:
                raise ChainValidationError(f"step {name} signal_set requires non-empty signal")
            if signal_name == "prepare_next_run_go":
                raise ChainValidationError(
                    f"step {name} reintroduces forbidden prepare lifecycle pattern; "
                    f"forbidden={FORBIDDEN_PREPARE_LIFECYCLE_PATTERNS}"
                )
        if step.get("type") == "signal_wait":
            signal_name = str(step.get("signal", "")).strip()
            if not signal_name:
                raise ChainValidationError(f"step {name} signal_wait requires non-empty signal")
        if step.get("type") == "case":
            source = str(step.get("source", "")).strip()
            if not source:
                raise ChainValidationError(f"step {name} case requires non-empty source")
            start_from = str(step.get("start_from", "head")).strip().lower()
            if start_from not in ("head", "tail"):
                raise ChainValidationError(f"step {name} case start_from must be head|tail")
            clauses = step.get("clauses")
            if not isinstance(clauses, list) or not clauses:
                raise ChainValidationError(f"step {name} case requires non-empty clauses list")
            for idx, clause in enumerate(clauses):
                if not isinstance(clause, dict):
                    raise ChainValidationError(f"step {name} case clause[{idx}] must be object")
                label = str(clause.get("label", "")).strip()
                pattern = str(clause.get("pattern", "")).strip()
                next_step = str(clause.get("next", "")).strip()
                if not label:
                    raise ChainValidationError(f"step {name} case clause[{idx}] requires non-empty label")
                if not pattern:
                    raise ChainValidationError(f"step {name} case clause[{idx}] requires non-empty pattern")
                if not next_step:
                    raise ChainValidationError(f"step {name} case clause[{idx}] requires non-empty next")
                if next_step != "self" and next_step not in steps:
                    raise ChainValidationError(
                        f"step {name} case clause[{idx}] target missing: {next_step}"
                    )
        if step.get("type") == "join":
            if "chain" in step:
                raise ChainValidationError(
                    f"step {name} join must not define 'chain'"
                )
            if "group" in step:
                raise ChainValidationError(
                    f"step {name} join must use 'join_groups', not legacy 'group'"
                )
            join_groups = step.get("join_groups")
            if not isinstance(join_groups, list) or not join_groups:
                raise ChainValidationError(
                    f"step {name} join requires non-empty join_groups list"
                )
            for group_name in join_groups:
                if not isinstance(group_name, str) or not group_name.strip():
                    raise ChainValidationError(
                        f"step {name} join has invalid join_groups entry: {group_name}"
                    )
            reduce_mode = str(step.get("reduce", "")).strip()
            if reduce_mode not in ("any_pass", "all_pass"):
                raise ChainValidationError(
                    f"step {name} join requires reduce=any_pass|all_pass"
                )
            labels = {outcome.get("label") for outcome in step.get("outcomes", [])}
            if "pass" not in labels or "fail" not in labels:
                raise ChainValidationError(
                    f"step {name} join requires outcomes for labels 'pass' and 'fail'"
                )
        if step.get("type") == "split":
            branches = step.get("branches")
            if not isinstance(branches, list) or not branches:
                raise ChainValidationError(f"step {name} split requires non-empty branches list")
            names = set()
            for branch in branches:
                if not isinstance(branch, dict):
                    raise ChainValidationError(f"step {name} has invalid branch entry")
                branch_name = str(branch.get("name", "")).strip()
                chain_name = str(branch.get("chain", "")).strip()
                if not branch_name or not chain_name:
                    raise ChainValidationError(
                        f"step {name} branch requires non-empty name and chain"
                    )
                if branch_name in names:
                    raise ChainValidationError(
                        f"step {name} duplicate branch name: {branch_name}"
                    )
                names.add(branch_name)
            group_name = str(step.get("group", "")).strip()
            if not group_name:
                raise ChainValidationError(f"step {name} split requires non-empty group")
            split_groups.add(group_name)
        if step.get("type") == "set_test_verdict":
            verdict = str(step.get("verdict", "")).strip()
            if verdict not in ("pass", "fail"):
                raise ChainValidationError(
                    f"step {name} set_test_verdict requires verdict=pass|fail"
                )
        if step.get("type") == "map_command_source":
            command = step.get("command")
            if not isinstance(command, list) or not command:
                raise ChainValidationError(f"step {name} map_command_source requires non-empty command list")
    for name, step in steps.items():
        if step.get("type") != "join":
            continue
        for group_name in step.get("join_groups", []):
            if str(group_name).strip() not in split_groups:
                raise ChainValidationError(
                    f"step {name} references unknown join group: {group_name}"
                )


class ChainRunner:
    def __init__(self, chain: dict, ctx: dict, recorder: ChainRecorder, finalize_on_exit: bool = True):
        self.chain = chain
        self.ctx = ctx
        self.recorder = recorder
        self.finalize_on_exit = finalize_on_exit
        self.event_queue: queue.Queue = ctx["event_queue"]
        self.cancel_flag = ctx["cancel_flag"]
        self.ctx.setdefault("chain_name", self.ctx.get("profile", "-"))
        stack = self.ctx.setdefault("chain_stack", [])
        if not stack:
            chain_name = str(self.ctx.get("chain_name", "")).strip()
            if chain_name:
                self.ctx["chain_stack"] = [chain_name]
        self.last_step_result: Optional[StepResult] = None

    def run(self) -> str:
        validate_chain(self.chain)
        self._validate_external_refs()
        current = self.chain["entry"]
        try:
            while True:
                self._check_cancel()
                step = self.chain["steps"][current]
                result, next_step = self._run_step(current, step)
                self.last_step_result = result
                self.recorder.record_step(result)
                if step["type"] in ("pass", "fail"):
                    if self.finalize_on_exit:
                        self.recorder.finalize(step["type"])
                    return step["type"]
                current = next_step
        except CancelRun:
            self._handle_cancel()
            if self.finalize_on_exit:
                self.recorder.finalize("failed", abort_reason="canceled")
            return "failed"
        except AbortRun:
            if self.finalize_on_exit:
                self.recorder.finalize("failed", abort_reason="user_abort")
            return "failed"

    def _run_step(self, name: str, step: dict) -> Tuple[StepResult, str]:
        started = time.time()
        source_offsets_before = self._snapshot_source_offsets()
        self._set_status(f"step={name}")
        try:
            next_step, outcome = self._dispatch_step(name, step)
            status = "ok"
            error_code = None
            error_message = None
            if outcome and outcome.label == "timeout":
                status = "timeout"
                error_code = "timeout"
        except (CancelRun, AbortRun):
            raise
        except Exception as exc:
            next_step = step.get("on_error", step.get("on_timeout", "fail"))
            outcome = OutcomeMatch(
                label="error",
                next_step=next_step,
                pattern=None,
                source=None,
                log_path=None,
                log_offset=None,
            )
            status = "error"
            error_code = "exception"
            error_message = str(exc)
        finished = time.time()
        source_offsets_after = self._snapshot_source_offsets()
        result = StepResult(
            step=name,
            status=status,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
            started_at=started,
            finished_at=finished,
            chain_name=self.ctx.get("chain_name", "-"),
            chain_stack=list(self.ctx.get("chain_stack", [])),
            source_ranges=self._build_source_ranges(source_offsets_before, source_offsets_after),
        )
        return result, next_step

    def _snapshot_source_offsets(self) -> Dict[str, Tuple[str, int]]:
        sources = self.ctx.get("sources")
        if not sources or not hasattr(sources, "snapshot_offsets"):
            return {}
        try:
            return sources.snapshot_offsets()
        except Exception:
            return {}

    def _build_source_ranges(
        self,
        before: Dict[str, Tuple[str, int]],
        after: Dict[str, Tuple[str, int]],
    ) -> Dict[str, dict]:
        ranges: Dict[str, dict] = {}
        for source in sorted(set(before.keys()) | set(after.keys())):
            before_item = before.get(source)
            after_item = after.get(source)

            if before_item and after_item and before_item[0] == after_item[0]:
                log_path = after_item[0]
                start_offset = before_item[1]
                end_offset = after_item[1]
            elif after_item:
                # Source appears/remaps during this step; range is relative to new log.
                log_path = after_item[0]
                start_offset = 0
                end_offset = after_item[1]
            elif before_item:
                # Source disappeared during this step; preserve stable zero-length range.
                log_path = before_item[0]
                start_offset = before_item[1]
                end_offset = before_item[1]
            else:
                continue

            start_offset = int(max(0, start_offset))
            end_offset = int(max(start_offset, end_offset))
            ranges[source] = {
                "log_path": log_path,
                "start_offset": start_offset,
                "end_offset": end_offset,
                "bytes": end_offset - start_offset,
            }
        return ranges

    def _dispatch_step(self, name: str, step: dict) -> Tuple[str, OutcomeMatch]:
        self._check_cancel()
        prev_step = self.ctx.get("_current_step_name")
        self.ctx["_current_step_name"] = name
        try:
            step_type = step["type"]
            if step_type == "pass":
                return "pass", OutcomeMatch("pass", "pass", None, None, None, None)
            if step_type == "fail":
                return "fail", OutcomeMatch("fail", "fail", None, None, None, None)
            if step_type == "relay":
                self.ctx["board"].boot(False)
                return self._simple_outcome(step)
            if step_type == "map_source":
                return self._step_map_source(step)
            if step_type == "map_tcu_mux_source":
                return self._step_map_tcu_mux_source(step)
            if step_type == "wait_router_session":
                return self._step_wait_router_session(step)
            if step_type == "map_router_session_panes":
                return self._step_map_router_session_panes(step)
            if step_type == "map_command_source":
                return self._step_map_command_source(step)
            if step_type == "purge_sources":
                return self._step_purge_sources(step)
            if step_type == "map_window":
                return self._step_map_window(step)
            if step_type == "send_cmd":
                return self._step_send_cmd(step)
            if step_type == "boot_menu":
                return self._step_boot_menu(step)
            if step_type == "boot_efi":
                return self._step_boot_efi(step)
            if step_type == "uefi_shell_run":
                return self._step_uefi_shell_run(step)
            if step_type == "wait_pattern":
                return self._step_wait_pattern(step)
            if step_type == "case":
                return self._step_case(step)
            if step_type == "upload_kernel":
                return self._step_upload(step, kind="kernel")
            if step_type == "upload_efi":
                return self._step_upload(step, kind="efi")
            if step_type == "upload_file":
                return self._step_upload_file(step)
            if step_type == "reboot":
                return self._step_reboot(step)
            if step_type == "ssh_cmd":
                return self._step_ssh_cmd(step)
            if step_type == "ssh_wait_ready":
                return self._step_ssh_wait_ready(step)
            if step_type == "call_chain":
                return self._step_call_chain(step)
            if step_type == "task_spawn":
                return self._step_task_spawn(step)
            if step_type == "task_join":
                return self._step_task_join(step)
            if step_type == "signal_set":
                return self._step_signal_set(step)
            if step_type == "signal_wait":
                return self._step_signal_wait(step)
            if step_type == "split":
                return self._step_split(step)
            if step_type == "join":
                return self._step_join(step)
            if step_type == "analyze_logs":
                return self._step_analyze_logs(step)
            if step_type == "interactive_console":
                return self._step_interactive_console(step)
            if step_type == "set_overrides":
                return self._step_set_overrides(step)
            if step_type == "set_test_verdict":
                return self._step_set_test_verdict(step)
            if step_type == "setup_demo":
                return self._step_setup_demo(step)
            if step_type == "check_verdict":
                return self._step_check_verdict(step)
            raise ChainValidationError(f"unknown step type: {step_type}")
        finally:
            if prev_step is None:
                self.ctx.pop("_current_step_name", None)
            else:
                self.ctx["_current_step_name"] = prev_step

    def _simple_outcome(self, step: dict) -> Tuple[str, OutcomeMatch]:
        outcomes = step.get("outcomes", [])
        if outcomes:
            label = outcomes[0].get("label", "ok")
            next_step = outcomes[0].get("next", step.get("on_timeout", "fail"))
        else:
            label = "ok"
            next_step = step.get("on_timeout", "fail")
        return next_step, OutcomeMatch(label, next_step, None, None, None, None)

    def _step_map_source(self, step: dict) -> Tuple[str, OutcomeMatch]:
        source = step["source"]
        tty = step.get("tty")
        defaults = self.ctx.get("default_ttys", {})
        if not tty:
            tty = defaults.get(source)
        if isinstance(tty, str) and tty.startswith("env:"):
            env_key = tty.split("env:", 1)[1]
            tty = (os.environ.get(env_key, "") or "").strip()
            if not tty:
                tty = defaults.get(source)
        if isinstance(tty, str):
            tty = tty.strip()
        if not tty:
            raise ValueError("map_source requires tty")
        log_rel = step.get("log", f"console/{source}.jsonl")
        baud = int(step.get("baud", 115200))
        self.ctx["sources"].map_source(source, tty, log_rel, baud=baud)
        ui = self.ctx.get("ui")
        if ui and hasattr(ui, "state"):
            ui.state.map_source(source, tty, str(self.ctx["result_dir"] / log_rel))
        return self._simple_outcome(step)

    def _step_map_command_source(self, step: dict) -> Tuple[str, OutcomeMatch]:
        source = step["source"]
        command = step.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError("map_command_source requires non-empty command list")
        resolved_command = [str(self._resolve_value(item)) for item in command]
        log_rel = step.get("log", f"console/{source}.raw")
        cwd_value = step.get("cwd")
        cwd = Path(self._resolve_value(cwd_value)) if cwd_value else None
        env_updates = step.get("env", {}) or {}
        if not isinstance(env_updates, dict):
            raise ValueError("map_command_source env must be a dictionary")
        env = os.environ.copy()
        for key, value in env_updates.items():
            env[str(key)] = str(self._resolve_value(value))
        self.ctx["sources"].map_command_source(source, resolved_command, log_rel, cwd=cwd, env=env)
        ui = self.ctx.get("ui")
        if ui and hasattr(ui, "state"):
            ui.state.map_source(source, shlex.join(resolved_command), str(self.ctx["result_dir"] / log_rel))
        return self._simple_outcome(step)

    def _step_map_tcu_mux_source(self, step: dict) -> Tuple[str, OutcomeMatch]:
        source = step["source"]
        tty = self._resolve_value(step.get("tty"))
        if isinstance(tty, str) and tty.startswith("env:"):
            env_key = tty.split("env:", 1)[1]
            tty = (os.environ.get(env_key, "") or "").strip()
        if not tty:
            raise ValueError("map_tcu_mux_source requires tty")
        tcu_muxer_path = str(self._resolve_value(step.get("tcu_muxer_path")))
        outer_mode = str(self._resolve_value(step.get("outer_mode", "raw")))
        outer_tag = str(self._resolve_value(step.get("outer_tag", "CCPLEX")))
        log_rel = step.get("log", f"console/{source}.raw")
        replace_sources = step.get("replace_sources", []) or []
        if not isinstance(replace_sources, list):
            raise ValueError("map_tcu_mux_source replace_sources must be a list")
        self.ctx["sources"].map_tcu_mux_source(
            source,
            str(tty),
            log_rel,
            tcu_muxer_path=tcu_muxer_path,
            outer_mode=outer_mode,
            outer_tag=outer_tag,
            replace_sources=[str(item) for item in replace_sources],
        )
        ui = self.ctx.get("ui")
        if ui and hasattr(ui, "state"):
            ui.state.map_source(source, f"{tcu_muxer_path} -d {tty}", str(self.ctx["result_dir"] / log_rel))
        return self._simple_outcome(step)

    def _step_wait_router_session(self, step: dict) -> Tuple[str, OutcomeMatch]:
        source = str(step.get("source") or "").strip()
        if not source:
            raise ValueError("wait_router_session requires source")
        timeout_s = float(step.get("timeout_s", 5.0))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self._poll_runtime_events()
            session = self.ctx["sources"].router_session_for_source(source)
            if session and (session.get("pty_path") or session.get("log_path")):
                return self._simple_outcome(step)
            time.sleep(0.05)
        raise RuntimeError(f"router session not available: {source}")

    def _step_map_router_session_panes(self, step: dict) -> Tuple[str, OutcomeMatch]:
        window = int(step["window"])
        title = str(step.get("title") or "Console Sessions")
        include_patterns = [str(item) for item in step.get("include_patterns", []) or []]
        exclude_patterns = [str(item) for item in step.get("exclude_patterns", []) or []]
        only_interactive = bool(step.get("only_interactive", False))
        max_panes = int(step.get("max_panes", 8) or 8)
        include_status_pane = bool(step.get("include_status_pane", False))
        status_title = str(step.get("status_title") or "Autopilot")
        layout = str(step.get("layout") or "tiled")
        status_rows = int(step.get("status_rows", 0) or 0)
        sessions = self.ctx["sources"].router_sessions()

        def matched(name: str, patterns: List[str]) -> bool:
            if not patterns:
                return False
            for pattern in patterns:
                if fnmatch.fnmatch(name, pattern):
                    return True
                try:
                    if re.search(pattern, name):
                        return True
                except re.error:
                    continue
            return False

        panes = []
        for name, session in sorted(sessions.items()):
            if only_interactive and not session.get("interactive"):
                continue
            if include_patterns and not matched(name, include_patterns):
                continue
            if exclude_patterns and matched(name, exclude_patterns):
                continue
            log_path = str(session.get("log_path") or "")
            if not log_path:
                continue
            panes.append({
                "source": name,
                "title": str(session.get("title") or name),
                "log_path": log_path,
                "read_only": bool(session.get("read_only", False)),
            })
            if max_panes > 0 and len(panes) >= max_panes:
                break

        ui = self.ctx.get("ui")
        if panes and ui and hasattr(ui, "bind_source_panes"):
            ui.bind_source_panes(
                window,
                title,
                panes,
                include_status_pane=include_status_pane,
                status_title=status_title,
                layout=layout,
                status_rows=status_rows,
            )
        return self._simple_outcome(step)

    def _step_map_window(self, step: dict) -> Tuple[str, OutcomeMatch]:
        window = int(step["window"])
        source = step["source"]
        title = step.get("title")
        ui = self.ctx.get("ui")
        if ui and hasattr(ui, "bind_window"):
            ui.bind_window(window, source, title=title)
        return self._simple_outcome(step)

    def _load_demo_layout(self, layout_name: str) -> dict:
        import yaml
        code_root = Path(__file__).resolve().parent
        layout_path = code_root / "demos" / f"{layout_name}.yaml"
        with open(layout_path) as f:
            return yaml.safe_load(f)

    def _step_setup_demo(self, step: dict) -> Tuple[str, OutcomeMatch]:
        layout_name = step["layout"]
        layout = self._load_demo_layout(layout_name)
        ui = self.ctx.get("ui")
        pane_windows: dict = {}
        for i, pane in enumerate(layout.get("layout", {}).get("panes", [])):
            window = i + 1
            pane_id = pane.get("id", str(i))
            source_spec = pane.get("source", "")
            title = pane.get("title", pane_id)
            pane_windows[pane_id] = window
            if source_spec.startswith("uart:"):
                source = source_spec[len("uart:"):]
                if ui and hasattr(ui, "bind_window"):
                    ui.bind_window(window, source, title=title)
            # mux: and container: sources are not yet wired; pane slot is reserved.
        self.ctx["_demo_pane_windows"] = pane_windows
        self.ctx["_demo_layout_name"] = layout_name
        return self._simple_outcome(step)

    def _step_check_verdict(self, step: dict) -> Tuple[str, OutcomeMatch]:
        layout_name = step.get("layout") or self.ctx.get("_demo_layout_name")
        layout = self._load_demo_layout(layout_name)
        verdict_spec = layout.get("verdict", {})
        authority = verdict_spec.get("authority", "exit_code")
        pattern = verdict_spec.get("pattern", "PASS")

        if authority == "artifact_grep":
            file_path = verdict_spec["file"]
            target_user = verdict_spec.get("target_user", "root")
            target_ip = self._resolve_value(verdict_spec.get("target_ip", "{target_ip}"))
            timeout_s = int(verdict_spec.get("timeout_s", 30))
            ssh_base = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "BatchMode=yes",
                "-o", "GSSAPIAuthentication=no",
                f"{target_user}@{target_ip}",
            ]
            verdict = "fail"
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                self._check_cancel()
                result = subprocess.run(
                    ssh_base + [f"grep -q {shlex.quote(pattern)} {shlex.quote(file_path)}"],
                    check=False,
                )
                if result.returncode == 0:
                    verdict = "pass"
                    break
                time.sleep(2)

        elif authority == "tmux_capture":
            pane_id = verdict_spec["pane"]
            pane_windows = self.ctx.get("_demo_pane_windows", {})
            window = pane_windows.get(pane_id)
            timeout_s = int(verdict_spec.get("timeout_s", 30))
            ui = self.ctx.get("ui")
            session = None
            if ui and hasattr(ui, "windows") and ui.windows:
                session = getattr(ui.windows, "session", None)
            verdict = "fail"
            if session and window:
                deadline = time.time() + timeout_s
                while time.time() < deadline:
                    self._check_cancel()
                    result = subprocess.run(
                        ["tmux", "capture-pane", "-t", f"{session}:{window}", "-p"],
                        capture_output=True, text=True, check=False,
                    )
                    if re.search(pattern, result.stdout):
                        verdict = "pass"
                        break
                    time.sleep(1)

        else:
            verdict = "fail"

        outcomes = step.get("outcomes", [])
        for outcome in outcomes:
            if outcome.get("label") == verdict:
                next_step = outcome["next"]
                return next_step, OutcomeMatch(verdict, next_step, None, None, None, None)
        fallback = step.get("on_timeout", "fail")
        return fallback, OutcomeMatch(verdict, fallback, None, None, None, None)

    def _step_purge_sources(self, step: dict) -> Tuple[str, OutcomeMatch]:
        names = step.get("sources")
        if names is None:
            names = list(self.ctx["sources"].sources.keys())
        for name in names:
            binding = self.ctx["sources"].get(name)
            if binding:
                binding.purge()
        return self._simple_outcome(step)

    def _step_send_cmd(self, step: dict) -> Tuple[str, OutcomeMatch]:
        source = step["source"]
        write_source = step.get("write_source", source)
        cmd = step["cmd"]
        binding = self.ctx["sources"].get(write_source)
        suffix = step.get("suffix", "\n")
        payload = cmd + suffix
        chunk_size = int(step.get("write_chunk_size", 0) or 0)
        chunk_delay_s = float(step.get("write_chunk_delay_s", 0.0) or 0.0)
        if binding:
            binding.write(payload, chunk_size=chunk_size, chunk_delay_s=chunk_delay_s)
            return self._simple_outcome(step)
        self.ctx["sources"].write_router_source(
            write_source,
            payload.encode("utf-8", errors="ignore"),
            chunk_size=chunk_size,
            chunk_delay_s=chunk_delay_s,
        )
        return self._simple_outcome(step)

    def _step_boot_menu(self, step: dict) -> Tuple[str, OutcomeMatch]:
        # Wait for a menu prompt (outcomes) then send boot option.
        next_step, outcome = self._step_wait_pattern(step)
        source = step.get("source") or outcome.source
        if source:
            binding = self.ctx["sources"].get(source)
            if binding:
                binding.write(str(step.get("boot_option", "")))
        return next_step, outcome

    def _poll_runtime_events(self) -> None:
        self._check_cancel()
        event = self._poll_event()
        if event:
            if event.kind == "abort":
                self._handle_abort()
            if event.kind == "exit":
                self.ctx["exit_flag"].set()
                raise AbortRun()

    def _initial_cursor(self, binding: SourceBinding, start_from: str) -> int:
        if start_from == "tail":
            _, cursor = binding.read_since(1 << 60)
            return cursor
        return 0

    def _append_capped_buffer(
        self,
        existing: bytes,
        existing_start: int,
        data: bytes,
        chunk_start: int,
        max_buffer: int,
    ) -> Tuple[bytes, int]:
        if existing:
            combined_start = existing_start
            combined = existing + data
        else:
            combined_start = chunk_start
            combined = data
        if len(combined) > max_buffer:
            trim = len(combined) - max_buffer
            combined = combined[trim:]
            combined_start += trim
        return combined, combined_start

    def _append_capped_text(
        self,
        existing: str,
        data: str,
        max_buffer: int,
    ) -> str:
        combined = existing + data
        if len(combined) > max_buffer:
            combined = combined[-max_buffer:]
        return combined

    def _wait_any_pattern_on_binding(
        self,
        binding: SourceBinding,
        cursor: int,
        patterns: List[str],
        timeout_s: int,
        max_buffer: int = 65536,
    ) -> Tuple[int, int]:
        compiled = [re.compile(pattern, re.MULTILINE) for pattern in patterns]
        buffer = ""
        start = time.time()
        while time.time() - start < timeout_s:
            self._check_cancel()
            event = self._poll_event()
            if event:
                if event.kind == "abort":
                    self._handle_abort()
                if event.kind == "exit":
                    self.ctx["exit_flag"].set()
                    raise AbortRun()
            data, new_cursor = binding.read_since(cursor)
            cursor = new_cursor
            if not data:
                time.sleep(0.1)
                continue
            text = normalize_tty_text(data.decode("utf-8", errors="ignore"))
            if text:
                buffer = self._append_capped_text(buffer, text, max_buffer)
                for idx, regex in enumerate(compiled):
                    if regex.search(buffer):
                        return idx, cursor
        return -1, cursor

    def _step_wait_pattern(self, step: dict) -> Tuple[str, OutcomeMatch]:
        timeout_s = int(step.get("timeout_s", 30))
        idle_timeout_s = step.get("idle_timeout_s")  # None = disabled
        outcomes = step.get("outcomes", [])
        start_from = str(step.get("start_from", "head")).strip().lower()
        if start_from not in ("head", "tail"):
            raise ValueError("wait_pattern start_from must be head|tail")
        start = time.time()
        last_data_time = start
        cursors: Dict[str, int] = {}
        buffers: Dict[str, bytes] = {}
        buffer_starts: Dict[str, int] = {}
        max_buffer = 65536
        while time.time() - start < timeout_s:
            self._poll_runtime_events()
            outcomes_by_source: Dict[str, List[dict]] = {}
            for outcome in outcomes:
                pattern = outcome.get("pattern")
                source = outcome.get("source")
                if not pattern or not source:
                    continue
                outcomes_by_source.setdefault(str(source), []).append(outcome)

            for source, source_outcomes in outcomes_by_source.items():
                binding = self.ctx["sources"].get(source)
                if binding:
                    if source not in cursors:
                        cursors[source] = self._initial_cursor(binding, start_from)
                    cursor = cursors.get(source, 0)
                    data, new_cursor = binding.read_since(cursor)
                    chunk_start = max(cursor, binding._base_offset)
                    cursors[source] = new_cursor
                    log_path = str(binding.log_path)
                else:
                    router_session = self.ctx["sources"].router_session_for_source(source)
                    if not router_session:
                        continue
                    if source not in cursors:
                        log_path_obj = Path(str(router_session.get("log_path", "")))
                        if start_from == "tail" and log_path_obj.exists():
                            cursors[source] = log_path_obj.stat().st_size
                        else:
                            cursors[source] = 0
                    cursor = cursors.get(source, 0)
                    data, new_cursor, log_path = self.ctx["sources"].read_router_since(source, cursor)
                    chunk_start = cursor
                    cursors[source] = new_cursor

                if data:
                    last_data_time = time.time()
                    existing = buffers.get(source, b"")
                    existing_start = buffer_starts.get(source, chunk_start)
                    combined, combined_start = self._append_capped_buffer(
                        existing=existing,
                        existing_start=existing_start,
                        data=data,
                        chunk_start=chunk_start,
                        max_buffer=max_buffer,
                    )
                    buffers[source] = combined
                    buffer_starts[source] = combined_start

                combined = buffers.get(source, b"")
                if not combined:
                    continue
                combined_start = buffer_starts.get(source, chunk_start)
                normalized, norm_map = normalize_tty_bytes(combined)
                if not normalized:
                    continue

                for outcome in source_outcomes:
                    pattern = outcome.get("pattern")
                    if not pattern:
                        continue
                    pattern_bytes = pattern.encode("utf-8")
                    match = re.search(pattern_bytes, normalized, re.MULTILINE)
                    if match:
                        offset = to_raw_offset(match.start(), norm_map, combined_start)
                        next_step = outcome.get("next", step.get("on_timeout", "fail"))
                        return next_step, OutcomeMatch(
                            label=outcome.get("label", "match"),
                            next_step=next_step,
                            pattern=pattern,
                            source=source,
                            log_path=log_path,
                            log_offset=offset,
                        )
            if idle_timeout_s is not None and time.time() - last_data_time > idle_timeout_s:
                break
            time.sleep(0.1)
        next_step = step.get("on_timeout", "fail")
        return next_step, OutcomeMatch(
            label="timeout",
            next_step=next_step,
            pattern=None,
            source=None,
            log_path=None,
            log_offset=None,
        )

    def _step_case(self, step: dict) -> Tuple[str, OutcomeMatch]:
        timeout_s = int(step.get("timeout_s", 30))
        idle_timeout_s = step.get("idle_timeout_s")  # None = disabled
        scan = bool(step.get("scan", False))
        source = str(step.get("source", "")).strip()
        if not source:
            raise ValueError("case requires source")
        clauses = step.get("clauses")
        if not isinstance(clauses, list) or not clauses:
            raise ValueError("case requires non-empty clauses list")
        start_from = str(step.get("start_from", "head")).strip().lower()
        if start_from not in ("head", "tail"):
            raise ValueError("case start_from must be head|tail")
        binding = self.ctx["sources"].get(source)
        if not binding and self.ctx["sources"].router_session_for_source(source):
            binding = RouterSourceAdapter(self.ctx["sources"], source)
        if not binding:
            raise ValueError(f"unknown source {source}")

        compiled = []
        for idx, clause in enumerate(clauses):
            if not isinstance(clause, dict):
                raise ValueError(f"case clause[{idx}] must be object")
            label = str(clause.get("label", "")).strip()
            pattern = str(clause.get("pattern", "")).strip()
            next_step = str(clause.get("next", "")).strip()
            if not label:
                raise ValueError(f"case clause[{idx}] requires non-empty label")
            if not pattern:
                raise ValueError(f"case clause[{idx}] requires non-empty pattern")
            if not next_step:
                raise ValueError(f"case clause[{idx}] requires non-empty next")
            compiled.append((label, pattern, next_step, re.compile(pattern.encode("utf-8"), re.MULTILINE)))

        cursor = self._initial_cursor(binding, start_from)

        max_buffer = 65536
        buffer = b""
        buffer_start = cursor
        start = time.time()
        last_data_time = start
        while time.time() - start < timeout_s:
            self._poll_runtime_events()
            data, new_cursor = binding.read_since(cursor)
            chunk_start = max(cursor, binding._base_offset)
            cursor = new_cursor
            if data:
                last_data_time = time.time()
                buffer, buffer_start = self._append_capped_buffer(
                    existing=buffer,
                    existing_start=buffer_start,
                    data=data,
                    chunk_start=chunk_start,
                    max_buffer=max_buffer,
                )
            normalized, norm_map = normalize_tty_bytes(buffer)
            if not normalized:
                if idle_timeout_s is not None and time.time() - last_data_time > idle_timeout_s:
                    break
                time.sleep(0.1)
                continue

            if scan:
                for label, pattern, next_step, compiled_pattern in compiled:
                    match = compiled_pattern.search(normalized)
                    if not match:
                        continue
                    offset = to_raw_offset(match.start(), norm_map, buffer_start)
                    return next_step, OutcomeMatch(
                        label=label,
                        next_step=next_step,
                        pattern=pattern,
                        source=source,
                        log_path=str(binding.log_path),
                        log_offset=offset,
                    )
            else:
                for label, pattern, next_step, compiled_pattern in compiled:
                    match = compiled_pattern.match(normalized)
                    if not match:
                        continue
                    if next_step == "self":
                        if match.end() <= match.start():
                            raise ValueError(
                                f"case clause '{label}' matched empty span with next=self; "
                                f"pattern={pattern}"
                            )
                        consumed_norm = match.end()
                        consumed_raw = norm_map[consumed_norm - 1] + 1
                        buffer = buffer[consumed_raw:]
                        buffer_start += consumed_raw
                        break

                    offset = to_raw_offset(match.start(), norm_map, buffer_start)
                    return next_step, OutcomeMatch(
                        label=label,
                        next_step=next_step,
                        pattern=pattern,
                        source=source,
                        log_path=str(binding.log_path),
                        log_offset=offset,
                    )
                else:
                    if idle_timeout_s is not None and time.time() - last_data_time > idle_timeout_s:
                        break
                    time.sleep(0.1)
                    continue
            if idle_timeout_s is not None and time.time() - last_data_time > idle_timeout_s:
                break
            time.sleep(0.1)

        next_step = step.get("on_timeout", "fail")
        return next_step, OutcomeMatch(
            label="timeout",
            next_step=next_step,
            pattern=None,
            source=None,
            log_path=None,
            log_offset=None,
        )

    def _wait_for_any_pattern(self, source: str, patterns: List[str], timeout_s: int) -> int:
        binding = self.ctx["sources"].get(source)
        if not binding and self.ctx["sources"].router_session_for_source(source):
            binding = RouterSourceAdapter(self.ctx["sources"], source)
        if not binding:
            raise ValueError(f"unknown source {source}")
        idx, _ = self._wait_any_pattern_on_binding(
            binding=binding,
            cursor=0,
            patterns=patterns,
            timeout_s=timeout_s,
        )
        if idx != -1:
            return idx
        return -1

    def _wait_for_pattern(self, source: str, pattern: str, timeout_s: int) -> bool:
        return self._wait_for_any_pattern(source, [pattern], timeout_s) == 0

    def _is_orin_agx_uefi_netboot(self) -> bool:
        return (os.environ.get("AUTOPILOT_PLATFORM", "") or "").strip() == "orin-agx-uefi-netboot"

    def _assert_reset_line(self) -> None:
        board = self.ctx.get("board")
        if board is None:
            raise RuntimeError("reset line control unavailable: board not configured")
        if hasattr(board, "assert_reset_line"):
            board.assert_reset_line()
            return
        if hasattr(board, "set_reset"):
            board.set_reset(True)
            return
        raise RuntimeError("reset line control unavailable: assert operation is not supported")

    def _deassert_reset_line(self) -> None:
        board = self.ctx.get("board")
        if board is None:
            raise RuntimeError("reset line control unavailable: board not configured")
        if hasattr(board, "deassert_reset_line"):
            board.deassert_reset_line()
            return
        if hasattr(board, "set_reset"):
            board.set_reset(False)
            return
        raise RuntimeError("reset line control unavailable: deassert operation is not supported")

    def _wait_for_uart_quiescence(self, quiet_s: float, timeout_s: float) -> None:
        if quiet_s <= 0:
            return
        sources = self.ctx.get("sources")
        if not sources:
            raise RuntimeError("uart quiescence check requires mapped sources")

        now = time.time()
        deadline = now + timeout_s
        offsets: Dict[str, int] = {}
        for source_name, binding in list(sources.sources.items()):
            offsets[source_name] = binding.current_offset()
        last_activity = now

        while time.time() < deadline:
            self._poll_runtime_events()
            now = time.time()
            changed = False
            for source_name, binding in list(sources.sources.items()):
                offset = binding.current_offset()
                prev = offsets.get(source_name)
                if prev is None or offset != prev:
                    offsets[source_name] = offset
                    changed = True
            if changed:
                last_activity = now
            elif (now - last_activity) >= quiet_s:
                return
            time.sleep(0.05)

        raise RuntimeError(
            f"uart traffic did not quiesce for {quiet_s:.3f}s within {timeout_s:.3f}s"
        )

    def _flush_tty_tmux_views(self) -> None:
        sources = self.ctx.get("sources")
        if not sources or not hasattr(sources, "sources"):
            return
        tty_sources = sorted(
            source_name
            for source_name in sources.sources.keys()
            if str(source_name).startswith("tty")
        )
        if not tty_sources:
            return

        ui = self.ctx.get("ui")
        if ui and hasattr(ui, "state"):
            for source_name in tty_sources:
                try:
                    live_path = ui.state.live_path_for_source(source_name)
                    live_path.parent.mkdir(parents=True, exist_ok=True)
                    live_path.write_bytes(b"")
                except Exception:
                    pass
        if ui and hasattr(ui, "clear_source_windows"):
            try:
                ui.clear_source_windows(tty_sources)
            except Exception:
                pass

    def _step_boot_efi(self, step: dict) -> Tuple[str, OutcomeMatch]:
        source = step.get("source")
        if not source:
            raise ValueError("boot_efi requires source")
        mode = str(self._resolve_value(step.get("mode")) or "").strip()
        if mode not in ("extlinux", "test_efi"):
            raise ValueError("boot_efi requires mode=extlinux|test_efi")

        prompt_timeout_s = int(step.get("prompt_timeout_s", 90))
        post_send_delay_s = float(step.get("post_send_delay_s", 0))
        success_timeout_s = int(step.get("success_timeout_s", 15))
        prompt_poke = str(step.get("prompt_poke", "\r"))
        prompt_poke_interval_s = float(step.get("prompt_poke_interval_s", 8.0))
        prompt_poke_max = int(step.get("prompt_poke_max", 1))
        prompt_patterns = step.get("prompt_patterns") or [
            r"Shell>",
            r"FS[0-9]+:\\>",
        ]
        if not isinstance(prompt_patterns, list) or not prompt_patterns:
            raise ValueError("boot_efi prompt_patterns must be a non-empty list")

        if mode == "extlinux":
            command = "fs3:\\EFI\\BOOT\\BOOTAA64.EFI"
            success_patterns = step.get("success_patterns") or [
                r"L4TLauncher: Attempting Direct Boot",
            ]
        else:
            target_binary_name = self._resolve_value(step.get("target_binary_name"))
            if not target_binary_name:
                target_binary_name = (self.ctx.get("request") or {}).get("target_binary_name")
            if not target_binary_name:
                raise ValueError("boot_efi mode=test_efi requires target_binary_name")
            command = f"fs2:\\efiboot\\{target_binary_name}"
            success_patterns = step.get("success_patterns")

        binding = self.ctx["sources"].get(source)
        if not binding:
            raise ValueError(f"unknown source {source}")

        shell_ready = False
        if self._is_orin_agx_uefi_netboot():
            shell_timeout_s = int(step.get("shell_timeout_s", 60))
            uart_quiet_s = float(step.get("uart_quiet_s", 1.0))
            uart_quiet_timeout_s = float(step.get("uart_quiet_timeout_s", 30.0))
            post_quiet_delay_s = float(step.get("post_quiet_delay_s", 0.5))
            startup_patterns = step.get("startup_patterns") or [
                r"startup\.nsh",
                r"Enter to continue boot\.",
            ]
            shell_patterns = step.get("shell_patterns") or [
                r"Shell>",
            ]
            if not isinstance(startup_patterns, list) or not startup_patterns:
                raise ValueError("boot_efi startup_patterns must be a non-empty list")
            if not isinstance(shell_patterns, list) or not shell_patterns:
                raise ValueError("boot_efi shell_patterns must be a non-empty list")
            startup_compiled = [re.compile(pattern, re.MULTILINE) for pattern in startup_patterns]
            shell_compiled = [re.compile(pattern, re.MULTILINE) for pattern in shell_patterns]

            _, cursor = binding.read_since(1 << 60)
            deasserted = False
            saw_startup = False
            shell_buffer = ""
            start = time.time()
            try:
                self._flush_tty_tmux_views()
                self._assert_reset_line()
                self._wait_for_uart_quiescence(
                    quiet_s=uart_quiet_s,
                    timeout_s=uart_quiet_timeout_s,
                )
                if post_quiet_delay_s > 0:
                    time.sleep(post_quiet_delay_s)
                self._deassert_reset_line()
                deasserted = True

                while time.time() - start < shell_timeout_s:
                    self._poll_runtime_events()
                    data, new_cursor = binding.read_since(cursor)
                    cursor = new_cursor
                    if not data:
                        time.sleep(0.1)
                        continue
                    text = normalize_tty_text(data.decode("utf-8", errors="ignore"))
                    if text:
                        shell_buffer = self._append_capped_text(shell_buffer, text, 65536)
                    if (not saw_startup) and any(regex.search(shell_buffer) for regex in startup_compiled):
                        binding.write("\r")
                        saw_startup = True
                    if any(regex.search(shell_buffer) for regex in shell_compiled):
                        shell_ready = True
                        break
                else:
                    raise RuntimeError(
                        f"boot_efi: failed to reach UEFI Shell prompt within {shell_timeout_s}s"
                    )
            finally:
                if not deasserted:
                    try:
                        self._deassert_reset_line()
                    except Exception:
                        pass

        # Start from fresh output to avoid stale prompt matches from previous boot phases.
        _, cursor = binding.read_since(1 << 60)
        if shell_ready:
            binding.write(f"{command}\r")
            if post_send_delay_s > 0:
                time.sleep(post_send_delay_s)
            if success_patterns:
                if not isinstance(success_patterns, list) or not success_patterns:
                    raise ValueError("boot_efi success_patterns must be a non-empty list when set")
                idx, cursor = self._wait_any_pattern_on_binding(
                    binding=binding,
                    cursor=cursor,
                    patterns=success_patterns,
                    timeout_s=success_timeout_s,
                )
                if idx != -1:
                    return self._simple_outcome(step)
                raise RuntimeError("boot_efi: command dispatched but success criterion not observed")
            return self._simple_outcome(step)

        start = time.time()
        last_poke_at = start
        poke_count = 0
        prompt_compiled = [re.compile(pattern, re.MULTILINE) for pattern in prompt_patterns]
        prompt_buffer = ""
        while time.time() - start < prompt_timeout_s:
            self._check_cancel()
            event = self._poll_event()
            if event:
                if event.kind == "abort":
                    self._handle_abort()
                if event.kind == "exit":
                    self.ctx["exit_flag"].set()
                    raise AbortRun()
            data, new_cursor = binding.read_since(cursor)
            cursor = new_cursor
            if not data:
                now = time.time()
                if (
                    prompt_poke
                    and prompt_poke_max > 0
                    and poke_count < prompt_poke_max
                    and (now - last_poke_at) >= prompt_poke_interval_s
                ):
                    binding.write(prompt_poke)
                    poke_count += 1
                    last_poke_at = now
                time.sleep(0.1)
                continue
            text = normalize_tty_text(data.decode("utf-8", errors="ignore"))
            if text:
                prompt_buffer = self._append_capped_text(prompt_buffer, text, 65536)
            if any(regex.search(prompt_buffer) for regex in prompt_compiled):
                binding.write(f"{command}\r")
                if post_send_delay_s > 0:
                    time.sleep(post_send_delay_s)
                if success_patterns:
                    if not isinstance(success_patterns, list) or not success_patterns:
                        raise ValueError("boot_efi success_patterns must be a non-empty list when set")
                    idx, cursor = self._wait_any_pattern_on_binding(
                        binding=binding,
                        cursor=cursor,
                        patterns=success_patterns,
                        timeout_s=success_timeout_s,
                    )
                    if idx != -1:
                        return self._simple_outcome(step)
                    raise RuntimeError("boot_efi: command dispatched but success criterion not observed")
                return self._simple_outcome(step)
        raise RuntimeError("boot_efi: failed to detect UEFI prompt")

    def _step_uefi_shell_run(self, step: dict) -> Tuple[str, OutcomeMatch]:
        source = step.get("source")
        if not source:
            raise ValueError("uefi_shell_run requires source")
        binary_name = self._resolve_value(step.get("binary_name"))
        if not binary_name:
            binary_name = (self.ctx.get("request") or {}).get("binary_name")
        if not binary_name:
            raise ValueError("uefi_shell_run requires binary_name")
        fs = step.get("fs", "fs3")
        prompt_timeout_s = int(step.get("prompt_timeout_s", 60))
        select_timeout_s = int(step.get("select_timeout_s", 30))
        boot_manager_timeout_s = int(step.get("boot_manager_timeout_s", 30))
        shell_timeout_s = int(step.get("shell_timeout_s", 30))
        fs_timeout_s = int(step.get("fs_timeout_s", 10))
        error_timeout_s = int(step.get("error_timeout_s", 2))
        binding = self.ctx["sources"].get(source)
        if not binding:
            raise ValueError(f"unknown source {source}")

        # Use a moving cursor so this step only matches fresh serial output.
        _, cursor = binding.read_since(1 << 60)

        def wait_any(patterns: List[str], timeout_s: int) -> int:
            nonlocal cursor
            idx, cursor = self._wait_any_pattern_on_binding(
                binding=binding,
                cursor=cursor,
                patterns=patterns,
                timeout_s=timeout_s,
            )
            if idx != -1:
                return idx
            return -1

        # Wait for UEFI prompt and enter menu
        idx = wait_any(
            [
                r"Enter to continue boot\.",
                r"Press ESCAPE for boot options",
                r"Press ESC to enter Setup",
                r"ESC\s+to enter Setup",
                r"F11\s+to enter Boot Manager Menu",
            ],
            prompt_timeout_s,
        )
        if idx == -1:
            raise RuntimeError("Failed to get UEFI prompt")
        # Prefer ESC path first; with fresh-cursor matching this aligns key timing
        # with the actual prompt and avoids stale matches from earlier boot text.
        for _ in range(3):
            binding.write("\x1b")
            time.sleep(0.15)

        # Wait for UEFI menu (fallback to F11 if ESC path does not open a menu)
        idx = wait_any(
            [r"Select Entry", r"Please select boot device"],
            select_timeout_s,
        )
        if idx == -1:
            binding.write("\x1b[23~")  # F11
            idx = wait_any(
                [r"Select Entry", r"Please select boot device"],
                select_timeout_s,
            )
            if idx == -1:
                raise RuntimeError("Failed to get UEFI Select Entry menu")
        time.sleep(0.2)
        if idx == 0:
            # "Select Entry" menu -> Boot Manager -> UEFI Shell
            binding.write("\x1b[B")  # Down
            time.sleep(0.3)
            binding.write("\x1b[B")  # Down
            time.sleep(0.3)
            binding.write("\r")      # Enter

            # Wait for Boot Manager
            if wait_any([r"Esc=Exit|ESC to exit"], boot_manager_timeout_s) != 0:
                raise RuntimeError("Failed to get Boot Manager menu")
            time.sleep(1)
            binding.write("\x1b[A")  # Up (UEFI Shell)
            time.sleep(0.3)
            binding.write("\r")      # Enter
        else:
            # "Please select boot device" menu -> select UEFI Shell directly
            for _ in range(6):
                binding.write("\x1b[B")
                time.sleep(0.2)
            binding.write("\r")

        # Wait for Shell prompt (handle startup.nsh delay)
        while True:
            idx = wait_any(
                [r"Shell>", r"Press ESC in \d+ seconds"],
                shell_timeout_s,
            )
            if idx == 0:
                break
            if idx == 1:
                binding.write(" ")
                continue
            raise RuntimeError("Failed to get Shell prompt")

        fs_cmd = fs
        if not fs_cmd.endswith(":"):
            fs_cmd = f"{fs_cmd}:"
        binding.write(f"{fs_cmd}\r")

        fs_prompt = re.escape(fs_cmd.upper()) + r"\\>"
        if wait_any([fs_prompt], fs_timeout_s) != 0:
            raise RuntimeError(f"Failed to switch to {fs_cmd}")

        binding.write(f"{binary_name}\r")
        idx = wait_any(
            [r"is not recognized as an internal or external command", r".+"],
            error_timeout_s,
        )
        if idx == 0:
            raise RuntimeError(f"Binary not found on target: {binary_name}")

        return self._simple_outcome(step)

    def _step_upload(self, step: dict, kind: str) -> Tuple[str, OutcomeMatch]:
        self._check_cancel()
        local_path = self._resolve_value(step.get("local_path"))
        if not local_path:
            if kind == "kernel":
                local_path = self.ctx.get("kernel_image")
            else:
                local_path = self.ctx.get("request", {}).get("binary_path")
        if not local_path:
            raise ValueError("upload step missing local_path")
        target_user = step.get("target_user", "root")
        target_ip = self._resolve_value(step.get("target_ip")) or self.ctx.get("target_ip")
        target_path = self._resolve_value(step.get("target_path"))
        if not target_path:
            raise ValueError("upload step missing target_path")
        method = step.get("method", "scp")
        if method == "scp":
            if not target_ip:
                raise ValueError("upload step requires target_ip for method=scp")
            local_sha = self._sha256_file(Path(str(local_path)))
            self._scp_upload_with_verification(
                local_path=Path(str(local_path)),
                target_user=str(target_user),
                target_ip=str(target_ip),
                target_path=str(target_path),
                local_sha=local_sha,
                skip_if_same=True,
                atomic_replace=False,
                error_prefix=f"{kind} upload",
            )
        elif method == "local_copy":
            target_file = Path(target_path)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = target_file.with_name(f".{target_file.name}.tmp.{os.getpid()}")
            shutil.copy2(str(local_path), str(tmp_file))
            os.replace(str(tmp_file), str(target_file))
        else:
            raise ValueError(f"unknown upload method: {method}")
        return self._simple_outcome(step)

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _ssh_run_capture(self, target_user: str, target_ip: str, cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "BatchMode=yes",
                "-o", "GSSAPIAuthentication=no",
                f"{target_user}@{target_ip}",
                cmd,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def _remote_sha256(self, target_user: str, target_ip: str, target_path: str) -> Optional[str]:
        target_q = shlex.quote(target_path)
        cmd = f"if [ -f {target_q} ]; then sha256sum {target_q} | awk '{{print $1}}'; fi"
        proc = self._ssh_run_capture(target_user, target_ip, cmd)
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise RuntimeError(f"remote sha256 query failed for {target_path}: {stderr or 'unknown error'}")
        digest = (proc.stdout or "").strip()
        if not digest:
            return None
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise RuntimeError(f"invalid remote sha256 output for {target_path}: {digest}")
        return digest.lower()

    def _remote_sync(self, target_user: str, target_ip: str, target_path: str, error_prefix: str) -> None:
        sync_proc = self._ssh_run_capture(target_user, target_ip, "sync && sync")
        if sync_proc.returncode != 0:
            stderr = (sync_proc.stderr or "").strip()
            raise RuntimeError(
                f"{error_prefix} remote sync failed for {target_path}: {stderr or 'unknown error'}"
            )

    def _scp_upload_with_verification(
        self,
        *,
        local_path: Path,
        target_user: str,
        target_ip: str,
        target_path: str,
        local_sha: str,
        skip_if_same: bool,
        atomic_replace: bool,
        error_prefix: str,
    ) -> None:
        if skip_if_same:
            remote_sha = self._remote_sha256(target_user, target_ip, target_path)
            if remote_sha == local_sha:
                return

        upload_path = target_path
        if atomic_replace:
            upload_path = f"{target_path}.autopilot-tmp-{os.getpid()}"

        subprocess.run(
            [
                "scp",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "BatchMode=yes",
                "-o", "GSSAPIAuthentication=no",
                str(local_path),
                f"{target_user}@{target_ip}:{upload_path}",
            ],
            check=True,
        )

        self._remote_sync(target_user, target_ip, upload_path, error_prefix)

        uploaded_sha = self._remote_sha256(target_user, target_ip, upload_path)
        if uploaded_sha != local_sha:
            if atomic_replace:
                cleanup_cmd = f"rm -f {shlex.quote(upload_path)}"
                self._ssh_run_capture(target_user, target_ip, cleanup_cmd)
            raise RuntimeError(
                f"{error_prefix} remote hash mismatch for {upload_path}: "
                f"expected {local_sha} got {uploaded_sha}"
            )

        if atomic_replace:
            move_cmd = f"mv -f {shlex.quote(upload_path)} {shlex.quote(target_path)}"
            proc = self._ssh_run_capture(target_user, target_ip, move_cmd)
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                raise RuntimeError(f"{error_prefix} atomic move failed: {stderr or 'unknown error'}")

            self._remote_sync(target_user, target_ip, target_path, f"{error_prefix} after move")

        verify_path = target_path if atomic_replace else upload_path
        verify_sha = self._remote_sha256(target_user, target_ip, verify_path)
        if verify_sha != local_sha:
            raise RuntimeError(
                f"{error_prefix} final remote hash mismatch for {verify_path}: "
                f"expected {local_sha} got {verify_sha}"
            )

    def _step_upload_file(self, step: dict) -> Tuple[str, OutcomeMatch]:
        self._check_cancel()
        local_path_value = self._resolve_value(step.get("local_path"))
        if not local_path_value:
            raise ValueError("upload_file requires local_path")
        local_path = Path(str(local_path_value))
        if not local_path.exists():
            raise ValueError(f"upload_file local_path does not exist: {local_path}")

        target_user = step.get("target_user")
        if not target_user:
            raise ValueError("upload_file requires target_user")
        target_path = self._resolve_value(step.get("target_path"))
        if not target_path:
            raise ValueError("upload_file requires target_path")

        method = step.get("method", "scp")
        skip_if_same = step.get("skip_if_same")
        atomic_replace = bool(step.get("atomic_replace", True))
        local_sha = self._sha256_file(local_path)

        if method == "local_copy":
            target_file = Path(str(target_path))
            if skip_if_same == "sha256" and target_file.exists():
                remote_sha = self._sha256_file(target_file)
                if remote_sha == local_sha:
                    return self._simple_outcome(step)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            if atomic_replace:
                tmp_file = target_file.with_name(f".{target_file.name}.tmp.{os.getpid()}")
                shutil.copy2(str(local_path), str(tmp_file))
                if self._sha256_file(tmp_file) != local_sha:
                    tmp_file.unlink(missing_ok=True)
                    raise RuntimeError(f"upload_file local hash mismatch after copy to {tmp_file}")
                os.replace(str(tmp_file), str(target_file))
            else:
                shutil.copy2(str(local_path), str(target_file))
                if self._sha256_file(target_file) != local_sha:
                    raise RuntimeError(f"upload_file local hash mismatch after copy to {target_file}")
            return self._simple_outcome(step)

        if method != "scp":
            raise ValueError(f"upload_file unknown method: {method}")

        target_ip = self._resolve_value(step.get("target_ip")) or self.ctx.get("target_ip")
        if not target_ip:
            raise ValueError("upload_file requires target_ip for method=scp")

        if skip_if_same == "sha256":
            skip_remote_match = True
        elif skip_if_same not in (None, ""):
            raise ValueError(f"upload_file unknown skip_if_same mode: {skip_if_same}")
        else:
            skip_remote_match = False

        self._scp_upload_with_verification(
            local_path=local_path,
            target_user=str(target_user),
            target_ip=str(target_ip),
            target_path=str(target_path),
            local_sha=local_sha,
            skip_if_same=skip_remote_match,
            atomic_replace=atomic_replace,
            error_prefix="upload_file",
        )
        return self._simple_outcome(step)

    def _step_reboot(self, step: dict) -> Tuple[str, OutcomeMatch]:
        import subprocess
        self._check_cancel()
        method = step.get("method", "ssh")
        if method == "ssh":
            target_user = step.get("target_user", "root")
            target_ip = self._resolve_value(step.get("target_ip")) or self.ctx.get("target_ip")
            subprocess.run([
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                f"{target_user}@{target_ip}", "reboot"
            ])
        else:
            self.ctx["board"].boot(False)
        return self._simple_outcome(step)

    def _step_ssh_cmd(self, step: dict) -> Tuple[str, OutcomeMatch]:
        import subprocess
        self._check_cancel()
        target_user = step.get("target_user", "root")
        target_ip = self._resolve_value(step.get("target_ip")) or self.ctx.get("target_ip")
        cmd = step.get("cmd")
        if not cmd:
            raise ValueError("ssh_cmd requires cmd")
        timeout_s = step.get("timeout_s")
        run_args = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes",
            "-o", "GSSAPIAuthentication=no",
            f"{target_user}@{target_ip}",
            cmd,
        ]
        if timeout_s is None:
            subprocess.run(run_args, check=True)
        else:
            subprocess.run(run_args, check=True, timeout=int(timeout_s))
        return self._simple_outcome(step)

    def _step_ssh_wait_ready(self, step: dict) -> Tuple[str, OutcomeMatch]:
        import subprocess

        self._check_cancel()
        target_user = step.get("target_user", "root")
        target_ip = self._resolve_value(step.get("target_ip")) or self.ctx.get("target_ip")
        cmd = step.get("cmd")
        if not cmd:
            raise ValueError("ssh_wait_ready requires cmd")

        per_try_timeout_s = float(step.get("per_try_timeout_s", 1))
        total_timeout_s = float(step.get("total_timeout_s", 10))
        retry_interval_s = float(step.get("retry_interval_s", 1))
        if per_try_timeout_s <= 0 or total_timeout_s <= 0:
            raise ValueError("ssh_wait_ready requires positive per_try_timeout_s and total_timeout_s")

        deadline = time.time() + total_timeout_s
        last_error = ""
        attempts = 0
        probe_start = time.time()
        while time.time() < deadline:
            self._check_cancel()
            attempts += 1
            attempt_started_at = time.time()
            run_args = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "BatchMode=yes",
                "-o", "GSSAPIAuthentication=no",
                f"{target_user}@{target_ip}",
                cmd,
            ]
            try:
                subprocess.run(run_args, check=True, timeout=per_try_timeout_s)
                elapsed = time.time() - probe_start
                print(
                    f"ssh_wait_ready success target={target_user}@{target_ip} attempts={attempts} elapsed_s={elapsed:.3f}",
                    flush=True,
                )
                return self._simple_outcome(step)
            except subprocess.TimeoutExpired:
                elapsed = time.time() - attempt_started_at
                last_error = f"timeout(after={elapsed:.3f}s)"
            except subprocess.CalledProcessError as exc:
                last_error = f"exit={exc.returncode}"
            remaining = deadline - time.time()
            print(
                "ssh_wait_ready attempt failed "
                f"target={target_user}@{target_ip} "
                f"attempt={attempts} last_error={last_error} remaining_s={max(0.0, remaining):.3f}",
                flush=True,
            )
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(retry_interval_s, max(0.1, remaining)))

        elapsed = time.time() - probe_start
        raise RuntimeError(
            "ssh_wait_ready timed out "
            f"after {total_timeout_s}s attempts={attempts} elapsed_s={elapsed:.3f} "
            f"(last_error={last_error})"
        )

    def _step_call_chain(self, step: dict) -> Tuple[str, OutcomeMatch]:
        name = step["chain"]
        chain = self._load_named_chain(name)
        stack = list(self.ctx.get("chain_stack", []))
        if name in stack:
            path = " -> ".join(stack + [name])
            raise ChainValidationError(f"call_chain recursion detected: {path}")
        sub_ctx = dict(self.ctx)
        sub_ctx["chain_name"] = name
        sub_ctx["chain_stack"] = stack + [name]
        runner = ChainRunner(chain, sub_ctx, self.recorder, finalize_on_exit=False)
        status = runner.run()
        label = "pass" if status == "pass" else "fail"
        child_outcome = runner.last_step_result.outcome if runner.last_step_result else None
        outcomes = step.get("outcomes", [])
        for outcome in outcomes:
            if outcome.get("label") == label:
                next_step = outcome.get("next", step.get("on_timeout", "fail"))
                return next_step, OutcomeMatch(
                    label,
                    next_step,
                    child_outcome.pattern if child_outcome else None,
                    child_outcome.source if child_outcome else None,
                    child_outcome.log_path if child_outcome else None,
                    child_outcome.log_offset if child_outcome else None,
                )
        next_step = step.get("on_timeout", "fail")
        return next_step, OutcomeMatch(
            label,
            next_step,
            child_outcome.pattern if child_outcome else None,
            child_outcome.source if child_outcome else None,
            child_outcome.log_path if child_outcome else None,
            child_outcome.log_offset if child_outcome else None,
        )

    def _task_registry(self) -> dict:
        registry = self.ctx.get("task_registry")
        if not registry:
            raise ValueError("task_registry not configured")
        return registry

    def _run_registry_task(self, task_name: str, chain_name: str) -> None:
        registry = self._task_registry()
        status = "fail"
        try:
            chain = self._load_named_chain(chain_name)
            with registry["lock"]:
                task = registry["tasks"][task_name]
                cancel_flag = task["cancel"]
            sub_ctx = dict(self.ctx)
            sub_ctx["cancel_flag"] = cancel_flag
            sub_ctx["chain_name"] = chain_name
            sub_ctx["chain_stack"] = list(self.ctx.get("chain_stack", [])) + [f"task:{task_name}", chain_name]
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", task_name)
            recorder = ChainRecorder(
                self.ctx["result_dir"],
                filename=f"chain.task.{safe_name}.json",
            )
            runner = ChainRunner(chain, sub_ctx, recorder)
            result = runner.run()
            status = "pass" if result == "pass" else "fail"
        except Exception:
            status = "fail"
        finally:
            with registry["lock"]:
                task = registry["tasks"].get(task_name)
                if task:
                    task["status"] = status
                    task["finished_at"] = time.time()

    def _step_task_spawn(self, step: dict) -> Tuple[str, OutcomeMatch]:
        task_name = str(step.get("task", "")).strip()
        chain_name = str(step.get("chain", "")).strip()
        if not task_name:
            raise ValueError("task_spawn requires non-empty task")
        if not chain_name:
            raise ValueError("task_spawn requires non-empty chain")

        # Validate chain reference before spawning thread.
        self._load_named_chain(chain_name)
        registry = self._task_registry()
        with registry["lock"]:
            existing = registry["tasks"].get(task_name)
            if existing and existing.get("thread") and existing["thread"].is_alive():
                raise ValueError(f"task_spawn task already running: {task_name}")
            cancel_flag = threading.Event()
            task = {
                "name": task_name,
                "chain": chain_name,
                "status": "running",
                "started_at": time.time(),
                "finished_at": None,
                "owner_request_id": self.ctx.get("request_id"),
                "cancel": cancel_flag,
                "thread": None,
            }
            thread = threading.Thread(
                target=self._run_registry_task,
                args=(task_name, chain_name),
                daemon=True,
            )
            task["thread"] = thread
            registry["tasks"][task_name] = task
            thread.start()
        return self._simple_outcome(step)

    def _step_task_join(self, step: dict) -> Tuple[str, OutcomeMatch]:
        task_names = [str(task).strip() for task in step.get("tasks", [])]
        if not task_names or any(not t for t in task_names):
            raise ValueError("task_join requires non-empty tasks list")
        reduce_mode = str(step.get("reduce", "")).strip()
        if reduce_mode not in ("any_pass", "all_pass"):
            raise ValueError("task_join requires reduce=any_pass|all_pass")
        timeout_s = int(step.get("timeout_s", 60))
        deadline = time.time() + timeout_s
        registry = self._task_registry()
        latched_decision = None

        while time.time() < deadline:
            self._check_cancel()
            with registry["lock"]:
                missing = [name for name in task_names if name not in registry["tasks"]]
                if missing:
                    raise ValueError(f"task_join unknown tasks: {', '.join(missing)}")
                tasks = [registry["tasks"][name] for name in task_names]
                statuses = [str(task.get("status", "running")) for task in tasks]
                all_stopped = all(not task["thread"].is_alive() for task in tasks)

                if latched_decision is None:
                    if reduce_mode == "any_pass":
                        if any(status == "pass" for status in statuses):
                            latched_decision = "pass"
                        elif all_stopped and all(status in ("fail", "canceled") for status in statuses):
                            latched_decision = "fail"
                    elif reduce_mode == "all_pass":
                        if any(status == "fail" for status in statuses):
                            latched_decision = "fail"
                        elif all_stopped:
                            latched_decision = "pass" if all(status == "pass" for status in statuses) else "fail"

                    if latched_decision == "pass" and reduce_mode == "any_pass":
                        for task in tasks:
                            if task.get("status") == "running":
                                task["cancel"].set()
                    if latched_decision == "fail" and reduce_mode == "all_pass":
                        for task in tasks:
                            if task.get("status") == "running":
                                task["cancel"].set()

                if latched_decision is not None and all_stopped:
                    return self._match_labeled_outcome(step, latched_decision)
            time.sleep(0.1)

        return self._match_labeled_outcome(step, "timeout")

    def _step_signal_set(self, step: dict) -> Tuple[str, OutcomeMatch]:
        signal_name = str(step.get("signal", "")).strip()
        if not signal_name:
            raise ValueError("signal_set requires non-empty signal")
        registry = self._task_registry()
        with registry["cond"]:
            state = registry["signals"].setdefault(signal_name, {"count": 0, "updated_at": None})
            state["count"] = int(state.get("count", 0)) + 1
            state["updated_at"] = time.time()
            registry["cond"].notify_all()
        return self._simple_outcome(step)

    def _step_signal_wait(self, step: dict) -> Tuple[str, OutcomeMatch]:
        signal_name = str(step.get("signal", "")).strip()
        if not signal_name:
            raise ValueError("signal_wait requires non-empty signal")
        timeout_s = int(step.get("timeout_s", 60))
        consume = bool(step.get("consume", True))
        deadline = time.time() + timeout_s
        registry = self._task_registry()
        with registry["cond"]:
            while time.time() < deadline:
                self._check_cancel()
                state = registry["signals"].setdefault(signal_name, {"count": 0, "updated_at": None})
                count = int(state.get("count", 0))
                if count > 0:
                    if consume:
                        state["count"] = count - 1
                        state["updated_at"] = time.time()
                    return self._simple_outcome(step)
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                registry["cond"].wait(timeout=min(0.2, remaining))
        return self._match_labeled_outcome(step, "timeout")

    def _match_labeled_outcome(self, step: dict, label: str) -> Tuple[str, OutcomeMatch]:
        outcomes = step.get("outcomes", [])
        for outcome in outcomes:
            if outcome.get("label") == label:
                next_step = outcome.get("next", step.get("on_timeout", "fail"))
                return next_step, OutcomeMatch(label, next_step, None, None, None, None)
        next_step = step.get("on_timeout", "fail")
        return next_step, OutcomeMatch(label, next_step, None, None, None, None)

    def _snapshot_parallel_group_unlocked(self, group: dict) -> dict:
        winner = group.get("winner")
        winner_copy = None
        if winner:
            winner_copy = {
                "branch": winner.get("branch"),
                "status": winner.get("status"),
                "finished_at": winner.get("finished_at"),
            }
        branches = {}
        for name, state in group.get("branches", {}).items():
            branches[name] = {
                "chain": state.get("chain"),
                "monitor": bool(state.get("monitor", False)),
                "status": state.get("status"),
                "finished_at": state.get("finished_at"),
                "cancel_reason": state.get("cancel_reason"),
            }
        join_state = group.get("join")
        join_copy = None
        if isinstance(join_state, dict):
            join_copy = {
                "step": join_state.get("step"),
                "chain": join_state.get("chain"),
                "reduce": join_state.get("reduce"),
                "decision": join_state.get("decision"),
                "joined_groups": list(join_state.get("joined_groups", [])),
                "decided_at": join_state.get("decided_at"),
            }
        return {
            "split_step": group.get("split_step"),
            "split_chain": group.get("split_chain"),
            "winner": winner_copy,
            "branches": branches,
            "join": join_copy,
        }

    def _snapshot_parallel_group(self, group: dict) -> dict:
        with group["lock"]:
            return self._snapshot_parallel_group_unlocked(group)

    def _record_parallel_group_state(self, group_name: str) -> None:
        groups = self.ctx.get("parallel_groups", {})
        group = groups.get(group_name)
        if not group:
            return
        self.recorder.record_parallel_group(group_name, self._snapshot_parallel_group(group))

    def _run_parallel_branch(self, group_name: str, branch_name: str, branch: dict) -> None:
        groups = self.ctx.get("parallel_groups", {})
        group = groups[group_name]
        branch_chain_name = branch["chain"]
        status = "fail"
        try:
            chain = self._load_named_chain(branch_chain_name)
            cancel_flag = group["cancel_flags"][branch_name]
            sub_ctx = dict(self.ctx)
            sub_ctx["cancel_flag"] = cancel_flag
            sub_ctx["chain_name"] = branch_chain_name
            sub_ctx["chain_stack"] = list(self.ctx.get("chain_stack", [])) + [f"{group_name}:{branch_name}", branch_chain_name]
            safe_group = re.sub(r"[^A-Za-z0-9._-]+", "_", group_name)
            safe_branch = re.sub(r"[^A-Za-z0-9._-]+", "_", branch_name)
            recorder = ChainRecorder(
                self.ctx["result_dir"],
                filename=f"chain.parallel.{safe_group}.{safe_branch}.json",
            )
            runner = ChainRunner(chain, sub_ctx, recorder)
            result = runner.run()
            status = "pass" if result == "pass" else "fail"
        except Exception:
            status = "fail"
        finally:
            with group["lock"]:
                branch_state = group["branches"][branch_name]
                branch_state["status"] = status
                branch_state["finished_at"] = time.time()
                if group.get("winner") is None and status in ("pass", "fail"):
                    group["winner"] = {
                        "branch": branch_name,
                        "status": status,
                        "finished_at": branch_state["finished_at"],
                    }
                    group["winner_event"].set()
                    for other_name, cancel in group["cancel_flags"].items():
                        if other_name != branch_name:
                            cancel.set()
                            other_state = group["branches"].get(other_name)
                            if other_state and other_state.get("status") == "running":
                                other_state["cancel_reason"] = f"winner:{branch_name}"
            self._record_parallel_group_state(group_name)

    def _step_split(self, step: dict) -> Tuple[str, OutcomeMatch]:
        group_name = str(step.get("group", "")).strip()
        if not group_name:
            raise ValueError("split requires non-empty group")
        branches_cfg = step.get("branches", [])
        groups = self.ctx.setdefault("parallel_groups", {})
        if group_name in groups:
            raise ValueError(f"split group already exists: {group_name}")

        group = {
            "lock": threading.Lock(),
            "winner_event": threading.Event(),
            "winner": None,
            "split_step": self.ctx.get("_current_step_name"),
            "split_chain": self.ctx.get("chain_name"),
            "branches": {},
            "cancel_flags": {},
            "threads": {},
        }
        groups[group_name] = group

        for branch in branches_cfg:
            branch_name = str(branch["name"]).strip()
            branch_chain_name = str(branch["chain"]).strip()
            monitor = bool(branch.get("monitor", False))
            cancel_flag = threading.Event()
            group["cancel_flags"][branch_name] = cancel_flag
            group["branches"][branch_name] = {
                "chain": branch_chain_name,
                "monitor": monitor,
                "status": "running",
                "finished_at": None,
                "cancel_reason": None,
            }
            thread = threading.Thread(
                target=self._run_parallel_branch,
                args=(group_name, branch_name, {"chain": branch_chain_name}),
                daemon=True,
            )
            group["threads"][branch_name] = thread
            thread.start()

        self._record_parallel_group_state(group_name)
        return self._simple_outcome(step)

    def _step_join(self, step: dict) -> Tuple[str, OutcomeMatch]:
        join_groups = step.get("join_groups", [])
        if not isinstance(join_groups, list) or not join_groups:
            raise ValueError("join requires non-empty join_groups")
        group_names = []
        for group_name in join_groups:
            group_name = str(group_name).strip()
            if not group_name:
                raise ValueError("join join_groups contains empty group name")
            group_names.append(group_name)
        reduce_mode = str(step.get("reduce", "")).strip()
        if reduce_mode not in ("any_pass", "all_pass"):
            raise ValueError("join requires reduce=any_pass|all_pass")
        groups = self.ctx.get("parallel_groups", {})
        for group_name in group_names:
            if group_name not in groups:
                raise ValueError(f"join unknown group: {group_name}")

        timeout_s = int(step.get("timeout_s", 60))
        deadline = time.time() + timeout_s
        latched_decision = None

        while time.time() < deadline:
            self._check_cancel()
            statuses = []
            all_stopped = True
            for group_name in group_names:
                group = groups[group_name]
                with group["lock"]:
                    group_statuses = [str(s.get("status", "running")) for s in group.get("branches", {}).values()]
                    statuses.extend(group_statuses)
                    if any(t.is_alive() for t in group.get("threads", {}).values()):
                        all_stopped = False

            if latched_decision is None:
                decision = None
                if reduce_mode == "any_pass":
                    if any(status == "pass" for status in statuses):
                        decision = "pass"
                    elif statuses and all(status in ("pass", "fail", "canceled") for status in statuses):
                        decision = "fail"
                elif reduce_mode == "all_pass":
                    if any(status == "fail" for status in statuses):
                        decision = "fail"
                    elif statuses and all(status in ("pass", "canceled") for status in statuses):
                        if all(status == "pass" for status in statuses):
                            decision = "pass"
                        else:
                            decision = "fail"
                if decision:
                    latched_decision = decision
                    for group_name in group_names:
                        group = groups[group_name]
                        with group["lock"]:
                            if decision == "pass" and reduce_mode == "any_pass":
                                winner = group.get("winner")
                                winner_branch = winner.get("branch") if isinstance(winner, dict) else None
                                for branch_name, cancel in group["cancel_flags"].items():
                                    if branch_name != winner_branch:
                                        cancel.set()
                                        other_state = group["branches"].get(branch_name)
                                        if other_state and other_state.get("status") == "running":
                                            other_state["cancel_reason"] = f"winner:{winner_branch}" if winner_branch else "join_decision:pass"
                            if decision == "fail" and reduce_mode == "all_pass":
                                for branch_name, cancel in group["cancel_flags"].items():
                                    cancel.set()
                                    other_state = group["branches"].get(branch_name)
                                    if other_state and other_state.get("status") == "running":
                                        other_state["cancel_reason"] = "join_decision:fail"
                            group["join"] = {
                                "step": self.ctx.get("_current_step_name"),
                                "chain": self.ctx.get("chain_name"),
                                "reduce": reduce_mode,
                                "decision": decision,
                                "joined_groups": list(group_names),
                                "decided_at": time.time(),
                            }
                        self._record_parallel_group_state(group_name)

            if latched_decision is not None and all_stopped:
                for group_name in group_names:
                    groups.pop(group_name, None)
                return self._match_labeled_outcome(step, latched_decision)
            time.sleep(0.1)

        for group_name in group_names:
            self._record_parallel_group_state(group_name)
        return self._match_labeled_outcome(step, "timeout")

    def _step_analyze_logs(self, step: dict) -> Tuple[str, OutcomeMatch]:
        import subprocess
        cmd = step.get("command")
        if cmd:
            if isinstance(cmd, list):
                cmd = [self._resolve_value(item) for item in cmd]
            else:
                cmd = self._resolve_value(cmd)
            subprocess.run(cmd, check=True)
        return self._simple_outcome(step)

    def _step_interactive_console(self, step: dict) -> Tuple[str, OutcomeMatch]:
        console_manager = self.ctx.get("console_manager")
        if not console_manager:
            raise ValueError("console_manager not configured")
        sessions_cfg = step.get("sessions", [])
        if not sessions_cfg:
            raise ValueError("interactive_console requires sessions")
        console_dir = self.ctx["result_dir"] / "console"
        console_dir.mkdir(parents=True, exist_ok=True)
        sessions_meta = []
        sessions = []
        session_states = []
        for sess in sessions_cfg:
            name = sess.get("name")
            port = sess.get("port")
            source = sess.get("source")
            profile_name = sess.get("profile", "linux-yocto")
            if isinstance(port, str) and port.startswith("env:"):
                env_key = port.split("env:", 1)[1]
                port = os.environ.get(env_key)
                if not port:
                    raise ValueError(f"interactive session env port missing: {env_key}")
            if not name or not port:
                raise ValueError("interactive session requires name and port")
            profile = load_profile(console_manager.profiles_dir, profile_name)
            binding = None
            if source:
                binding = self.ctx["sources"].get(source)
            if binding:
                runtime_dir = console_manager.runtime_dir / f"{self.ctx['request_id']}-{name}"
                runtime_dir.mkdir(parents=True, exist_ok=True)
                cmd_dir = runtime_dir / "cmd"
                resp_dir = runtime_dir / "resp"
                cmd_dir.mkdir(parents=True, exist_ok=True)
                resp_dir.mkdir(parents=True, exist_ok=True)
                session_states.append({
                    "mode": "binding",
                    "name": name,
                    "source": source,
                    "binding": binding,
                    "profile": profile,
                    "runtime_dir": runtime_dir,
                    "cmd_dir": cmd_dir,
                    "resp_dir": resp_dir,
                    "offset": 0,
                    "seen_shell": False,
                })
                sessions_meta.append({
                    "session_id": f"{self.ctx['request_id']}-{name}",
                    "name": name,
                    "port": port,
                    "pty_path": binding.tty if isinstance(binding.tty, str) and binding.tty.startswith("/dev/pts/") else None,
                    "interactive": True,
                    "kind": "binding",
                    "profile": profile_name,
                    "log_path": str(binding.log_path),
                    "events_path": str(binding.log_path),
                })
            else:
                session = console_manager.create_session(
                    request_id=self.ctx["request_id"],
                    name=name,
                    port=port,
                    profile_name=profile_name,
                    log_dir=console_dir
                )
                sessions.append(session)
                sessions_meta.append({
                    "session_id": session.session_id,
                    "name": name,
                    "port": port,
                    "pty_path": port if isinstance(port, str) and port.startswith("/dev/pts/") else None,
                    "interactive": True,
                    "kind": "serial",
                    "profile": profile_name,
                    "log_path": str(session.log_path),
                    "events_path": str(session.events_path),
                })
        manifest = {
            "request_id": self.ctx["request_id"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "active",
            "sessions": sessions_meta,
        }
        (console_dir / "sessions.json").write_text(json.dumps(manifest, indent=2))
        if step.get("auto_login", True):
            for session in sessions:
                try:
                    session.perform_login(timeout_s=60)
                    session.seen_shell = True
                except Exception:
                    pass
        idle_timeout = int(step.get("idle_timeout_s", 900))
        exit_after_shell = step.get("exit_after_shell", True)
        hold_open = step.get("hold_open", True)
        last_activity = time.time()
        exit_patterns = step.get("exit_patterns")
        if not hold_open:
            manifest["status"] = "active"
            (console_dir / "sessions.json").write_text(json.dumps(manifest, indent=2))
            return self._simple_outcome(step)
        active = True
        while active:
            self._check_cancel()
            event = self._poll_event()
            if event and event.kind == "abort":
                self._handle_abort()
            # Handle binding-based sessions
            for state in list(session_states):
                runtime_dir = state["runtime_dir"]
                cmd_dir = state["cmd_dir"]
                resp_dir = state["resp_dir"]
                close_flag = runtime_dir / "close"
                if close_flag.exists():
                    close_flag.unlink(missing_ok=True)
                    session_states.remove(state)
                    continue
                for cmd_file in sorted(cmd_dir.glob("*.json")):
                    cmd_data = json.loads(cmd_file.read_text())
                    cmd_id = cmd_data.get("cmd_id", cmd_file.stem)
                    command = cmd_data.get("command", "")
                    append_newline = cmd_data.get("append_newline", True)
                    wait_for_prompt = cmd_data.get("wait_for_prompt", True)
                    prompt_override = cmd_data.get("prompt_override")
                    timeout_s = int(cmd_data.get("timeout_s", 10))
                    binding = state["binding"]
                    text = command + ("\n" if append_newline else "")
                    binding.write(text)
                    if wait_for_prompt:
                        prompt = prompt_override or state["profile"].shell_prompt
                        output, new_offset, matched = self._wait_for_binding_prompt(
                            binding, state["offset"], prompt, timeout_s
                        )
                        state["offset"] = new_offset
                        if matched:
                            state["seen_shell"] = True
                        resp = {"output": output, "new_offset": new_offset, "matched": matched}
                    else:
                        resp = {"output": "", "new_offset": binding._total_bytes, "matched": True}
                    resp["cmd_id"] = cmd_id
                    (resp_dir / f"{cmd_id}.json").write_text(json.dumps(resp, indent=2))
                    cmd_file.unlink(missing_ok=True)
                    last_activity = time.time()

                exit_regex = exit_patterns or state["profile"].login_prompt
                if exit_regex and (state.get("seen_shell") or not exit_after_shell):
                    _, new_offset, matched = self._wait_for_binding_prompt(
                        state["binding"], state["offset"], exit_regex, 1
                    )
                    state["offset"] = new_offset
                    if matched:
                        active = False
                        break

            # Handle console_manager sessions
            for session in list(sessions):
                runtime_dir = session.runtime_dir
                close_flag = runtime_dir / "close"
                if close_flag.exists():
                    close_flag.unlink(missing_ok=True)
                    console_manager.close_session(session.session_id)
                    sessions.remove(session)
                    continue
                cmd_dir = runtime_dir / "cmd"
                resp_dir = runtime_dir / "resp"
                for cmd_file in sorted(cmd_dir.glob("*.json")):
                    cmd_data = json.loads(cmd_file.read_text())
                    cmd_id = cmd_data.get("cmd_id", cmd_file.stem)
                    command = cmd_data.get("command", "")
                    append_newline = cmd_data.get("append_newline", True)
                    wait_for_prompt = cmd_data.get("wait_for_prompt", True)
                    prompt_override = cmd_data.get("prompt_override")
                    timeout_s = int(cmd_data.get("timeout_s", 10))
                    result = session.send_command(
                        command=command,
                        append_newline=append_newline,
                        wait_for_prompt=wait_for_prompt,
                        prompt_regex=prompt_override,
                        timeout_s=timeout_s
                    )
                    result["cmd_id"] = cmd_id
                    (resp_dir / f"{cmd_id}.json").write_text(json.dumps(result, indent=2))
                    cmd_file.unlink(missing_ok=True)
                    last_activity = time.time()

                exit_regex = exit_patterns or session.profile.login_prompt
                if exit_regex:
                    if getattr(session, "seen_shell", False) or not exit_after_shell:
                        start_offset = session.get_offset()
                        _, _, matched = session.wait_for_prompt(exit_regex, start_offset, 1)
                        if matched:
                            active = False
                            break

            if not sessions and not session_states:
                break
            if idle_timeout > 0 and (time.time() - last_activity) > idle_timeout:
                active = False
            time.sleep(0.2)

        manifest["status"] = "closed"
        (console_dir / "sessions.json").write_text(json.dumps(manifest, indent=2))
        return self._simple_outcome(step)

    def _wait_for_binding_prompt(
        self,
        binding: SourceBinding,
        offset: int,
        prompt_regex: str,
        timeout_s: int,
    ) -> Tuple[str, int, bool]:
        compiled = re.compile(prompt_regex, re.MULTILINE)
        buffer = ""
        deadline = time.time() + timeout_s
        cursor = offset

        while time.time() < deadline:
            self._check_cancel()
            data, cursor = binding.read_since(cursor)
            if data:
                chunk = data.decode("utf-8", errors="ignore")
                buffer += chunk
                if len(buffer) > 65536:
                    buffer = buffer[-65536:]
                if compiled.search(normalize_tty_text(buffer)):
                    return buffer, cursor, True
            else:
                time.sleep(0.1)
        return buffer, cursor, False

    def _poll_event(self) -> Optional[Event]:
        try:
            return self.event_queue.get_nowait()
        except queue.Empty:
            return None

    def _resolve_value(self, value):
        if isinstance(value, str):
            format_ctx = dict(self.ctx)
            code_root = Path(__file__).resolve().parent
            format_ctx.setdefault("code_root", str(code_root))
            format_ctx.setdefault("chains_dir", str(code_root / "chains"))
            format_ctx.setdefault("profiles_dir", str(code_root / "profiles"))
            sources = self.ctx.get("sources")
            if sources and hasattr(sources, "analysis_log_paths"):
                for source_name, analysis_path in sources.analysis_log_paths().items():
                    key = re.sub(r"[^A-Za-z0-9_]", "_", source_name)
                    if analysis_path:
                        format_ctx.setdefault(f"{key}_analysis_log", analysis_path)
            format_ctx.update(self.ctx.get("request", {}))
            return value.format(**format_ctx)
        return value

    def _load_named_chain(self, name: str) -> dict:
        chain_name = self._resolve_chain_name(name)
        loader = self.ctx.get("load_chain")
        if not loader:
            raise ValueError("load_chain callback not configured")
        return loader(chain_name)

    def _resolve_chain_name(self, name: str) -> str:
        overrides = self.ctx.get("platform_overrides", {}) or {}
        aliases = overrides.get("chain_aliases", {}) or {}
        return str(aliases.get(name, name))

    def _step_set_overrides(self, step: dict) -> Tuple[str, OutcomeMatch]:
        updates = step.get("overrides")
        if not isinstance(updates, dict):
            raise ValueError("set_overrides requires dictionary field 'overrides'")
        current = self.ctx.setdefault("platform_overrides", {})
        self._deep_merge_dict(current, updates)
        return self._simple_outcome(step)

    def _step_set_test_verdict(self, step: dict) -> Tuple[str, OutcomeMatch]:
        verdict = str(step.get("verdict", "")).strip()
        if verdict not in ("pass", "fail"):
            raise ValueError("set_test_verdict requires verdict=pass|fail")
        self.ctx["test_verdict"] = verdict
        self.recorder.set_test_verdict(verdict)
        return self._simple_outcome(step)

    def _deep_merge_dict(self, current: dict, updates: dict) -> None:
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                self._deep_merge_dict(current[key], value)
            else:
                current[key] = value

    def _validate_external_refs(self) -> None:
        monitor_validation_cache: Dict[str, bool] = {}
        for step_name, step in self.chain.get("steps", {}).items():
            step_type = step.get("type")
            if step_type == "call_chain":
                chain_name = step.get("chain")
                if not chain_name:
                    raise ChainValidationError(f"step {step_name} missing chain name")
                try:
                    self._load_named_chain(str(chain_name))
                except Exception as exc:
                    raise ChainValidationError(
                        f"step {step_name} references unknown chain {chain_name}: {exc}"
                    ) from exc
                continue
            if step_type == "task_spawn":
                chain_name = str(step.get("chain", "")).strip()
                if not chain_name:
                    raise ChainValidationError(f"step {step_name} missing chain name")
                try:
                    self._load_named_chain(chain_name)
                except Exception as exc:
                    raise ChainValidationError(
                        f"step {step_name} references unknown chain {chain_name}: {exc}"
                    ) from exc
                continue
            if step_type == "split":
                for branch in step.get("branches", []):
                    branch_name = str(branch.get("name", "")).strip()
                    chain_name = str(branch.get("chain", "")).strip()
                    if not chain_name:
                        raise ChainValidationError(
                            f"step {step_name} branch {branch_name} missing chain"
                        )
                    try:
                        self._load_named_chain(chain_name)
                    except Exception as exc:
                        raise ChainValidationError(
                            f"step {step_name} branch {branch_name} references unknown chain {chain_name}: {exc}"
                        ) from exc
                    if branch.get("monitor", False):
                        if chain_name not in monitor_validation_cache:
                            monitor_validation_cache[chain_name] = self._is_fail_only_chain(chain_name, set())
                        if not monitor_validation_cache[chain_name]:
                            raise ChainValidationError(
                                f"step {step_name} branch {branch_name} monitor chain {chain_name} is not fail-only"
                            )

    def _is_fail_only_chain(self, chain_name: str, visited: set) -> bool:
        resolved_name = self._resolve_chain_name(chain_name)
        if resolved_name in visited:
            return True
        visited.add(resolved_name)
        chain = self._load_named_chain(resolved_name)
        steps = chain.get("steps", {})
        for step in steps.values():
            if step.get("type") == "pass":
                return False
            if step.get("type") == "call_chain":
                nested = str(step.get("chain", "")).strip()
                if nested and not self._is_fail_only_chain(nested, visited):
                    return False
        return True

    def _spawn_detached_chain(self, chain_name: str, filename_prefix: str) -> None:
        subchain = self._load_named_chain(chain_name)
        cancel_flag = threading.Event()
        sub_ctx = dict(self.ctx)
        sub_ctx["cancel_flag"] = cancel_flag
        sub_ctx["chain_name"] = chain_name
        sub_ctx["chain_stack"] = list(self.ctx.get("chain_stack", [])) + [chain_name]
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", chain_name)
        recorder = ChainRecorder(
            self.ctx["result_dir"],
            filename=f"{filename_prefix}.{safe_name}.json",
        )
        runner = ChainRunner(subchain, sub_ctx, recorder)
        thread = threading.Thread(target=runner.run, daemon=True)
        thread.start()

    def _handle_abort(self) -> None:
        registry = self.ctx.get("task_registry")
        if registry:
            with registry["lock"]:
                for task in registry.get("tasks", {}).values():
                    cancel = task.get("cancel")
                    if cancel:
                        cancel.set()
        for group in self.ctx.get("parallel_groups", {}).values():
            for cancel in group.get("cancel_flags", {}).values():
                cancel.set()
        recovery = self.ctx.get("abort_recovery_chain")
        if recovery:
            try:
                self._spawn_detached_chain(str(recovery), "chain.abort_recovery")
            except Exception:
                pass
        raise AbortRun()

    def _handle_cancel(self) -> None:
        registry = self.ctx.get("task_registry")
        request_id = self.ctx.get("request_id")
        if registry:
            with registry["lock"]:
                for task in registry.get("tasks", {}).values():
                    if task.get("owner_request_id") != request_id:
                        continue
                    cancel = task.get("cancel")
                    if cancel:
                        cancel.set()
        for group in self.ctx.get("parallel_groups", {}).values():
            for cancel in group.get("cancel_flags", {}).values():
                cancel.set()

    def _check_cancel(self) -> None:
        if self.cancel_flag.is_set():
            raise CancelRun()

    def _set_status(self, extra: str) -> None:
        ui = self.ctx.get("ui")
        if not ui:
            return
        step = extra
        if extra.startswith("step="):
            step = extra.split("=", 1)[1]
        request_id = self.ctx.get("request_id", "-")
        profile = self.ctx.get("profile", "-")
        chain_name = self.ctx.get("chain_name", "-")
        start = self.ctx.get("request_start")
        elapsed = int(time.time() - start) if start else 0
        if hasattr(ui, "state"):
            ui.state.set_request(str(request_id), str(profile), str(chain_name))
            ui.state.set_step(step, elapsed)
