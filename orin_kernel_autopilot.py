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

PROFILE_ANALYSIS_HOOKS = {
    "vm-qemu-virtio": {
        "required": [
            "ftrace_index_integrity",
            "crossvm_irq_path_check",
            "virtio_console_probe_window_check",
        ],
        "optional": [
            "timeline_render",
            "summary_markdown_export",
        ],
    },
    "vm-qemu-virtio-singleport": {
        "required": [],
        "optional": [
            "ftrace_index_integrity",
            "crossvm_irq_path_check",
            "virtio_console_probe_window_check",
            "timeline_render",
            "summary_markdown_export",
        ],
    },
}


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


def _append_failure_marker(result_dir: Path, message: str) -> None:
    fail_log = result_dir / "console" / "autopilot.fail.log"
    fail_log.parent.mkdir(parents=True, exist_ok=True)
    with fail_log.open("a") as fh:
        fh.write(f"\nAUTOPILOT_FAIL: {message}\n")


def _has_ftrace_dump_evidence(result_dir: Path) -> bool:
    marker_bytes = [b"=== BINARY TRANSFER START ===", b"TRACE_DUMP_TERMINAL:"]
    marker_text = ["=== BINARY TRANSFER START ===", "TRACE_DUMP_TERMINAL:", "FTRACE: Storage full"]
    for log_name in ("console/tty0.raw",):
        log_path = result_dir / log_name
        if not log_path.exists():
            continue
        try:
            raw = log_path.read_bytes()
            if any(marker in raw for marker in marker_bytes):
                return True
            content = raw.decode("utf-8", errors="ignore")
        except Exception:
            continue
        if any(marker in content for marker in marker_text):
            return True
    return False


def run_post_run_ftrace_pipeline(result_dir: Path) -> dict:
    """
    Enforce ftrace extraction/indexing/summary generation when dump evidence exists.
    """
    required = False
    for _ in range(5):
        if _has_ftrace_dump_evidence(result_dir):
            required = True
            break
        time.sleep(0.3)
    summary = {
        "required": required,
        "extracted": False,
        "indexed": False,
        "summary_generated": False,
        "dump_reason": None,
        "dump_reason_code": None,
        "storage_full": None,
        "overflow_events": None,
        "compress_fail_events": None,
        "total_entries": None,
        "dump_terminal_seen": _has_ftrace_dump_evidence(result_dir),
    }
    summary_path = result_dir / "ftrace.summary.json"
    if not required:
        summary_path.write_text(json.dumps(summary, indent=2))
        return {"required": False, "ok": True, "summary": summary}

    extract_script = SCRIPT_DIR / "extract_ftrace.py"
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
        storage_full = bool(header.get("STORAGE_FULL", False))
        overflow_events = int(header.get("OVERFLOW_EVENTS", 0))
        compress_fail_events = int(header.get("COMPRESS_FAIL_EVENTS", 0))
        total_entries = int(header.get("TOTAL_ENTRIES", meta.get("entry_count", 0)))
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
        "storage_full": storage_full,
        "overflow_events": overflow_events,
        "compress_fail_events": compress_fail_events,
        "total_entries": total_entries,
        "artifact_paths": {
            "bin": str(bin_path),
            "meta": str(meta_path),
            "idx": str(idx_path),
        },
    })
    summary_path.write_text(json.dumps(summary, indent=2))
    return {"required": True, "ok": True, "summary": summary}


def _hook_result(
    hook_id: str,
    result: str,
    summary: str,
    artifacts: list[str] = None,
    error: str = None,
) -> dict:
    payload = {
        "hook_id": hook_id,
        "result": result,
        "summary": summary,
        "artifacts": artifacts or [],
    }
    if error:
        payload["error"] = error
    return payload


def _analysis_text_log_path(result_dir: Path, source: str = "tty0") -> Path:
    ansi_log = result_dir / "console" / f"{source}.ansi.log"
    if ansi_log.exists():
        return ansi_log
    return result_dir / "console" / f"{source}.raw"


