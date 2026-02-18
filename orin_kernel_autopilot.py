#! /usr/bin/env python3

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import BoardControl
from chain_runtime import ChainRecorder, ChainRunner, ChainValidationError, Event, SourceManager, validate_chain
from console_sessions import ConsoleManager
from config import get_autopilot_dir, get_default_ttys, get_paths, get_target_ip
from extract_guest_dtb import extract_guest_dtbs
from tmux_ui import TmuxControlServer, TmuxUICompat, TmuxUIState, TmuxWindowManager, detect_tmux_session

SCRIPT_DIR = Path(__file__).resolve().parent
AUTOPILOT_DIR = get_autopilot_dir()
CHAINS_DIR = SCRIPT_DIR / "chains"

WORKSPACE = Path(os.environ.get("WORKSPACE", "/home/hlyytine/pkvm"))
KERNEL_DIR = WORKSPACE / "Linux_for_Tegra/source/kernel/linux"
KERNEL_IMAGE = KERNEL_DIR / "arch/arm64/boot/Image"
KERNEL_RELEASE_FILE = KERNEL_DIR / "include/config/kernel.release"

TARGET_IP = get_target_ip()
AUTOPILOT_PLATFORM = os.environ.get("AUTOPILOT_PLATFORM", "").strip()
DEFAULT_TTY0, DEFAULT_TTY1 = get_default_ttys()

PATHS = get_paths(str(AUTOPILOT_DIR))
PENDING_DIR = PATHS["pending"]
PROCESSING_DIR = PATHS["processing"]
COMPLETED_DIR = PATHS["completed"]
FAILED_DIR = PATHS["failed"]
RESULTS_DIR = PATHS["results"]
RUNTIME_DIR = PATHS["runtime"]


