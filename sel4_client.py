#!/usr/bin/env python3
"""
seL4 Autopilot Client Library

Helper library for AI agents (or humans) to submit seL4 EFI binary tests
to the autopilot service and retrieve results.

Usage:
    from sel4_client import submit_sel4_efi_test, wait_for_result, get_logs

    timestamp = submit_sel4_efi_test(
        binary_path='/path/to/sel4test-driver-image-arm-orinagx',
        binary_name='sel4test.efi',
        description='Testing MMU enable with TCU debug',
        profile='sel4test'
    )

    result = wait_for_result(timestamp)
    if result['status'] == 'completed':
        logs = get_logs(timestamp)
        print(logs)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import get_autopilot_dir, get_paths
from startup_queue import read_startup_cleanup
from chain_runtime import ChainValidationError, validate_chain


class QueueCapacityExceededError(RuntimeError):
    def __init__(self, pending: list, processing: list, max_inflight: int):
        super().__init__("queue_capacity_exceeded")
        self.pending = pending
        self.processing = processing
        self.max_inflight = max_inflight
        self.inflight = len(pending) + len(processing)


class QueueNotEmptyError(QueueCapacityExceededError):
    # Backward-compatible alias for older callers.
    def __init__(self, pending: list, processing: list):
        super().__init__(pending, processing, max_inflight=1)


def _validate_chain_admission(profile: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", profile or ""):
        raise ValueError(f"invalid profile name: {profile!r}")
    code_root = Path(__file__).resolve().parent
    chain_path = code_root / "chains" / f"{profile}.json"
    if not chain_path.exists():
        raise FileNotFoundError(f"profile chain not found: {chain_path}")
    try:
        chain = json.loads(chain_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"profile chain JSON parse failed: {chain_path}: {exc}") from exc
    try:
        validate_chain(chain)
    except ChainValidationError as exc:
        raise ValueError(f"profile chain validation failed ({chain_path.name}): {exc}") from exc

    lint_script = code_root / "scripts" / "lint_prepare_lifecycle.py"
    proc = subprocess.run(
        [sys.executable, str(lint_script)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stdout + "\n" + proc.stderr).strip()
        raise ValueError(f"prepare lifecycle lint failed:\n{detail}")


def _sanitize_test_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        return "test"
    cleaned = []
    prev_underscore = False
    for ch in name:
        if ch.isalnum() or ch in "._-":
            cleaned.append(ch)
            prev_underscore = False
            continue
        if not prev_underscore:
            cleaned.append("_")
            prev_underscore = True
    out = "".join(cleaned).strip("._-")
    return out or "test"


def _derive_target_binary_name(binary_name: str, timestamp: str) -> tuple[str, str]:
    stem = Path(binary_name).stem if binary_name else ""
    test_name = _sanitize_test_name(stem)
    return test_name, f"{test_name}-{timestamp}.EFI"


# Legacy module-level paths for backward compatibility
# These mirror get_paths() so queue path mapping has one source of truth.
AUTOPILOT_DIR = get_autopilot_dir()
_DEFAULT_PATHS = get_paths(str(AUTOPILOT_DIR))
PENDING_DIR = _DEFAULT_PATHS['pending']
PROCESSING_DIR = _DEFAULT_PATHS['processing']
COMPLETED_DIR = _DEFAULT_PATHS['completed']
FAILED_DIR = _DEFAULT_PATHS['failed']
RESULTS_DIR = _DEFAULT_PATHS['results']
BINARIES_DIR = _DEFAULT_PATHS['binaries']
RUNTIME_DIR = _DEFAULT_PATHS['runtime']


def ensure_queue_empty(autopilot_dir: str = None) -> None:
    ensure_queue_capacity(max_inflight=1, autopilot_dir=autopilot_dir)


def ensure_queue_capacity(max_inflight: int = 2, autopilot_dir: str = None) -> None:
    pending = list_pending(autopilot_dir=autopilot_dir)
    processing = list_processing(autopilot_dir=autopilot_dir)
    inflight = len(pending) + len(processing)
    if inflight >= max_inflight:
        raise QueueCapacityExceededError(pending, processing, max_inflight=max_inflight)


def submit_sel4_efi_test(
    binary_path: str,
    binary_name: str = 'sel4test.efi',
    description: str = '',
    copy_to_staging: bool = True,
    build_config: dict = None,
    autopilot_dir: str = None,
    profile: str = 'sel4test'
) -> str:
    """
    Submit a seL4 EFI binary for testing.

    Args:
        binary_path: Path to the EFI binary to test
        binary_name: Name to use for the binary on target (default: sel4test.efi)
        description: Optional description of the test
        copy_to_staging: If True, copy binary to staging area (default: True)
        build_config: Optional build configuration metadata
        autopilot_dir: Optional override for autopilot working directory
        profile: Profile name that defines the chain to run

    Returns:
        timestamp: Request ID that can be used to check status/get results
    """
    ensure_queue_capacity(max_inflight=2, autopilot_dir=autopilot_dir)
    _validate_chain_admission(profile)
    paths = get_paths(autopilot_dir)
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')

    # Ensure directories exist
    paths['pending'].mkdir(parents=True, exist_ok=True)
    paths['binaries'].mkdir(parents=True, exist_ok=True)

    # Resolve and validate binary path
    binary_src = Path(binary_path).resolve()
    if not binary_src.exists():
        raise FileNotFoundError(f"Binary not found: {binary_src}")

    # Determine binary path for request
    if copy_to_staging:
        staged_binary = paths['binaries'] / binary_name
        shutil.copy(binary_src, staged_binary)
        request_binary_path = str(staged_binary)
    else:
        request_binary_path = str(binary_src)

    # Create request file
    test_name, target_binary_name = _derive_target_binary_name(binary_name, timestamp)
    request = {
        'profile': profile,
        'binary_path': request_binary_path,
        'binary_name': binary_name,
        'test_name': test_name,
        'target_binary_name': target_binary_name,
        'description': description,
        'submitted_at': timestamp,
        'original_binary': str(binary_src),
        'build_config': build_config
    }

    request_file = paths['pending'] / f'{timestamp}.request'
    request_file.write_text(json.dumps(request, indent=2))

    return timestamp




def submit_boot_interactive(
    boot_target: str = "stock_linux",
    binary_path: str = "",
    binary_name: str = "sel4test.efi",
    interactive: dict = None,
    description: str = "",
    copy_to_staging: bool = True,
    build_config: dict = None,
    autopilot_dir: str = None,
    profile: str = "boot-interactive"
) -> str:
    """
    Submit a boot_interactive request that boots a target and opens console sessions.

    Args:
        boot_target: "stock_linux" or "efi" (any non-stock value uses EFI binary)
        binary_path: EFI binary path (required for non-stock targets)
        binary_name: EFI binary name on target
        interactive: Interactive configuration dict (required)
        description: Optional description
        copy_to_staging: If True, copy binary to staging area
        build_config: Optional build config metadata
        autopilot_dir: Optional override for autopilot working directory
        profile: Profile name that defines the chain to run
    """
    if interactive is None:
        raise ValueError("interactive config is required")

    ensure_queue_capacity(max_inflight=2, autopilot_dir=autopilot_dir)
    _validate_chain_admission(profile)
    paths = get_paths(autopilot_dir)
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')

    paths['pending'].mkdir(parents=True, exist_ok=True)
    paths['binaries'].mkdir(parents=True, exist_ok=True)

    request_binary_path = ""
    if boot_target != "stock_linux":
        if not binary_path:
            raise ValueError("binary_path required for non-stock boot targets")
        binary_src = Path(binary_path).resolve()
        if not binary_src.exists():
            raise FileNotFoundError(f"Binary not found: {binary_src}")
        if copy_to_staging:
            staged_binary = paths['binaries'] / binary_name
            shutil.copy(binary_src, staged_binary)
            request_binary_path = str(staged_binary)
        else:
            request_binary_path = str(binary_src)

    request = {
        "profile": profile,
        "boot_target": boot_target,
        "binary_path": request_binary_path,
        "binary_name": binary_name,
        "description": description,
        "submitted_at": timestamp,
        "interactive": interactive,
        "build_config": build_config
    }
    if boot_target != "stock_linux":
        test_name, target_binary_name = _derive_target_binary_name(binary_name, timestamp)
        request["test_name"] = test_name
        request["target_binary_name"] = target_binary_name

    request_file = paths['pending'] / f"{timestamp}.request"
    request_file.write_text(json.dumps(request, indent=2))
    return timestamp




def get_status(timestamp: str, autopilot_dir: str = None) -> dict:
    """
    Get the current status of a test request.

    Args:
        timestamp: Request ID from submit_sel4_efi_test()
        autopilot_dir: Optional override for autopilot working directory

    Returns:
        dict with 'status' key: 'pending', 'processing', 'completed', 'failed', or 'not_found'
    """
    paths = get_paths(autopilot_dir)
    request_name = f'{timestamp}.request'

    if (paths['completed'] / request_name).exists():
        return {
            'status': 'completed',
            'result_dir': paths['results'] / timestamp
        }
    elif (paths['failed'] / request_name).exists():
        return {
            'status': 'failed',
            'result_dir': paths['results'] / timestamp
        }
    elif (paths['processing'] / request_name).exists():
        return {'status': 'processing'}
    elif (paths['pending'] / request_name).exists():
        return {'status': 'pending'}
    else:
        return {'status': 'not_found'}


def wait_for_result(timestamp: str, timeout: int = 300, poll_interval: int = 1,
                    autopilot_dir: str = None) -> dict:
    """
    Wait for test to complete and return results.

    Args:
        timestamp: Request ID from submit_sel4_efi_test()
    timeout: Maximum time to wait in seconds (default: 300)
    poll_interval: How often to check status in seconds (default: 1)
        autopilot_dir: Optional override for autopilot working directory

    Returns:
        dict with 'status' key: 'completed', 'failed', or 'timeout'
              and 'result_dir' key if completed/failed
    """
    start = time.time()
    while time.time() - start < timeout:
        status = get_status(timestamp, autopilot_dir=autopilot_dir)
        if status['status'] in ('completed', 'failed'):
            return status
        time.sleep(poll_interval)

    return {'status': 'timeout'}


def get_logs(timestamp: str, autopilot_dir: str = None, include_contents: bool = False) -> dict:
    """
    List console logs for a request.

    Args:
        timestamp: Request ID from submit_sel4_efi_test()
        autopilot_dir: Optional override for autopilot working directory
        include_contents: If True, include file contents in response

    Returns:
        dict with console_dir and list of files (path, size, contents optional)
    """
    paths = get_paths(autopilot_dir)
    console_dir = paths['results'] / timestamp / 'console'
    files = []
    if console_dir.exists():
        for path in sorted(console_dir.glob("*")):
            if path.is_file():
                entry = {
                    "path": str(path),
                    "size": path.stat().st_size,
                }
                if include_contents:
                    entry["contents"] = path.read_text(errors="replace")
                files.append(entry)
    return {
        "console_dir": str(console_dir),
        "files": files,
    }


def get_request_info(timestamp: str, autopilot_dir: str = None) -> Optional[dict]:
    """
    Get the original request metadata.

    Args:
        timestamp: Request ID from submit_sel4_efi_test()
        autopilot_dir: Optional override for autopilot working directory

    Returns:
        Request dict, or None if not found
    """
    paths = get_paths(autopilot_dir)
    for directory in [paths['completed'], paths['failed'], paths['processing'], paths['pending']]:
        request_file = directory / f'{timestamp}.request'
        if request_file.exists():
            return json.loads(request_file.read_text())
    return None


def list_pending(autopilot_dir: str = None) -> list:
    """List all pending request timestamps.

    Args:
        autopilot_dir: Optional override for autopilot working directory
    """
    paths = get_paths(autopilot_dir)
    if not paths['pending'].exists():
        return []
    return sorted([f.stem for f in paths['pending'].glob('*.request')])


def list_processing(autopilot_dir: str = None) -> list:
    """List all processing request timestamps.

    Args:
        autopilot_dir: Optional override for autopilot working directory
    """
    paths = get_paths(autopilot_dir)
    if not paths['processing'].exists():
        return []
    return sorted([f.stem for f in paths['processing'].glob('*.request')])


def list_completed(autopilot_dir: str = None) -> list:
    """List all completed request timestamps.

    Args:
        autopilot_dir: Optional override for autopilot working directory
    """
    paths = get_paths(autopilot_dir)
    if not paths['completed'].exists():
        return []
    return sorted([f.stem for f in paths['completed'].glob('*.request')])


def list_failed(autopilot_dir: str = None) -> list:
    """List all failed request timestamps.

    Args:
        autopilot_dir: Optional override for autopilot working directory
    """
    paths = get_paths(autopilot_dir)
    if not paths['failed'].exists():
        return []
    return sorted([f.stem for f in paths['failed'].glob('*.request')])


def _read_request_file(request_path: Path) -> Optional[dict]:
    if not request_path.exists():
        return None
    try:
        return json.loads(request_path.read_text())
    except Exception:
        return None


def _read_chain_last_step(result_dir: Path) -> Optional[dict]:
    chain_path = result_dir / "chain.json"
    if not chain_path.exists():
        return None
    try:
        chain = json.loads(chain_path.read_text())
    except Exception:
        return None
    steps = chain.get("steps", [])
    if not steps:
        return None
    last = steps[-1]
    return {
        "step": last.get("step"),
        "status": last.get("status"),
        "error_code": last.get("error_code"),
        "error_message": last.get("error_message"),
        "finished_at": last.get("finished_at"),
    }


def _read_chain_summary(result_dir: Path) -> Optional[dict]:
    chain_path = result_dir / "chain.json"
    if not chain_path.exists():
        return None
    try:
        chain = json.loads(chain_path.read_text())
    except Exception:
        return None
    return {
        "overall_status": chain.get("overall_status"),
        "test_verdict": chain.get("test_verdict"),
        "workflow_state": chain.get("workflow_state"),
        "abort_reason": chain.get("abort_reason"),
    }


def get_autopilot_status(autopilot_dir: str = None) -> dict:
    paths = get_paths(autopilot_dir)
    pending = list_pending(autopilot_dir=autopilot_dir)
    processing = list_processing(autopilot_dir=autopilot_dir)
    completed = list_completed(autopilot_dir=autopilot_dir)
    failed = list_failed(autopilot_dir=autopilot_dir)

    current = []
    for request_id in processing:
        request_path = paths['processing'] / f"{request_id}.request"
        req = _read_request_file(request_path) or {}
        result_dir = paths['results'] / request_id
        current.append({
            "request_id": request_id,
            "profile": req.get("profile"),
            "description": req.get("description"),
            "submitted_at": req.get("submitted_at"),
            "binary_path": req.get("binary_path"),
            "binary_name": req.get("binary_name"),
            "result_dir": str(result_dir),
            "last_step": _read_chain_last_step(result_dir),
            "chain_summary": _read_chain_summary(result_dir),
        })

    prepare_state = None
    prepare_path = paths["runtime"] / "prepare_state.json"
    if prepare_path.exists():
        try:
            prepare_state = json.loads(prepare_path.read_text())
        except Exception:
            prepare_state = {"state": "unknown", "error": "failed_to_parse_prepare_state"}

    return {
        "pending": {"count": len(pending), "ids": pending},
        "processing": {"count": len(processing), "ids": processing},
        "completed": {"count": len(completed), "latest_ids": completed[-10:]},
        "failed": {"count": len(failed), "latest_ids": failed[-10:]},
        "current": current,
        "prepare": prepare_state,
        "startup_cleanup": read_startup_cleanup(str(paths["autopilot"])),
    }


def get_test_status(timestamp: str, autopilot_dir: str = None) -> dict:
    paths = get_paths(autopilot_dir)
    status = get_status(timestamp, autopilot_dir=autopilot_dir)
    result_dir = paths['results'] / timestamp
    error_path = result_dir / "error.txt"
    error_text = error_path.read_text() if error_path.exists() else None
    return {
        "request_id": timestamp,
        "status": status["status"],
        "result_dir": str(status["result_dir"]) if "result_dir" in status else None,
        "request": get_request_info(timestamp, autopilot_dir=autopilot_dir),
        "last_step": _read_chain_last_step(result_dir),
        "chain_summary": _read_chain_summary(result_dir),
        "error": error_text,
    }


def _write_canceled_result(result_dir: Path, request: dict) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "request.json").write_text(json.dumps(request, indent=2))
    (result_dir / "error.txt").write_text("Canceled by user\n")
    chain = {
        "overall_status": "failed",
        "test_verdict": "fail",
        "workflow_state": "failed",
        "abort_reason": "canceled",
        "steps": [],
        "parallel_groups": {},
    }
    (result_dir / "chain.json").write_text(json.dumps(chain, indent=2))


def cancel_test(timestamp: str, autopilot_dir: str = None) -> dict:
    paths = get_paths(autopilot_dir)
    request_name = f"{timestamp}.request"
    pending_path = paths['pending'] / request_name
    processing_path = paths['processing'] / request_name
    result_dir = paths['results'] / timestamp

    if pending_path.exists():
        request = _read_request_file(pending_path) or {"submitted_at": timestamp}
        _write_canceled_result(result_dir, request)
        pending_path.rename(paths['failed'] / request_name)
        return {"status": "canceled", "request_id": timestamp, "mode": "pending"}

    if processing_path.exists():
        runtime_dir = paths['runtime'] / timestamp
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "cancel").write_text("canceled\n")
        # Best-effort short wait so API can report applied cancellation when it is immediate.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not processing_path.exists():
                final = get_status(timestamp, autopilot_dir=autopilot_dir)
                if final.get("status") == "failed":
                    return {
                        "status": "canceled",
                        "request_id": timestamp,
                        "mode": "processing",
                        "applied": True,
                    }
                return {
                    "status": "cancel_requested",
                    "request_id": timestamp,
                    "mode": "processing",
                    "applied": False,
                    "observed_status": final.get("status"),
                }
            time.sleep(0.2)
        return {
            "status": "cancel_requested",
            "request_id": timestamp,
            "mode": "processing",
            "applied": False,
        }

    if (paths['completed'] / request_name).exists():
        return {"status": "completed", "request_id": timestamp}
    if (paths['failed'] / request_name).exists():
        return {"status": "failed", "request_id": timestamp}
    return {"status": "not_found", "request_id": timestamp}


def get_console_manifest(timestamp: str, autopilot_dir: str = None) -> Optional[dict]:
    """Get console session manifest for an interactive request."""
    paths = get_paths(autopilot_dir)
    manifest_path = paths['results'] / timestamp / 'console' / 'sessions.json'
    if not manifest_path.exists():
        return None
    return json.loads(manifest_path.read_text())


def open_console_session(timestamp: str, session_name: str, autopilot_dir: str = None) -> dict:
    """Resolve a session name to a session_id and return initial offset."""
    manifest = get_console_manifest(timestamp, autopilot_dir=autopilot_dir)
    if not manifest:
        raise FileNotFoundError("Console sessions manifest not found")

    for sess in manifest.get('sessions', []):
        if sess.get('name') == session_name:
            log_path = Path(sess['log_path'])
            offset = log_path.stat().st_size if log_path.exists() else 0
            return {
                'session_id': sess['session_id'],
                'offset': offset,
                'log_path': sess['log_path'],
                'events_path': sess.get('events_path'),
                'pty_path': sess.get('pty_path'),
                'interactive': sess.get('interactive'),
                'kind': sess.get('kind'),
            }
    raise ValueError(f"Session '{session_name}' not found")


def read_console_output(session_id: str, offset: int, max_bytes: int = 4096,
                        autopilot_dir: str = None) -> dict:
    """Read console output from a session log using byte offsets."""
    base = get_autopilot_dir(autopilot_dir)
    session_log_path = None
    meta_path = base / "runtime" / "console" / session_id / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            session_log_path = Path(meta.get("log_path", ""))
        except Exception:
            session_log_path = None
    else:
        manifest_paths = list((base / "results").glob("*/console/sessions.json"))
        for mp in manifest_paths:
            try:
                data = json.loads(mp.read_text())
                for sess in data.get("sessions", []):
                    if sess.get("session_id") == session_id:
                        session_log_path = Path(sess["log_path"])
                        break
            except Exception:
                continue
            if session_log_path:
                break

    if not session_log_path or not session_log_path.exists():
        return {"output": "", "new_offset": offset}

    with open(session_log_path, "rb") as f:
        f.seek(offset)
        data = f.read(max_bytes)
    new_offset = offset + len(data)
    return {"output": data.decode("utf-8", errors="replace"), "new_offset": new_offset}


def send_console_command(session_id: str, command: str, append_newline: bool = True,
                         wait_for_prompt: bool = True, prompt_override: str = None,
                         timeout_s: int = 10, autopilot_dir: str = None) -> dict:
    """Send a command to a console session and wait for the response."""
    base = get_autopilot_dir(autopilot_dir)
    runtime_dir = base / "runtime" / "console" / session_id
    cmd_dir = runtime_dir / "cmd"
    resp_dir = runtime_dir / "resp"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    resp_dir.mkdir(parents=True, exist_ok=True)

    cmd_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    cmd_data = {
        "cmd_id": cmd_id,
        "command": command,
        "append_newline": append_newline,
        "wait_for_prompt": wait_for_prompt,
        "prompt_override": prompt_override,
        "timeout_s": timeout_s
    }
    cmd_path = cmd_dir / f"{cmd_id}.json"
    cmd_path.write_text(json.dumps(cmd_data, indent=2))

    deadline = time.time() + timeout_s + 5
    resp_path = resp_dir / f"{cmd_id}.json"
    while time.time() < deadline:
        if resp_path.exists():
            return json.loads(resp_path.read_text())
        time.sleep(0.1)

    return {"error": "timeout waiting for response", "cmd_id": cmd_id}


def close_console_session(session_id: str, autopilot_dir: str = None) -> None:
    """Request the console session to close."""
    base = get_autopilot_dir(autopilot_dir)
    runtime_dir = base / "runtime" / "console" / session_id
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "close").write_text("close\n")


# Command-line interface
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='seL4 Autopilot Client')
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # submit command
    submit_parser = subparsers.add_parser('submit', help='Submit a test')
    submit_parser.add_argument('binary_path', help='Path to EFI binary')
    submit_parser.add_argument('--name', default='sel4test.efi', help='Binary name on target')
    submit_parser.add_argument('--desc', default='', help='Description')
    submit_parser.add_argument('--profile', default='sel4test', help='Profile name (default: sel4test)')
    submit_parser.add_argument('--wait', action='store_true', help='Wait for result')
    hyp_group = submit_parser.add_mutually_exclusive_group(required=False)
    hyp_group.add_argument('--arm-hyp', dest='arm_hyp', action='store_true',
                           help='Built with ARM_HYPERVISOR_SUPPORT=ON')
    hyp_group.add_argument('--no-arm-hyp', dest='arm_hyp', action='store_false',
                           help='Built with ARM_HYPERVISOR_SUPPORT=OFF')
    submit_parser.add_argument('--platform', default='orinagx', help='Platform name (default: orinagx)')

    # status command
    status_parser = subparsers.add_parser('status', help='Check test status')
    status_parser.add_argument('timestamp', help='Request timestamp')

    # logs command
    log_parser = subparsers.add_parser('logs', help='List console logs')
    log_parser.add_argument('timestamp', help='Request timestamp')
    log_parser.add_argument('--show', action='store_true', help='Show log contents')

    # list command
    list_parser = subparsers.add_parser('list', help='List requests')
    list_parser.add_argument('--pending', action='store_true', help='Show pending')
    list_parser.add_argument('--completed', action='store_true', help='Show completed')
    list_parser.add_argument('--failed', action='store_true', help='Show failed')

    args = parser.parse_args()

    if args.command == 'submit':
        build_config = None
        if args.arm_hyp is not None:
            build_config = {
                'arm_hyp': args.arm_hyp,
                'platform': args.platform
            }
        ts = submit_sel4_efi_test(
            args.binary_path,
            binary_name=args.name,
            description=args.desc,
            build_config=build_config,
            profile=args.profile
        )
        print(f"Submitted: {ts}")
        if build_config:
            print(f"  ARM_HYPERVISOR_SUPPORT: {'ON' if build_config['arm_hyp'] else 'OFF'}")
            print(f"  Platform: {build_config['platform']}")
        if args.wait:
            print("Waiting for result...")
            result = wait_for_result(ts)
            print(f"Status: {result['status']}")
            if result['status'] == 'completed':
                logs = get_logs(ts, include_contents=True)
                for entry in logs["files"]:
                    print(f"\n=== {entry['path']} ===")
                    if "contents" in entry:
                        print(entry["contents"])

    elif args.command == 'status':
        result = get_status(args.timestamp)
        print(f"Status: {result['status']}")
        if 'result_dir' in result:
            print(f"Results: {result['result_dir']}")

    elif args.command == 'logs':
        logs = get_logs(args.timestamp, include_contents=args.show)
        if not logs["files"]:
            print(f"No console logs found in {logs['console_dir']}")
        else:
            print(f"Console logs in {logs['console_dir']}:")
            for entry in logs["files"]:
                print(f"  {entry['path']} ({entry['size']} bytes)")
                if "contents" in entry:
                    print(f"\n=== {entry['path']} ===")
                    print(entry["contents"])

    elif args.command == 'list':
        if args.pending or not (args.completed or args.failed):
            pending = list_pending()
            if pending:
                print("Pending:")
                for ts in pending:
                    print(f"  {ts}")
        if args.completed:
            completed = list_completed()
            if completed:
                print("Completed:")
                for ts in completed:
                    print(f"  {ts}")
        if args.failed:
            failed = list_failed()
            if failed:
                print("Failed:")
                for ts in failed:
                    print(f"  {ts}")

    else:
        parser.print_help()
