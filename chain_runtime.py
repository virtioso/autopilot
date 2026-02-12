import json
import os
import queue
import re
import threading
import time
import shutil
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import serial

from console_sessions import load_profile


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


class NoopChainRecorder:
    def record_step(self, result: StepResult) -> None:
        return

    def record_fork(self, name: str, status: str) -> None:
        return

    def finalize(self, status: str, abort_reason: Optional[str] = None) -> None:
        return


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
        live_log_path: Optional[Path] = None,
        baud: int = 115200,
        emit=None,
    ):
        self.source = source
        self.tty = tty
        self.log_path = log_path
        self.live_log_path = live_log_path
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
        if self.live_log_path:
            self.live_log_path.parent.mkdir(parents=True, exist_ok=True)
        live_ctx = open(self.live_log_path, "ab", buffering=0) if self.live_log_path else nullcontext()
        with open(self.log_path, "ab", buffering=0) as f, live_ctx as live_file:
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

    def write(self, text: str) -> None:
        self.write_bytes(text.encode("utf-8", errors="ignore"))

    def write_bytes(self, payload: bytes) -> None:
        with self._lock:
            self._serial.write(payload)

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
    def __init__(self, result_dir: Path, ui=None):
        self.result_dir = result_dir
        self.ui = ui
        self.sources: Dict[str, SourceBinding] = {}
        self.tty_to_source: Dict[str, str] = {}

    def set_result_dir(self, result_dir: Path) -> None:
        self.result_dir = result_dir

    def map_source(self, source: str, tty: str, log_rel: str, baud: int = 115200) -> None:
        if source in self.sources:
            self.sources[source].stop()
            del self.sources[source]
        log_path = self.result_dir / log_rel
        live_log_path = None
        if self.ui and hasattr(self.ui, "state"):
            live_log_path = self.ui.state.live_path_for_source(source)
        binding = SourceBinding(
            source,
            tty,
            log_path,
            live_log_path=live_log_path,
            baud=baud,
            emit=self._emit,
        )
        self.sources[source] = binding
        self.tty_to_source[tty] = source

    def _emit(self, source: str, data: bytes) -> None:
        if self.ui:
            self.ui.emit_output(source, data)

    def get(self, source: str) -> Optional[SourceBinding]:
        return self.sources.get(source)

    def stop_all(self) -> None:
        for binding in list(self.sources.values()):
            binding.stop()
        self.sources.clear()


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
        if step.get("type") == "call_chain":
            labels = {outcome.get("label") for outcome in step.get("outcomes", [])}
            if "pass" not in labels or "fail" not in labels:
                raise ChainValidationError(
                    f"step {name} call_chain requires outcomes for labels 'pass' and 'fail'"
                )