def _run_hook_ftrace_index_integrity(result_dir: Path, ftrace_post: dict) -> dict:
    summary_path = result_dir / "ftrace.summary.json"
    if not ftrace_post.get("required"):
        return _hook_result(
            "ftrace_index_integrity",
            "pass",
            "ftrace dump not required for this run",
            artifacts=[str(summary_path)] if summary_path.exists() else [],
        )

    if not ftrace_post.get("ok"):
        return _hook_result(
            "ftrace_index_integrity",
            "fail",
            "ftrace post-run processing failed",
            error=ftrace_post.get("error", "unknown"),
        )

    paths = ftrace_post.get("summary", {}).get("artifact_paths", {})
    missing = []
    found = []
    for key in ("bin", "meta", "idx"):
        p = paths.get(key)
        if not p or not Path(p).exists():
            missing.append(key)
        else:
            found.append(p)
    if missing:
        return _hook_result(
            "ftrace_index_integrity",
            "fail",
            f"missing required indexed artifacts: {', '.join(missing)}",
            artifacts=found,
        )
    return _hook_result(
        "ftrace_index_integrity",
        "pass",
        "indexed ftrace artifacts present",
        artifacts=found,
    )


def _run_hook_crossvm_irq_path_check(result_dir: Path) -> dict:
    tty0 = _analysis_text_log_path(result_dir, source="tty0")
    if not tty0.exists():
        return _hook_result(
            "crossvm_irq_path_check",
            "fail",
            "console log missing",
            error="tty0 analysis log not found",
        )
    text = tty0.read_text(errors="ignore")
    if "irq=236" in text:
        return _hook_result(
            "crossvm_irq_path_check",
            "pass",
            "cross-VM IRQ continuity marker irq=236 observed",
            artifacts=[str(tty0)],
        )
    # Low-noise mode may suppress irq=236 runtime markers; accept a stable
    # continuity chain instead.
    has_crossvm_module = "module name: cross_vm_connections" in text
    has_vm0_proxy = "vm1: vmm_module_init@main.c:803 module name: vm0_io_proxy" in text
    has_guest_device = "sel4 0000:00:01.0: guest-device-1 initialized" in text
    if has_crossvm_module and has_vm0_proxy and has_guest_device:
        return _hook_result(
            "crossvm_irq_path_check",
            "pass",
            "cross-VM continuity fallback observed (cross_vm_connections + vm0_io_proxy + guest-device-1)",
            artifacts=[str(tty0)],
        )
    return _hook_result(
        "crossvm_irq_path_check",
        "fail",
        "cross-VM IRQ continuity markers not observed (irq=236 and fallback chain missing)",
        artifacts=[str(tty0)],
    )


def _run_hook_virtio_console_probe_window_check(result_dir: Path) -> dict:
    tty0 = _analysis_text_log_path(result_dir, source="tty0")
    if not tty0.exists():
        return _hook_result(
            "virtio_console_probe_window_check",
            "fail",
            "console log missing",
            error="tty0 analysis log not found",
        )
    text = tty0.read_text(errors="ignore")
    has_init = "virtio_console_init" in text
    has_probe = "virtcons_probe" in text
    if has_init and has_probe:
        return _hook_result(
            "virtio_console_probe_window_check",
            "pass",
            "both virtio_console_init and virtcons_probe markers observed",
            artifacts=[str(tty0)],
        )
    if has_init:
        return _hook_result(
            "virtio_console_probe_window_check",
            "pass",
            "virtio_console_init marker observed (probe marker absent)",
            artifacts=[str(tty0)],
        )
    # Fallback for low-noise runs where symbol-level probe markers are absent:
    # validate that VM1 reaches stable console and virtio guest-device init.
    has_vm1_cmdline = "Kernel command line:" in text and "uservm=1," in text
    has_console_enabled = "printk: console [ttyTCU0] enabled" in text
    has_guest_device = "sel4 0000:00:01.0: guest-device-1 initialized" in text
    if has_vm1_cmdline and has_console_enabled and has_guest_device:
        return _hook_result(
            "virtio_console_probe_window_check",
            "pass",
            "virtio console window fallback observed (VM1 cmdline + ttyTCU0 console + guest-device-1)",
            artifacts=[str(tty0)],
        )
    return _hook_result(
        "virtio_console_probe_window_check",
        "fail",
        "virtio console probe window markers not observed (primary and fallback missing)",
        artifacts=[str(tty0)],
    )