def ensure_dirs() -> None:
    for d in [PENDING_DIR, PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR, RESULTS_DIR, RUNTIME_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def cleanup() -> None:
    print("\nCleaning up...", flush=True)
    if PROCESSING_DIR.exists():
        for request_file in PROCESSING_DIR.glob("*.request"):
            try:
                request_file.rename(PENDING_DIR / request_file.name)
                print(f"Moved {request_file.name} back to pending", flush=True)
            except Exception as exc:
                print(f"Error moving {request_file.name}: {exc}", flush=True)


def handle_signal(signum, frame) -> None:
    cleanup()
    sys.exit(0)


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def _validate_chain_name(chain_name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", chain_name):
        raise ValueError(f"invalid chain name: {chain_name}")


def load_chain(chain_name: str) -> dict:
    _validate_chain_name(chain_name)
    chain_path = CHAINS_DIR / f"{chain_name}.json"
    if not chain_path.exists():
        raise ValueError(f"chain not found: {chain_name}")
    return json.loads(chain_path.read_text())


def validate_all_chains() -> None:
    errors = []
    for chain_path in sorted(CHAINS_DIR.glob("*.json")):
        try:
            chain = json.loads(chain_path.read_text())
            validate_chain(chain)
        except (json.JSONDecodeError, ChainValidationError, ValueError) as exc:
            errors.append(f"{chain_path.name}: {exc}")
        except Exception as exc:
            errors.append(f"{chain_path.name}: unexpected validation error: {exc}")
    if errors:
        raise RuntimeError("chain validation failed:\n - " + "\n - ".join(errors))


def run_chain(chain: dict, ctx: dict, recorder: ChainRecorder) -> str:
    runner = ChainRunner(chain, ctx, recorder)
    return runner.run()


def run_bootstrap_chain(
    chain_name: str,
    source_manager: SourceManager,
    board,
    event_queue: queue.Queue,
    cancel_flag: threading.Event,
    ui,
    ui_state: TmuxUIState,
    exit_flag: threading.Event,
    console_manager: ConsoleManager,
    platform_overrides: dict,
    task_registry: dict,
) -> str:
    chain = load_chain(chain_name)
    bootstrap_dir = RUNTIME_DIR / chain_name
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    source_manager.set_result_dir(bootstrap_dir)
    ctx = {
        "board": board,
        "sources": source_manager,
        "ui": ui,
        "event_queue": event_queue,
        "cancel_flag": cancel_flag,
        "result_dir": bootstrap_dir,
        "request_id": chain_name,
        "request": {},
        "profile": chain_name,
        "chain_name": chain_name,
        "request_start": time.time(),
        "target_ip": TARGET_IP,
        "kernel_image": KERNEL_IMAGE,
        "kernel_release": KERNEL_RELEASE_FILE.read_text().strip() if KERNEL_RELEASE_FILE.exists() else "unknown",
        "console_manager": console_manager,
        "abort_recovery_chain": "recovery_boot",
        "default_ttys": {"tty0": DEFAULT_TTY0, "tty1": DEFAULT_TTY1},
        "exit_flag": exit_flag,
        "load_chain": load_chain,
        "platform_overrides": platform_overrides,
        "task_registry": task_registry,
        "code_root": str(SCRIPT_DIR),
        "chains_dir": str(CHAINS_DIR),
        "profiles_dir": str(SCRIPT_DIR / "profiles"),
    }
    recorder = ChainRecorder(bootstrap_dir)
    try:
        ui_state.set_request(chain_name, chain_name, chain_name)
        return run_chain(chain, ctx, recorder)
    finally:
        ui_state.clear_request()


def write_post_run_dtb_artifacts(result_dir: Path, status: str) -> None:
    try:
        summary = extract_guest_dtbs(result_dir)
        payload = summary.to_json()
        payload["request_status"] = status
        payload["review_required"] = (
            status != "pass" and payload.get("generated_dts", 0) > 0
        )
        summary_path = summary.output_dir / "summary.json"
        summary_path.write_text(json.dumps(payload, indent=2))
        print(
            "DT artifacts:"
            f" dtb={payload.get('generated_dtb', 0)}"
            f" dts={payload.get('generated_dts', 0)}"
            f" summary={summary_path}",
            flush=True,
        )
        if payload["review_required"]:
            print(
                "DT review required: guest run failed; inspect generated DTS files",
                flush=True,
            )
    except Exception as exc:
        print(f"WARNING: DTB extraction failed: {exc}", flush=True)


def _append_sel4_failure_marker(result_dir: Path, message: str) -> None:
    sel4_log = result_dir / "console" / "sel4.log"
    sel4_log.parent.mkdir(parents=True, exist_ok=True)
    with sel4_log.open("a") as fh:
        fh.write(f"\nAUTOPILOT_FAIL: {message}\n")


def _has_ftrace_dump_evidence(result_dir: Path) -> bool:
    for log_name in ("console/tty0.raw", "console/sel4.log"):
        log_path = result_dir / log_name
        if not log_path.exists():
            continue
        try:
            content = log_path.read_text(errors="ignore")
        except Exception:
            continue
        if "=== BINARY TRANSFER START ===" in content or "TRACE_DUMP_TERMINAL:" in content:
            return True
    return False


def run_post_run_ftrace_pipeline(result_dir: Path) -> dict:
    """
    Enforce ftrace extraction/indexing/summary generation when dump evidence exists.
    """
    required = _has_ftrace_dump_evidence(result_dir)
    summary = {
        "required": required,
        "extracted": False,
        "indexed": False,
        "summary_generated": False,
        "dump_reason": None,
        "dump_reason_code": None,
    }
    summary_path = result_dir / "ftrace.summary.json"
    if not required:
        summary_path.write_text(json.dumps(summary, indent=2))
        return {"required": False, "ok": True, "summary": summary}

    extract_script = SCRIPT_DIR / "extract_ftrace.py"
    source_log = result_dir / "console" / "sel4.log"
    if not source_log.exists():
        source_log = result_dir / "console" / "tty0.raw"

    if not extract_script.exists() or not source_log.exists():
        return {
            "required": True,
            "ok": False,
            "error": "FTRACE_POSTPROCESS_MISSING_INPUTS",
            "summary": summary,
        }

    proc = subprocess.run(
        ["python3", str(extract_script), str(source_log), str(result_dir)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        return {
            "required": True,
            "ok": False,
            "error": f"FTRACE_EXTRACT_FAILED rc={proc.returncode}",
            "stderr": proc.stderr,
            "summary": summary,
        }

    bin_path = result_dir / "ftrace.bin"
    meta_path = result_dir / "ftrace.meta"
    idx_path = result_dir / "ftrace.idx"
    if not (bin_path.exists() and meta_path.exists() and idx_path.exists()):
        return {
            "required": True,
            "ok": False,
            "error": "FTRACE_POSTPROCESS_ARTIFACTS_MISSING",
            "summary": summary,
        }

    try:
        meta = json.loads(meta_path.read_text())
        header = meta.get("header", {})
        dump_reason = header["DUMP_REASON"]
        dump_reason_code = header["DUMP_REASON_CODE"]
    except Exception as exc:
        return {
            "required": True,
            "ok": False,
            "error": f"FTRACE_META_PARSE_FAILED ({exc})",
            "summary": summary,
        }

    summary.update({
        "extracted": True,
        "indexed": True,
        "summary_generated": True,
        "dump_reason": dump_reason,
        "dump_reason_code": dump_reason_code,
        "artifact_paths": {
            "bin": str(bin_path),
            "meta": str(meta_path),
            "idx": str(idx_path),
        },
    })
    summary_path.write_text(json.dumps(summary, indent=2))
    return {"required": True, "ok": True, "summary": summary}


def poll_idle_events(event_queue: queue.Queue, exit_flag: threading.Event) -> None:
    while True:
        try:
            event = event_queue.get_nowait()
        except queue.Empty:
            break
        if event.kind == "exit":
            exit_flag.set()

def watch_cancel_file(cancel_flag: threading.Event, cancel_path: Path, exit_flag: threading.Event) -> None:
    while not exit_flag.is_set() and not cancel_flag.is_set():
        if cancel_path.exists():
            cancel_flag.set()
            break
        time.sleep(0.2)


class PrepareLifecycle:
    def __init__(
        self,
        source_manager: SourceManager,
        board,
        event_queue: queue.Queue,
        ui,
        ui_state: TmuxUIState,
        exit_flag: threading.Event,
        console_manager: ConsoleManager,
        platform_overrides: dict,
        task_registry: dict,
    ):
        self.source_manager = source_manager
        self.board = board
        self.event_queue = event_queue
        self.ui = ui
        self.ui_state = ui_state
        self.exit_flag = exit_flag
        self.console_manager = console_manager
        self.platform_overrides = platform_overrides
        self.task_registry = task_registry

        self.state = "unknown"
        self.degraded_reason = None
        self.retry_count = 0
        self.last_probe = None
        self.last_prepare = None
        self.probe_chain = "boot_stock_linux"
        self.run_chain = "recovery_boot"
        self.max_retries = 3
        self.degraded_holds_queue = True
        self.status_path = RUNTIME_DIR / "prepare_state.json"
        self._reload_policy()
        self._write_status()

    def _reload_policy(self) -> None:
        lifecycle = self.platform_overrides.get("lifecycle", {}) or {}
        prepare = lifecycle.get("prepare", {}) or {}
        probe_chain = str(prepare.get("probe_chain", self.probe_chain)).strip()
        run_chain = str(prepare.get("run_chain", self.run_chain)).strip()
        if probe_chain:
            self.probe_chain = probe_chain
        if run_chain:
            self.run_chain = run_chain
        try:
            self.max_retries = max(0, int(prepare.get("max_retries", self.max_retries)))
        except Exception:
            self.max_retries = 3
        self.degraded_holds_queue = bool(prepare.get("degraded_holds_queue", True))

    def apply_overrides(self) -> None:
        self._reload_policy()
        self._write_status()

    def _stamp(self, status: str, error: str = None) -> dict:
        payload = {
            "at": time.time(),
            "status": status,
            "chain": None,
            "error": error,
        }
        return payload

    def _set_state(self, state: str, reason: str = None) -> None:
        self.state = state
        self.degraded_reason = reason
        self._write_status()

    def _write_status(self) -> None:
        payload = {
            "state": self.state,
            "degraded_reason": self.degraded_reason,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "last_probe": self.last_probe,
            "last_prepare": self.last_prepare,
            "policy": {
                "probe_chain": self.probe_chain,
                "run_chain": self.run_chain,
                "degraded_holds_queue": self.degraded_holds_queue,
            },
        }
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(json.dumps(payload, indent=2))

    def _run_chain_once(self, chain_name: str) -> tuple[bool, str]:
        try:
            status = run_bootstrap_chain(
                chain_name=chain_name,
                source_manager=self.source_manager,
                board=self.board,
                event_queue=self.event_queue,
                cancel_flag=threading.Event(),
                ui=self.ui,
                ui_state=self.ui_state,
                exit_flag=self.exit_flag,
                console_manager=self.console_manager,
                platform_overrides=self.platform_overrides,
                task_registry=self.task_registry,
            )
            return status == "pass", status
        except Exception as exc:
            return False, str(exc)

    def startup_probe(self) -> None:
        self._set_state("probing")
        ok, detail = self._run_chain_once(self.probe_chain)
        self.last_probe = self._stamp("pass" if ok else "fail", None if ok else detail)
        self.last_probe["chain"] = self.probe_chain
        if ok:
            self.retry_count = 0
            self._set_state("pass")
            return
        self.run_prepare_cycle(trigger="startup_probe_fail")

    def run_prepare_cycle(self, trigger: str) -> bool:
        self._set_state("preparing")
        attempts = self.max_retries + 1
        last_detail = ""
        for attempt in range(1, attempts + 1):
            self.retry_count = attempt - 1
            ok, detail = self._run_chain_once(self.run_chain)
            self.last_prepare = self._stamp("pass" if ok else "fail", None if ok else detail)
            self.last_prepare["chain"] = self.run_chain
            self.last_prepare["trigger"] = trigger
            self.last_prepare["attempt"] = attempt
            if ok:
                self.retry_count = 0
                self._set_state("pass")
                return True
            last_detail = detail
            self._set_state("fail")
            print(
                f"prepare_next_run failed ({attempt}/{attempts}) trigger={trigger}: {detail}",
                flush=True,
            )
        reason = (
            f"prepare_next_run failed after {attempts} attempts "
            f"(trigger={trigger}, last_error={last_detail})"
        )
        self._set_state("degraded", reason=reason)
        return False

    def can_admit_request(self) -> bool:
        if self.state == "pass":
            return True
        if self.state == "degraded" and not self.degraded_holds_queue:
            return True
        return False


def main() -> None:
    ensure_dirs()
    validate_all_chains()

    board = BoardControl.BoardControlLocal()
    console_manager = ConsoleManager(AUTOPILOT_DIR)

    event_queue: queue.Queue = queue.Queue()
    exit_flag = threading.Event()
    cancel_flag = threading.Event()

    session_name = detect_tmux_session()
    ui_state = TmuxUIState(AUTOPILOT_DIR)
    window_manager = TmuxWindowManager(session_name) if session_name else None
    ui = TmuxUICompat(ui_state, windows=window_manager)

    source_manager = SourceManager(RESULTS_DIR, ui=ui)
    platform_overrides = {}
    task_registry_lock = threading.Lock()
    task_registry = {
        "lock": task_registry_lock,
        "cond": threading.Condition(task_registry_lock),
        "tasks": {},
        "signals": {},
    }
    prepare_lifecycle = PrepareLifecycle(
        source_manager=source_manager,
        board=board,
        event_queue=event_queue,
        ui=ui,
        ui_state=ui_state,
        exit_flag=exit_flag,
        console_manager=console_manager,
        platform_overrides=platform_overrides,
        task_registry=task_registry,
    )

    def _on_abort() -> None:
        event_queue.put(Event("abort"))

    def _on_tx(source: str, data: bytes) -> None:
        binding = source_manager.get(source)
        if not binding:
            return
        if data:
            binding.write_bytes(data)

    control = TmuxControlServer(
        socket_path=ui_state.control_socket_path,
        on_abort=_on_abort,
        on_tx=_on_tx,
    )
    control.start()

    print(f"Watching: {PENDING_DIR}", flush=True)
    print(f"Results:  {RESULTS_DIR}", flush=True)

    # Startup chain (optional)
    try:
        run_bootstrap_chain(
            chain_name="startup",
            source_manager=source_manager,
            board=board,
            event_queue=event_queue,
            cancel_flag=cancel_flag,
            ui=ui,
            ui_state=ui_state,
            exit_flag=exit_flag,
            console_manager=console_manager,
            platform_overrides=platform_overrides,
            task_registry=task_registry,
        )
    except ValueError as exc:
        if "chain not found: startup" not in str(exc):
            print(f"Startup chain failed: {exc}", flush=True)
    except Exception as exc:
        print(f"Startup chain failed: {exc}", flush=True)

    if AUTOPILOT_PLATFORM:
        platform_chain_name = f"platform-init-{AUTOPILOT_PLATFORM}"
        try:
            run_bootstrap_chain(
                chain_name=platform_chain_name,
                source_manager=source_manager,
                board=board,
                event_queue=event_queue,
                cancel_flag=cancel_flag,
                ui=ui,
                ui_state=ui_state,
                exit_flag=exit_flag,
                console_manager=console_manager,
                platform_overrides=platform_overrides,
                task_registry=task_registry,
            )
            print(
                f"Platform init complete: {platform_chain_name} overrides={json.dumps(platform_overrides)}",
                flush=True,
            )
            prepare_lifecycle.apply_overrides()
        except Exception as exc:
            print(f"Platform init failed ({platform_chain_name}): {exc}", flush=True)
            control.stop()
            ui.stop()
            cleanup()
            sys.exit(1)

    prepare_lifecycle.startup_probe()

    # Main loop
    while not exit_flag.is_set():
        poll_idle_events(event_queue, exit_flag)
        if exit_flag.is_set():
            break

        requests = sorted(PENDING_DIR.glob("*.request"))
        if not requests:
            time.sleep(1)
            continue
        if not prepare_lifecycle.can_admit_request():
            if prepare_lifecycle.state == "degraded":
                print(
                    f"Queue admission blocked: {prepare_lifecycle.degraded_reason}",
                    flush=True,
                )
            time.sleep(1)
            continue

        request_file = requests[0]
        timestamp = request_file.stem
        processing_file = PROCESSING_DIR / request_file.name

        try:
            request_file.rename(processing_file)
        except Exception as exc:
            print(f"ERROR: Failed to move request to processing: {exc}", flush=True)
            time.sleep(1)
            continue

        result_dir = RESULTS_DIR / timestamp
        result_dir.mkdir(parents=True, exist_ok=True)
        source_manager.set_result_dir(result_dir)
        runtime_dir = RUNTIME_DIR / timestamp
        runtime_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n=== New request: {timestamp} ===", flush=True)
        print(f"Results: {result_dir}/", flush=True)

        try:
            request_data = json.loads(processing_file.read_text())
        except Exception as exc:
            request_data = {}
            print(f"WARNING: request JSON parse failed: {exc}", flush=True)

        (result_dir / "request.json").write_text(json.dumps(request_data, indent=2))

        profile_name = request_data.get("profile")
        if not profile_name:
            err = "request missing profile"
            (result_dir / "error.txt").write_text(err)
            processing_file.rename(FAILED_DIR / request_file.name)
            print(f"ERROR: {err}", flush=True)
            continue

        try:
            chain = load_chain(profile_name)
        except Exception as exc:
            (result_dir / "error.txt").write_text(str(exc))
            processing_file.rename(FAILED_DIR / request_file.name)
            print(f"ERROR: {exc}", flush=True)
            continue

        cancel_flag = threading.Event()
        cancel_path = runtime_dir / "cancel"
        cancel_watcher = threading.Thread(
            target=watch_cancel_file,
            args=(cancel_flag, cancel_path, exit_flag),
            daemon=True,
        )
        cancel_watcher.start()

        ctx = {
            "board": board,
            "sources": source_manager,
            "ui": ui,
            "event_queue": event_queue,
            "cancel_flag": cancel_flag,
            "result_dir": result_dir,
            "runtime_dir": runtime_dir,
            "request_id": timestamp,
            "request": request_data,
            "profile": profile_name,
            "chain_name": profile_name,
            "request_start": time.time(),
            "target_ip": TARGET_IP,
            "kernel_image": KERNEL_IMAGE,
            "kernel_release": KERNEL_RELEASE_FILE.read_text().strip() if KERNEL_RELEASE_FILE.exists() else "unknown",
            "console_manager": console_manager,
            "abort_recovery_chain": "recovery_boot",
            "default_ttys": {"tty0": DEFAULT_TTY0, "tty1": DEFAULT_TTY1},
            "exit_flag": exit_flag,
            "load_chain": load_chain,
            "platform_overrides": platform_overrides,
            "task_registry": task_registry,
            "code_root": str(SCRIPT_DIR),
            "chains_dir": str(CHAINS_DIR),
            "profiles_dir": str(SCRIPT_DIR / "profiles"),
        }
        recorder = ChainRecorder(result_dir)

        status = "failed"
        try:
            ui_state.set_request(timestamp, profile_name, profile_name)
            status = run_chain(chain, ctx, recorder)
        except Exception as exc:
            (result_dir / "error.txt").write_text(str(exc))
            status = "failed"
        finally:
            ui_state.clear_request()
            write_post_run_dtb_artifacts(result_dir, status)

        ftrace_post = run_post_run_ftrace_pipeline(result_dir)
        if ftrace_post.get("required"):
            if not ftrace_post.get("ok"):
                reason = ftrace_post.get("error", "FTRACE_POSTPROCESS_FAILED")
                _append_sel4_failure_marker(result_dir, f"FTRACE_POSTPROCESS_FAILED ({reason})")
                status = "failed"
            else:
                dump_reason = ftrace_post.get("summary", {}).get("dump_reason")
                if dump_reason == "storage_full":
                    _append_sel4_failure_marker(
                        result_dir,
                        "FTRACE_OVERFLOW_STORAGE_FULL (kernel auto-dump reason=storage_full)"
                    )
                    status = "failed"

        if status == "pass":
            processing_file.rename(COMPLETED_DIR / request_file.name)
        else:
            processing_file.rename(FAILED_DIR / request_file.name)

        print(f"=== {timestamp} completed: {status} ===", flush=True)
        prepare_lifecycle.run_prepare_cycle(trigger=f"request_complete:{timestamp}")

    control.stop()
    ui.stop()
    cleanup()


if __name__ == "__main__":
    main()
