import json
import os
import queue
import re
import select
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import serial

from console_sessions import load_profile


class ChainValidationError(Exception):
    pass


class AbortRun(Exception):
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


class ChainRecorder:
    def __init__(self, result_dir: Path):
        self.result_dir = result_dir
        self.steps: List[dict] = []
        self.forks: Dict[str, dict] = {}
        self.overall_status: Optional[str] = None
        self.abort_reason: Optional[str] = None
        self.path = result_dir / "chain.json"

    def record_step(self, result: StepResult) -> None:
        entry = {
            "step": result.step,
            "status": result.status,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "error_code": result.error_code,
            "error_message": result.error_message,
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
        self.steps.append(entry)
        self._flush()

    def record_fork(self, name: str, status: str) -> None:
        self.forks[name] = {"status": status, "updated_at": time.time()}
        self._flush()

    def finalize(self, status: str, abort_reason: Optional[str] = None) -> None:
        self.overall_status = status
        self.abort_reason = abort_reason
        self._flush()

    def _flush(self) -> None:
        payload = {
            "overall_status": self.overall_status,
            "abort_reason": self.abort_reason,
            "steps": self.steps,
            "forks": self.forks,
        }
        self.path.write_text(json.dumps(payload, indent=2))


class Event:
    def __init__(self, kind: str, payload: Optional[dict] = None):
        self.kind = kind
        self.payload = payload or {}


class SourceBinding:
    def __init__(
        self,
        source: str,
        tty: str,
        log_path: Path,
        baud: int = 115200,
        emit=None,
    ):
        self.source = source
        self.tty = tty
        self.log_path = log_path
        self.baud = baud
        self.emit = emit
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._buffer = bytearray()
        self._base_offset = 0
        self._total_bytes = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._serial = serial.Serial(self.tty, baudrate=self.baud, timeout=0.1)
        self._thread.start()

    def _run(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "ab", buffering=0) as f:
            while not self._stop.is_set():
                try:
                    data = self._serial.read(1024)
                except Exception:
                    time.sleep(0.1)
                    continue
                if not data:
                    continue
                if self.emit:
                    self.emit(self.source, data)
                f.write(data)
                with self._lock:
                    self._buffer.extend(data)
                    self._total_bytes += len(data)
                    max_buf = 1024 * 1024
                    if len(self._buffer) > max_buf:
                        trim = len(self._buffer) - max_buf
                        del self._buffer[:trim]
                        self._base_offset += trim

    def write(self, text: str) -> None:
        with self._lock:
            self._serial.write(text.encode("utf-8", errors="ignore"))

    def read_since(self, offset: int) -> Tuple[bytes, int]:
        with self._lock:
            if offset < self._base_offset:
                offset = self._base_offset
            rel = offset - self._base_offset
            data = bytes(self._buffer[rel:])
            new_offset = self._base_offset + len(self._buffer)
        return data, new_offset

    def stop(self) -> None:
        self._stop.set()
        try:
            self._serial.close()
        except Exception:
            pass


class SourceManager:
    def __init__(self, result_dir: Path, tui=None):
        self.result_dir = result_dir
        self.tui = tui
        self.sources: Dict[str, SourceBinding] = {}
        self.tty_to_source: Dict[str, str] = {}

    def set_result_dir(self, result_dir: Path) -> None:
        self.result_dir = result_dir

    def map_source(self, source: str, tty: str, log_rel: str, baud: int = 115200) -> None:
        if source in self.sources:
            self.sources[source].stop()
            del self.sources[source]
        log_path = self.result_dir / log_rel
        binding = SourceBinding(source, tty, log_path, baud=baud, emit=self._emit)
        self.sources[source] = binding
        self.tty_to_source[tty] = source

    def _emit(self, source: str, data: bytes) -> None:
        if self.tui:
            self.tui.emit_output(source, data)

    def get(self, source: str) -> Optional[SourceBinding]:
        return self.sources.get(source)

    def stop_all(self) -> None:
        for binding in list(self.sources.values()):
            binding.stop()
        self.sources.clear()


class TUIManager:
    def __init__(self):
        self.enabled = sys.stdin.isatty()
        self.active_window = 1
        self.window_map: Dict[int, str] = {}
        self.interactive_enabled = False
        self.status_text = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._event_queue: Optional[queue.Queue] = None
        self._input_handler = None
        self._rows = 0
        self._use_bottom = True

    def start(self, event_queue: queue.Queue) -> None:
        if not self.enabled:
            return
        self._event_queue = event_queue
        self._rows = shutil.get_terminal_size((80, 24)).lines
        self._init_status_line()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import termios
        import tty
        old = termios.tcgetattr(sys.stdin)
        try:
            tty.setraw(sys.stdin.fileno())
            while not self._stop.is_set():
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if not r:
                    continue
                ch = sys.stdin.read(1)
                if ch != "\x01":
                    if self.interactive_enabled:
                        self._send_input(ch)
                    continue
                nxt = sys.stdin.read(1)
                if nxt in ("x", "X"):
                    self._event_queue.put(Event("exit"))
                elif nxt in ("w", "W"):
                    self._event_queue.put(Event("list_windows"))
                elif nxt in ("r", "R"):
                    self._event_queue.put(Event("abort"))
                elif nxt in ("i", "I"):
                    self.interactive_enabled = not self.interactive_enabled
                    state = "enabled" if self.interactive_enabled else "disabled"
                    self._print(f"[TUI] interactive {state}\n")
                    self.set_status(self.status_text)
                elif nxt.isdigit():
                    self._event_queue.put(Event("switch_window", {"window": int(nxt)}))
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
            self._reset_status_line()

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def bind_window(self, window: int, source: str, title: Optional[str] = None) -> None:
        self.window_map[window] = source
        self._print(f"[TUI] window {window} -> {source}{' (' + title + ')' if title else ''}\n")
        self.set_status(self.status_text)

    def handle_event(self, event: Event) -> None:
        if event.kind == "switch_window":
            window = int(event.payload.get("window", 1))
            self.active_window = window
            self._print(f"[TUI] switched to window {window}\n")
            self.set_status(self.status_text)
        elif event.kind == "list_windows":
            lines = ["[TUI] window list:"]
            for win in sorted(self.window_map.keys()):
                src = self.window_map[win]
                marker = "*" if win == self.active_window else " "
                lines.append(f"  {marker} {win}: {src}")
            self._print("\n".join(lines) + "\n")

    def emit_output(self, source: str, data: bytes) -> None:
        for win, src in self.window_map.items():
            if src == source and win == self.active_window:
                self._print(self._sanitize_output(data))
                break

    def set_input_handler(self, handler) -> None:
        self._input_handler = handler

    def set_status(self, text: str) -> None:
        if not self.enabled:
            return
        self.status_text = text
        clean = self._strip_ansi(text)
        max_len = max(0, shutil.get_terminal_size((80, 24)).columns - 1)
        clean = clean[:max_len]
        with self._lock:
            self._render_status(clean)

    def _send_input(self, ch: str) -> None:
        source = self.window_map.get(self.active_window)
        if not source or not self._input_handler:
            return
        try:
            self._input_handler(source, ch)
        except Exception:
            pass

    def _print(self, text: str) -> None:
        with self._lock:
            sys.stdout.write(text)
            sys.stdout.flush()

    def _strip_ansi(self, text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)

    def _sanitize_output(self, data: bytes) -> str:
        text = data.decode("utf-8", errors="ignore")
        # Keep SGR (color) sequences, strip other CSI/OSC controls that can move cursor.
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", lambda m: m.group(0) if m.group(0).endswith("m") else "", text)
        # Remove OSC sequences (e.g., title changes)
        text = re.sub(r"\x1b\].*?\x07", "", text)
        # Remove save/restore cursor (ESC 7/8)
        text = text.replace("\x1b7", "").replace("\x1b8", "")
        return text

    def _init_status_line(self) -> None:
        if not self.enabled:
            return
        if self._use_bottom and self._rows >= 2:
            sys.stdout.write(f"\x1b[1;{self._rows - 1}r")
        sys.stdout.flush()

    def _reset_status_line(self) -> None:
        if not self.enabled:
            return
        sys.stdout.write("\x1b[r")
        sys.stdout.flush()

    def _render_status(self, text: str) -> None:
        if self._use_bottom and self._rows >= 1:
            row = self._rows
            sys.stdout.write("\x1b7")
            sys.stdout.write(f"\x1b[{row};1H")
            sys.stdout.write("\x1b[2K")
            sys.stdout.write("\x1b[1;37;44m")
            sys.stdout.write(text)
            sys.stdout.write("\x1b[0m")
            sys.stdout.write("\x1b8")
            sys.stdout.flush()


def validate_chain(chain: dict) -> None:
    if "entry" not in chain:
        raise ChainValidationError("chain entry missing")
    if "steps" not in chain:
        raise ChainValidationError("chain steps missing")
    steps = chain["steps"]
    if chain["entry"] not in steps:
        raise ChainValidationError("entry step not found in steps")
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


class ChainRunner:
    def __init__(self, chain: dict, ctx: dict, recorder: ChainRecorder):
        self.chain = chain
        self.ctx = ctx
        self.recorder = recorder
        self.event_queue: queue.Queue = ctx["event_queue"]
        self.cancel_flag = ctx["cancel_flag"]
        self.ctx.setdefault("chain_name", self.ctx.get("profile", "-"))
        self.ctx.setdefault("subchain_name", "-")

    def run(self) -> str:
        validate_chain(self.chain)
        current = self.chain["entry"]
        try:
            while True:
                if self.cancel_flag.is_set():
                    self.recorder.finalize("failed", abort_reason="canceled")
                    return "failed"
                step = self.chain["steps"][current]
                result, next_step = self._run_step(current, step)
                self.recorder.record_step(result)
                if step["type"] in ("pass", "fail"):
                    self.recorder.finalize(step["type"])
                    return step["type"]
                current = next_step
        except AbortRun:
            self.recorder.finalize("failed", abort_reason="user_abort")
            return "failed"

    def _run_step(self, name: str, step: dict) -> Tuple[StepResult, str]:
        started = time.time()
        self._set_status(f"step={name}")
        try:
            next_step, outcome = self._dispatch_step(name, step)
            status = "ok"
            error_code = None
            error_message = None
            if outcome and outcome.label == "timeout":
                status = "timeout"
                error_code = "timeout"
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
        result = StepResult(
            step=name,
            status=status,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
            started_at=started,
            finished_at=finished,
        )
        return result, next_step

    def _dispatch_step(self, name: str, step: dict) -> Tuple[str, OutcomeMatch]:
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
        if step_type == "map_window":
            return self._step_map_window(step)
        if step_type == "send_cmd":
            return self._step_send_cmd(step)
        if step_type == "boot_menu":
            return self._step_boot_menu(step)
        if step_type == "wait_pattern":
            return self._step_wait_pattern(step)
        if step_type == "upload_kernel":
            return self._step_upload(step, kind="kernel")
        if step_type == "upload_efi":
            return self._step_upload(step, kind="efi")
        if step_type == "reboot":
            return self._step_reboot(step)
        if step_type == "fork":
            return self._step_fork(step)
        if step_type == "join":
            return self._step_join(step)
        if step_type == "analyze_logs":
            return self._step_analyze_logs(step)
        if step_type == "interactive_console":
            return self._step_interactive_console(step)
        raise ChainValidationError(f"unknown step type: {step_type}")

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
        if not tty:
            defaults = self.ctx.get("default_ttys", {})
            tty = defaults.get(source)
        if isinstance(tty, str) and tty.startswith("env:"):
            env_key = tty.split("env:", 1)[1]
            tty = os.environ.get(env_key, "")
        if not tty:
            raise ValueError("map_source requires tty")
        log_rel = step.get("log", f"console/{source}.jsonl")
        baud = int(step.get("baud", 115200))
        self.ctx["sources"].map_source(source, tty, log_rel, baud=baud)
        return self._simple_outcome(step)

    def _step_map_window(self, step: dict) -> Tuple[str, OutcomeMatch]:
        window = int(step["window"])
        source = step["source"]
        title = step.get("title")
        self.ctx["tui"].bind_window(window, source, title=title)
        return self._simple_outcome(step)

    def _step_send_cmd(self, step: dict) -> Tuple[str, OutcomeMatch]:
        source = step["source"]
        cmd = step["cmd"]
        binding = self.ctx["sources"].get(source)
        if not binding:
            raise ValueError(f"unknown source {source}")
        suffix = step.get("suffix", "\n")
        binding.write(cmd + suffix)
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

    def _step_wait_pattern(self, step: dict) -> Tuple[str, OutcomeMatch]:
        timeout_s = int(step.get("timeout_s", 30))
        outcomes = step.get("outcomes", [])
        start = time.time()
        cursors: Dict[str, int] = {}
        while time.time() - start < timeout_s:
            event = self._poll_event()
            if event:
                if event.kind == "abort":
                    self._handle_abort()
                if event.kind == "exit":
                    self.ctx["exit_flag"].set()
                    raise AbortRun()
                if event.kind in ("switch_window", "list_windows"):
                    self.ctx["tui"].handle_event(event)
            for outcome in outcomes:
                pattern = outcome.get("pattern")
                source = outcome.get("source")
                if not pattern or not source:
                    continue
                binding = self.ctx["sources"].get(source)
                if not binding:
                    continue
                cursor = cursors.get(source, 0)
                data, new_cursor = binding.read_since(cursor)
                cursors[source] = new_cursor
                if not data:
                    continue
                match = re.search(pattern, data.decode("utf-8", errors="ignore"), re.MULTILINE)
                if match:
                    offset = binding._base_offset + match.start()
                    log_path = str(binding.log_path)
                    next_step = outcome.get("next", step.get("on_timeout", "fail"))
                    return next_step, OutcomeMatch(
                        label=outcome.get("label", "match"),
                        next_step=next_step,
                        pattern=pattern,
                        source=source,
                        log_path=log_path,
                        log_offset=offset,
                    )
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

    def _step_upload(self, step: dict, kind: str) -> Tuple[str, OutcomeMatch]:
        import subprocess

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
        subprocess.run([
            "scp", "-o", "StrictHostKeyChecking=no",
            str(local_path),
            f"{target_user}@{target_ip}:{target_path}"
        ], check=True)
        return self._simple_outcome(step)

    def _step_reboot(self, step: dict) -> Tuple[str, OutcomeMatch]:
        import subprocess
        method = step.get("method", "ssh")
        if method == "ssh":
            target_user = step.get("target_user", "root")
            target_ip = self._resolve_value(step.get("target_ip")) or self.ctx.get("target_ip")
            subprocess.run([
                "ssh", "-o", "StrictHostKeyChecking=no",
                f"{target_user}@{target_ip}", "reboot"
            ])
        else:
            self.ctx["board"].boot(False)
        return self._simple_outcome(step)

    def _step_fork(self, step: dict) -> Tuple[str, OutcomeMatch]:
        name = step["chain"]
        subchain = self.chain.get("subchains", {}).get(name)
        if not subchain:
            raise ValueError(f"unknown subchain {name}")
        cancel_flag = threading.Event()
        sub_ctx = dict(self.ctx)
        sub_ctx["cancel_flag"] = cancel_flag
        sub_ctx["subchain_name"] = name
        recorder = self.ctx["fork_recorders"].setdefault(
            name, ChainRecorder(self.ctx["result_dir"])
        )
        runner = ChainRunner(subchain, sub_ctx, recorder)
        thread = threading.Thread(target=runner.run, daemon=True)
        self.ctx["forks"][name] = {
            "thread": thread,
            "cancel": cancel_flag,
        }
        thread.start()
        self.recorder.record_fork(name, "running")
        return self._simple_outcome(step)

    def _step_join(self, step: dict) -> Tuple[str, OutcomeMatch]:
        name = step.get("chain")
        if name:
            fork = self.ctx["forks"].get(name)
            if fork:
                fork["thread"].join()
        else:
            for fork in self.ctx["forks"].values():
                fork["thread"].join()
        return self._simple_outcome(step)

    def _step_analyze_logs(self, step: dict) -> Tuple[str, OutcomeMatch]:
        import subprocess
        cmd = step.get("command")
        if cmd:
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
            data, cursor = binding.read_since(cursor)
            if data:
                chunk = data.decode("utf-8", errors="ignore")
                buffer += chunk
                if len(buffer) > 65536:
                    buffer = buffer[-65536:]
                if compiled.search(buffer):
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
            return value.format(**self.ctx.get("request", {}), **self.ctx)
        return value

    def _handle_abort(self) -> None:
        for fork in self.ctx["forks"].values():
            fork["cancel"].set()
        recovery = self.ctx.get("abort_recovery_chain")
        if recovery and recovery in self.chain.get("subchains", {}):
            step = {"type": "fork", "chain": recovery, "outcomes": [{"label": "started", "next": "fail"}]}
            try:
                self._step_fork(step)
            except Exception:
                pass
        raise AbortRun()

    def _set_status(self, extra: str) -> None:
        tui = self.ctx.get("tui")
        if not tui or not tui.enabled:
            return
        win = tui.active_window
        source = tui.window_map.get(win, "-")
        input_state = "on" if tui.interactive_enabled else "off"
        request_id = self.ctx.get("request_id", "-")
        profile = self.ctx.get("profile", "-")
        chain_name = self.ctx.get("chain_name", "-")
        subchain = self.ctx.get("subchain_name", "-")
        start = self.ctx.get("request_start")
        elapsed = f"{int(time.time() - start)}s" if start else "-"
        text = (
            f"{extra} | req={request_id} profile={profile} chain={chain_name} sub={subchain} "
            f"| win={win} src={source} | input={input_state} | elapsed={elapsed}"
        )
        tui.set_status(text)