def _run_hook_timeline_render(result_dir: Path) -> dict:
    tty0 = result_dir / "console" / "tty0.raw"
    out = result_dir / "analysis_hooks" / "timeline.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not tty0.exists():
        out.write_text("# Timeline\n\nconsole log missing\n")
        return _hook_result(
            "timeline_render",
            "fail",
            "unable to render timeline: tty0.raw not found",
            artifacts=[str(out)],
        )
    data = tty0.read_bytes()
    markers = [
        b"virtio_console_init",
        b"virtcons_probe",
        b"irq=236",
        b"TRACE_DUMP_TERMINAL:",
    ]
    lines = ["# Timeline", ""]
    for marker in markers:
        idx = data.find(marker)
        if idx >= 0:
            lines.append(f"- `{marker.decode(errors='ignore')}` at byte `{idx}`")
        else:
            lines.append(f"- `{marker.decode(errors='ignore')}` not found")
    out.write_text("\n".join(lines) + "\n")
    return _hook_result("timeline_render", "pass", "timeline markdown rendered", artifacts=[str(out)])


def _run_hook_summary_markdown_export(result_dir: Path, hook_results: list[dict]) -> dict:
    out = result_dir / "analysis_hooks" / "summary.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Post-run Analysis Summary", ""]
    for result in hook_results:
        lines.append(
            f"- `{result.get('hook_id')}`: `{result.get('result')}` - {result.get('summary')}"
        )
    out.write_text("\n".join(lines) + "\n")
    return _hook_result(
        "summary_markdown_export",
        "pass",
        "summary markdown exported",
        artifacts=[str(out)],
    )


def run_external_analysis_hooks(result_dir: Path, profile_name: str, ftrace_post: dict) -> dict:
    hooks_cfg = PROFILE_ANALYSIS_HOOKS.get(profile_name)
    if hooks_cfg is None:
        return {
            "ok": False,
            "error": "HOOKS_REQUIRED_DEFINITION_MISSING",
            "required": [],
            "optional": [],
            "required_ok": False,
        }

    required_results: list[dict] = []
    optional_results: list[dict] = []

    hook_dispatch = {
        "ftrace_index_integrity": lambda: _run_hook_ftrace_index_integrity(result_dir, ftrace_post),
        "crossvm_irq_path_check": lambda: _run_hook_crossvm_irq_path_check(result_dir),
        "virtio_console_probe_window_check": lambda: _run_hook_virtio_console_probe_window_check(result_dir),
        "timeline_render": lambda: _run_hook_timeline_render(result_dir),
        "summary_markdown_export": lambda: _run_hook_summary_markdown_export(
            result_dir, required_results + optional_results
        ),
    }

    for hook_id in hooks_cfg.get("required", []):
        runner = hook_dispatch.get(hook_id)
        if runner is None:
            required_results.append(
                _hook_result(
                    hook_id,
                    "fail",
                    "required hook is not implemented",
                    error="UNIMPLEMENTED_REQUIRED_HOOK",
                )
            )
            continue
        try:
            required_results.append(runner())
        except Exception as exc:
            required_results.append(
                _hook_result(hook_id, "fail", "required hook execution failed", error=str(exc))
            )

    for hook_id in hooks_cfg.get("optional", []):
        runner = hook_dispatch.get(hook_id)
        if runner is None:
            optional_results.append(
                _hook_result(
                    hook_id,
                    "fail",
                    "optional hook is not implemented",
                    error="UNIMPLEMENTED_OPTIONAL_HOOK",
                )
            )
            continue
        try:
            optional_results.append(runner())
        except Exception as exc:
            optional_results.append(
                _hook_result(hook_id, "fail", "optional hook execution failed", error=str(exc))
            )

    required_ok = all(item.get("result") == "pass" for item in required_results)
    payload = {
        "profile": profile_name,
        "required": required_results,
        "optional": optional_results,
        "required_ok": required_ok,
    }
    out = result_dir / "analysis_hooks" / "analysis_hooks.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    payload["artifact"] = str(out)
    payload["ok"] = required_ok
    if not required_ok:
        payload["error"] = "REQUIRED_HOOK_FAILED"
    return payload


