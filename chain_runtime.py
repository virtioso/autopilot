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
    chain_name: str
    chain_stack: List[str]


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
        # Always start from an empty UART state for deterministic pattern matching.
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()
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

    def purge(self) -> None:
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            self._buffer = bytearray()
            self._base_offset = 0
            self._total_bytes = 0


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
        )
        return result, next_step

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
            if step_type == "upload_kernel":
                return self._step_upload(step, kind="kernel")
            if step_type == "upload_efi":
                return self._step_upload(step, kind="efi")
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

    def _step_map_window(self, step: dict) -> Tuple[str, OutcomeMatch]:
        window = int(step["window"])
        source = step["source"]
        title = step.get("title")
        ui = self.ctx.get("ui")
        if ui and hasattr(ui, "bind_window"):
            ui.bind_window(window, source, title=title)
        return self._simple_outcome(step)

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

        # Start from fresh output to avoid stale prompt matches from previous boot phases.
        _, cursor = binding.read_since(1 << 60)
        # Force prompt redraw for already-idle UEFI shells/menu screens.
        binding.write("\r")
        last_probe_at = time.time()
        start = time.time()
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
                if now - last_probe_at >= 1.0:
                    binding.write("\r")
                    last_probe_at = now
                time.sleep(0.1)
                continue
            text = data.decode("utf-8", errors="ignore")
            if any(re.search(pattern, text, re.MULTILINE) for pattern in prompt_patterns):
                binding.write(f"{command}\r")
                if post_send_delay_s > 0:
                    time.sleep(post_send_delay_s)
                if success_patterns:
                    if not isinstance(success_patterns, list) or not success_patterns:
                        raise ValueError("boot_efi success_patterns must be a non-empty list when set")
                    success_deadline = time.time() + success_timeout_s
                    while time.time() < success_deadline:
                        self._check_cancel()
                        event = self._poll_event()
                        if event:
                            if event.kind == "abort":
                                self._handle_abort()
                            if event.kind == "exit":
                                self.ctx["exit_flag"].set()
                                raise AbortRun()
                        sdata, new_cursor = binding.read_since(cursor)
                        cursor = new_cursor
                        if not sdata:
                            time.sleep(0.1)
                            continue
                        stext = sdata.decode("utf-8", errors="ignore")
                        if any(re.search(pattern, stext, re.MULTILINE) for pattern in success_patterns):
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
        while time.time() < deadline:
            self._check_cancel()
            run_args = [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                f"{target_user}@{target_ip}",
                cmd,
            ]
            try:
                subprocess.run(run_args, check=True, timeout=per_try_timeout_s)
                return self._simple_outcome(step)
            except subprocess.TimeoutExpired:
                last_error = "timeout"
            except subprocess.CalledProcessError as exc:
                last_error = f"exit={exc.returncode}"
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(retry_interval_s, max(0.1, remaining)))

        raise RuntimeError(f"ssh_wait_ready timed out after {total_timeout_s}s (last_error={last_error})")

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
                    if task.get("name") == "prepare_next_run":
                        continue
                    cancel = task.get("cancel")
                    if cancel:
                        cancel.set()
            with registry["cond"]:
                state = registry["signals"].setdefault(
                    "prepare_next_run_go", {"count": 0, "updated_at": None}
                )
                state["count"] = int(state.get("count", 0)) + 1
                state["updated_at"] = time.time()
                registry["cond"].notify_all()
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