class ChainRunner:
    def __init__(self, chain: dict, ctx: dict, recorder: ChainRecorder):
        self.chain = chain
        self.ctx = ctx
        self.recorder = recorder
        self.event_queue: queue.Queue = ctx["event_queue"]
        self.cancel_flag = ctx["cancel_flag"]
        self.ctx.setdefault("chain_name", self.ctx.get("profile", "-"))
        stack = self.ctx.setdefault("chain_stack", [])
        if not stack:
            chain_name = str(self.ctx.get("chain_name", "")).strip()
            if chain_name:
                self.ctx["chain_stack"] = [chain_name]

    def run(self) -> str:
        validate_chain(self.chain)
        self._validate_external_refs()
        current = self.chain["entry"]
        try:
            while True:
                self._check_cancel()
                step = self.chain["steps"][current]
                result, next_step = self._run_step(current, step)
                self.recorder.record_step(result)
                if step["type"] in ("pass", "fail"):
                    self.recorder.finalize(step["type"])
                    return step["type"]
                current = next_step
        except CancelRun:
            self.recorder.finalize("failed", abort_reason="canceled")
            return "failed"
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
        self._check_cancel()
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
        if step_type == "uefi_shell_run":
            return self._step_uefi_shell_run(step)
        if step_type == "wait_pattern":
            return self._step_wait_pattern(step)
        if step_type == "upload_kernel":
            return self._step_upload(step, kind="kernel")
        if step_type == "upload_efi":
            return self._step_upload(step, kind="efi")
        if step_type == "reboot":
            return self._step_reboot(step)
        if step_type == "ssh_cmd":
            return self._step_ssh_cmd(step)
        if step_type == "fork":
            return self._step_fork(step)
        if step_type == "call_chain":
            return self._step_call_chain(step)
        if step_type == "join":
            return self._step_join(step)
        if step_type == "analyze_logs":
            return self._step_analyze_logs(step)
        if step_type == "interactive_console":
            return self._step_interactive_console(step)
        if step_type == "set_overrides":
            return self._step_set_overrides(step)
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

    def _step_map_window(self, step: dict) -> Tuple[str, OutcomeMatch]:
        window = int(step["window"])
        source = step["source"]
        title = step.get("title")
        ui = self.ctx.get("ui")
        if ui and hasattr(ui, "bind_window"):
            ui.bind_window(window, source, title=title)
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
            self._check_cancel()
            event = self._poll_event()
            if event:
                if event.kind == "abort":
                    self._handle_abort()
                if event.kind == "exit":
                    self.ctx["exit_flag"].set()
                    raise AbortRun()
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

    def _wait_for_any_pattern(self, source: str, patterns: List[str], timeout_s: int) -> int:
        start = time.time()
        cursor = 0
        binding = self.ctx["sources"].get(source)
        if not binding:
            raise ValueError(f"unknown source {source}")
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
            text = data.decode("utf-8", errors="ignore")
            for idx, pattern in enumerate(patterns):
                if re.search(pattern, text, re.MULTILINE):
                    return idx
        return -1

    def _wait_for_pattern(self, source: str, pattern: str, timeout_s: int) -> bool:
        return self._wait_for_any_pattern(source, [pattern], timeout_s) == 0

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
                text = data.decode("utf-8", errors="ignore")
                for idx, pattern in enumerate(patterns):
                    if re.search(pattern, text, re.MULTILINE):
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
        import subprocess

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
            subprocess.run([
                "scp", "-o", "StrictHostKeyChecking=no",
                str(local_path),
                f"{target_user}@{target_ip}:{target_path}"
            ], check=True)
        elif method == "local_copy":
            target_file = Path(target_path)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = target_file.with_name(f".{target_file.name}.tmp.{os.getpid()}")
            shutil.copy2(str(local_path), str(tmp_file))
            os.replace(str(tmp_file), str(target_file))
        else:
            raise ValueError(f"unknown upload method: {method}")
        return self._simple_outcome(step)

    def _step_reboot(self, step: dict) -> Tuple[str, OutcomeMatch]:
        import subprocess
        self._check_cancel()
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
            "-o",
            "StrictHostKeyChecking=no",
            f"{target_user}@{target_ip}",
            cmd,
        ]
        if timeout_s is None:
            subprocess.run(run_args, check=True)
        else:
            subprocess.run(run_args, check=True, timeout=int(timeout_s))
        return self._simple_outcome(step)

    def _step_fork(self, step: dict) -> Tuple[str, OutcomeMatch]:
        name = step["chain"]
        subchain = self._load_named_chain(name)
        cancel_flag = threading.Event()
        sub_ctx = dict(self.ctx)
        sub_ctx["cancel_flag"] = cancel_flag
        sub_ctx["chain_name"] = name
        sub_ctx["chain_stack"] = list(self.ctx.get("chain_stack", []))
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
        recorder = NoopChainRecorder()
        runner = ChainRunner(chain, sub_ctx, recorder)
        status = runner.run()
        label = "pass" if status == "pass" else "fail"
        outcomes = step.get("outcomes", [])
        for outcome in outcomes:
            if outcome.get("label") == label:
                next_step = outcome.get("next", step.get("on_timeout", "fail"))
                return next_step, OutcomeMatch(label, next_step, None, None, None, None)
        next_step = step.get("on_timeout", "fail")
        return next_step, OutcomeMatch(label, next_step, None, None, None, None)

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
            format_ctx = dict(self.ctx)
            code_root = Path(__file__).resolve().parent
            format_ctx.setdefault("code_root", str(code_root))
            format_ctx.setdefault("chains_dir", str(code_root / "chains"))
            format_ctx.setdefault("profiles_dir", str(code_root / "profiles"))
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

    def _deep_merge_dict(self, current: dict, updates: dict) -> None:
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                self._deep_merge_dict(current[key], value)
            else:
                current[key] = value

    def _validate_external_refs(self) -> None:
        for step_name, step in self.chain.get("steps", {}).items():
            step_type = step.get("type")
            if step_type not in ("fork", "call_chain"):
                continue
            chain_name = step.get("chain")
            if not chain_name:
                raise ChainValidationError(f"step {step_name} missing chain name")
            try:
                self._load_named_chain(str(chain_name))
            except Exception as exc:
                raise ChainValidationError(
                    f"step {step_name} references unknown chain {chain_name}: {exc}"
                ) from exc

    def _handle_abort(self) -> None:
        for fork in self.ctx["forks"].values():
            fork["cancel"].set()
        recovery = self.ctx.get("abort_recovery_chain")
        if recovery:
            step = {"type": "fork", "chain": recovery, "outcomes": [{"label": "started", "next": "fail"}]}
            try:
                self._step_fork(step)
            except Exception:
                pass
        raise AbortRun()

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