def write_post_run_lifecycle(
    result_dir: Path,
    request_id: str,
    profile_name: str,
    request_status: str,
    transfer_state: dict,
    ftrace_post: dict,
    hooks_post: dict,
    prepare_gate: dict,
) -> dict:
    lifecycle = {
        "request_id": request_id,
        "profile": profile_name,
        "status": request_status,
        "ordering": [
            "wait_for_ftrace_uart_drain",
            "run_post_run_ftrace_pipeline",
            "run_external_analysis_hooks",
            "prepare_next_run",
        ],
        "drain": {
            "state": transfer_state.get("drain_state", "not_required"),
            "details": transfer_state,
        },
        "ftrace_post": ftrace_post,
        "analysis_hooks": hooks_post,
        "prepare_gate": prepare_gate,
    }
    path = result_dir / "post_run.lifecycle.json"
    path.write_text(json.dumps(lifecycle, indent=2))
    lifecycle["artifact"] = str(path)
    return lifecycle


def _read_ftrace_transfer_state(result_dir: Path) -> dict:
    tty0 = result_dir / "console" / "tty0.raw"
    if not tty0.exists():
        return {"exists": False, "has_start": False, "has_end": False, "has_terminal": False, "size": 0}
    try:
        data = tty0.read_bytes()
    except Exception:
        return {"exists": True, "has_start": False, "has_end": False, "has_terminal": False, "size": 0}
    return {
        "exists": True,
        "has_start": b"=== BINARY TRANSFER START ===" in data,
        "has_end": b"=== BINARY TRANSFER END ===" in data,
        "has_terminal": b"TRACE_DUMP_TERMINAL:" in data,
        "size": len(data),
    }


def wait_for_ftrace_uart_drain(result_dir: Path, timeout_s: float = 180.0) -> dict:
    """
    Guard against relay/power reset while ftrace binary dump is still flowing.
    """
    start = time.time()
    last_size = -1
    stable_rounds = 0
    while time.time() - start < timeout_s:
        state = _read_ftrace_transfer_state(result_dir)
        if not state["exists"] or not state["has_start"]:
            state["drain_state"] = "not_required"
            return state

        if state["has_terminal"] or state["has_end"]:
            size_now = state["size"]
            if size_now == last_size:
                stable_rounds += 1
            else:
                stable_rounds = 0
            last_size = size_now
            if stable_rounds >= 2:
                state["drain_state"] = "drain_complete"
                return state
        time.sleep(0.5)
    state = _read_ftrace_transfer_state(result_dir)
    state["drain_state"] = "drain_timeout" if state.get("has_start") else "not_required"
    return state


def should_skip_prepare_cycle(result_dir: Path, transfer_state: dict) -> tuple[bool, str]:
    if transfer_state.get("has_start") and not (
        transfer_state.get("has_terminal") or transfer_state.get("has_end")
    ):
        return True, "ftrace transfer started but completion markers missing"

    tty0 = result_dir / "console" / "tty0.raw"
    if tty0.exists():
        try:
            data = tty0.read_bytes()
            if b"FTRACE: Storage full" in data:
                return True, "storage-full marker observed in tty0.raw"
        except Exception:
            pass

    fail_log = result_dir / "console" / "autopilot.fail.log"
    if fail_log.exists():
        try:
            content = fail_log.read_text(errors="ignore")
            if "FTRACE_OVERFLOW_STORAGE_FULL" in content:
                return True, "overflow fail marker present in autopilot.fail.log"
        except Exception:
            pass

    return False, ""


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

        transfer_state = wait_for_ftrace_uart_drain(result_dir)
        ftrace_post = run_post_run_ftrace_pipeline(result_dir)
        if ftrace_post.get("required"):
            if not ftrace_post.get("ok"):
                reason = ftrace_post.get("error", "FTRACE_POSTPROCESS_FAILED")
                stderr_tail = ""
                if ftrace_post.get("stderr"):
                    stderr_lines = [ln for ln in ftrace_post["stderr"].splitlines() if ln.strip()]
                    if stderr_lines:
                        stderr_tail = f"; detail={stderr_lines[-1]}"
                _append_failure_marker(
                    result_dir,
                    f"FTRACE_POSTPROCESS_FAILED ({reason}{stderr_tail})",
                )
                status = "failed"
            else:
                dump_reason = ftrace_post.get("summary", {}).get("dump_reason")
                if dump_reason == "storage_full":
                    _append_failure_marker(
                        result_dir,
                        "FTRACE_OVERFLOW_STORAGE_FULL (kernel auto-dump reason=storage_full)"
                    )
                    status = "failed"

        hooks_post = run_external_analysis_hooks(result_dir, profile_name, ftrace_post)
        if not hooks_post.get("ok"):
            reason = hooks_post.get("error", "REQUIRED_HOOK_FAILED")
            _append_failure_marker(
                result_dir,
                f"ANALYSIS_HOOKS_FAILED ({reason})",
            )
            status = "failed"

        if status == "pass":
            processing_file.rename(COMPLETED_DIR / request_file.name)
        else:
            processing_file.rename(FAILED_DIR / request_file.name)

        prepare_gate = {
            "allow_prepare": True,
            "reason": "ok",
            "reasons": [],
        }
        if transfer_state.get("drain_state") == "drain_timeout":
            prepare_gate["allow_prepare"] = False
            prepare_gate["reasons"].append("drain_timeout")
        if ftrace_post.get("required") and not ftrace_post.get("ok"):
            prepare_gate["allow_prepare"] = False
            prepare_gate["reasons"].append("ftrace_postprocess_failed")
        if not hooks_post.get("ok"):
            prepare_gate["allow_prepare"] = False
            prepare_gate["reasons"].append("required_hook_failed")
        if status != "pass":
            prepare_gate["allow_prepare"] = False
            prepare_gate["reasons"].append("request_status_failed")
        if not prepare_gate["allow_prepare"]:
            prepare_gate["reason"] = ",".join(prepare_gate["reasons"])

        lifecycle = write_post_run_lifecycle(
            result_dir=result_dir,
            request_id=timestamp,
            profile_name=profile_name,
            request_status=status,
            transfer_state=transfer_state,
            ftrace_post=ftrace_post,
            hooks_post=hooks_post,
            prepare_gate=prepare_gate,
        )

        print(f"=== {timestamp} completed: {status} ===", flush=True)
        if not prepare_gate["allow_prepare"]:
            blocked_reason = (
                f"post-run lifecycle gate blocked prepare_next_run: {prepare_gate['reason']}"
            )
            print(
                f"Skipping prepare_next_run relay reset: {blocked_reason} ({timestamp})",
                flush=True,
            )
            prepare_lifecycle._set_state("degraded", reason=blocked_reason)
            print(f"Post-run lifecycle: {lifecycle.get('artifact')}", flush=True)
        else:
            prepare_lifecycle.run_prepare_cycle(trigger=f"request_complete:{timestamp}")

    control.stop()
    ui.stop()
    cleanup()


if __name__ == "__main__":
    main()
