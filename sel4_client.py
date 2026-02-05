#!/usr/bin/env python3
"""
seL4 Autopilot Client Library

Helper library for AI agents (or humans) to submit seL4 EFI binary tests
to the autopilot service and retrieve results.

Usage:
    from sel4_client import submit_sel4_test, wait_for_result, get_sel4_log

    timestamp = submit_sel4_test(
        binary_path='/path/to/sel4test-driver-image-arm-orinagx',
        binary_name='sel4test.efi',
        description='Testing MMU enable with TCU debug'
    )

    result = wait_for_result(timestamp)
    if result['status'] == 'completed':
        log = get_sel4_log(timestamp)
        print(f"seL4 output:\\n{log}")
"""

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Default autopilot directory (can be overridden via AUTOPILOT_DIR env var or function parameter)
DEFAULT_AUTOPILOT_DIR = Path('/home/hlyytine/tii-sel4/autopilot')


def get_autopilot_dir(override: str = None) -> Path:
    """Get the autopilot working directory.

    Priority order:
    1. override parameter (if provided)
    2. AUTOPILOT_DIR environment variable (if set)
    3. Default: /home/hlyytine/pkvm/autopilot

    Args:
        override: Optional path to use instead of env var or default

    Returns:
        Path to the autopilot directory
    """
    if override:
        return Path(override)
    env_dir = os.environ.get('AUTOPILOT_DIR')
    if env_dir:
        return Path(env_dir)
    return DEFAULT_AUTOPILOT_DIR


def get_paths(autopilot_dir: str = None) -> dict:
    """Get all autopilot paths derived from the base directory.

    Args:
        autopilot_dir: Optional override for the autopilot directory

    Returns:
        dict with keys: autopilot, pending, processing, completed, failed, results, binaries
    """
    base = get_autopilot_dir(autopilot_dir)
    return {
        'autopilot': base,
        'pending': base / 'requests' / 'pending',
        'processing': base / 'requests' / 'processing',
        'completed': base / 'requests' / 'completed',
        'failed': base / 'requests' / 'failed',
        'results': base / 'results',
        'binaries': base / 'binaries',
    }


# Legacy module-level paths for backward compatibility
# These use the default/environment-based directory
AUTOPILOT_DIR = get_autopilot_dir()
PENDING_DIR = AUTOPILOT_DIR / 'requests' / 'pending'
PROCESSING_DIR = AUTOPILOT_DIR / 'requests' / 'processing'
COMPLETED_DIR = AUTOPILOT_DIR / 'requests' / 'completed'
FAILED_DIR = AUTOPILOT_DIR / 'requests' / 'failed'
RESULTS_DIR = AUTOPILOT_DIR / 'results'
BINARIES_DIR = AUTOPILOT_DIR / 'binaries'


def submit_sel4_test(
    binary_path: str,
    binary_name: str = 'sel4test.efi',
    description: str = '',
    copy_to_staging: bool = True,
    build_config: dict = None,
    autopilot_dir: str = None,
    profile: str = 'sel4-efi'
) -> str:
    """
    Submit a seL4 EFI binary for testing.

    Args:
        binary_path: Path to the EFI binary to test
        binary_name: Name to use for the binary on target (default: sel4test.efi)
        description: Optional description of the test
        copy_to_staging: If True, copy binary to staging area (default: True)
        build_config: Build configuration dict with keys:
            - arm_hyp (bool): ARM_HYPERVISOR_SUPPORT setting (REQUIRED)
            - platform (str): Platform name (e.g., 'orinagx')
            - num_nodes (int): SMP core count (optional)
        autopilot_dir: Optional override for autopilot working directory
        profile: Profile name that defines the chain to run

    Returns:
        timestamp: Request ID that can be used to check status/get results

    Raises:
        ValueError: If build_config is missing or lacks arm_hyp
    """
    # Validate build_config
    if build_config is None:
        raise ValueError("build_config is required. Must specify arm_hyp (True/False)")
    if 'arm_hyp' not in build_config:
        raise ValueError("build_config must include 'arm_hyp' (True/False)")

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
    request = {
        'profile': profile,
        'type': 'sel4',
        'binary_path': request_binary_path,
        'binary_name': binary_name,
        'description': description,
        'submitted_at': timestamp,
        'original_binary': str(binary_src),
        'build_config': build_config
    }

    request_file = paths['pending'] / f'{timestamp}.request'
    request_file.write_text(json.dumps(request, indent=2))

    return timestamp


def submit_multi_run_test(
    binary_path: str,
    run_count: int = 5,
    binary_name: str = 'sel4test.efi',
    test_type: str = 'sel4',
    description: str = '',
    copy_to_staging: bool = True,
    build_config: dict = None,
    autopilot_dir: str = None,
    profile: str = None
) -> str:
    """
    Submit a test for multiple boot iterations without re-uploading the binary.

    Args:
        binary_path: Path to the EFI binary (seL4) or kernel image (Linux)
        run_count: Number of boot iterations (default: 5)
        binary_name: Name to use for the binary on target (default: sel4test.efi)
        test_type: 'sel4' or 'linux' (default: sel4)
        description: Optional description of the test
        copy_to_staging: If True, copy binary to staging area (default: True)
        build_config: Build configuration dict with keys:
            - arm_hyp (bool): ARM_HYPERVISOR_SUPPORT setting (REQUIRED for sel4)
            - platform (str): Platform name (e.g., 'orinagx')
            - num_nodes (int): SMP core count (optional)
        autopilot_dir: Optional override for autopilot working directory
        profile: Profile name to use (defaults based on test_type)

    Returns:
        timestamp: Request ID that can be used to check status/get results

    Raises:
        ValueError: If build_config is missing or lacks arm_hyp (for sel4 tests)
    """
    # Validate build_config for seL4 tests
    if test_type == 'sel4':
        if build_config is None:
            raise ValueError("build_config is required for seL4 tests. Must specify arm_hyp (True/False)")
        if 'arm_hyp' not in build_config:
            raise ValueError("build_config must include 'arm_hyp' (True/False)")

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

    if profile is None:
        profile = 'sel4-efi-multi' if test_type == 'sel4' else 'linux-kernel-multi'

    # Create request file
    request = {
        'profile': profile,
        'type': test_type,
        'binary_path': request_binary_path,
        'binary_name': binary_name,
        'description': description,
        'submitted_at': timestamp,
        'original_binary': str(binary_src),
        'multi_run': True,
        'run_count': run_count,
        'build_config': build_config
    }

    request_file = paths['pending'] / f'{timestamp}.request'
    request_file.write_text(json.dumps(request, indent=2))

    return timestamp


def submit_vm_minimal_test(
    binary_path: str,
    binary_name: str = 'capdl-vm_minimal.efi',
    description: str = '',
    copy_to_staging: bool = True,
    build_config: dict = None,
    autopilot_dir: str = None,
    profile: str = 'vm-minimal'
) -> str:
    """
    Submit a vm_minimal capdl-loader binary for testing.

    This test type captures both seL4/capdl-loader output (ttyACM0) and
    VM console output (ttyACM1). The test waits for 5 seconds of quiescence
    on the VM console before completing.

    Args:
        binary_path: Path to the capdl-loader EFI binary to test
        binary_name: Name to use for the binary on target
        description: Optional description of the test
        copy_to_staging: If True, copy binary to staging area (default: True)
        build_config: Build configuration dict with keys:
            - arm_hyp (bool): ARM_HYPERVISOR_SUPPORT setting
            - platform (str): Platform name (e.g., 'orinagx')
        autopilot_dir: Optional override for autopilot working directory
        profile: Profile name that defines the chain to run

    Returns:
        timestamp: Request ID that can be used to check status/get results
    """
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
    request = {
        'profile': profile,
        'type': 'vm_minimal',
        'binary_path': request_binary_path,
        'binary_name': binary_name,
        'description': description,
        'submitted_at': timestamp,
        'original_binary': str(binary_src),
        'build_config': build_config or {'arm_hyp': True, 'platform': 'orinagx'}
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
        "type": "boot_interactive",
        "boot_target": boot_target,
        "binary_path": request_binary_path,
        "binary_name": binary_name,
        "description": description,
        "submitted_at": timestamp,
        "interactive": interactive,
        "build_config": build_config
    }

    request_file = paths['pending'] / f"{timestamp}.request"
    request_file.write_text(json.dumps(request, indent=2))
    return timestamp


def get_vm_logs(timestamp: str, autopilot_dir: str = None) -> dict:
    """
    Get logs from a vm_minimal test.

    Args:
        timestamp: Request ID from submit_vm_minimal_test()
        autopilot_dir: Optional override for autopilot working directory

    Returns:
        dict with:
            - 'sel4_log': seL4/capdl-loader log content (or None if not found)
            - 'vm_log': VM console log content (or None if not found)
            - 'sel4_log_path': Path to seL4 log file
            - 'vm_log_path': Path to VM log file
    """
    paths = get_paths(autopilot_dir)
    result_dir = paths['results'] / timestamp

    sel4_log_path = result_dir / 'sel4.log'
    vm_log_path = result_dir / 'vm.log'

    result = {
        'sel4_log': None,
        'vm_log': None,
        'sel4_log_path': str(sel4_log_path),
        'vm_log_path': str(vm_log_path),
    }

    if sel4_log_path.exists():
        result['sel4_log'] = sel4_log_path.read_text()

    if vm_log_path.exists():
        result['vm_log'] = vm_log_path.read_text()

    return result


def get_multi_run_logs(timestamp: str, autopilot_dir: str = None) -> dict:
    """
    Get all logs from a multi-run test.

    Args:
        timestamp: Request ID from submit_multi_run_test()
        autopilot_dir: Optional override for autopilot working directory

    Returns:
        dict with:
            - 'runs': list of dicts with run_number, sel4_log/kernel_log, raw_log, error
            - 'summary': dict with total_runs, completed_runs, failed_runs (if available)
    """
    paths = get_paths(autopilot_dir)
    result_dir = paths['results'] / timestamp
    runs = []

    if not result_dir.exists():
        return {'runs': [], 'summary': None}

    # Look for numbered subdirectories (run_1, run_2, etc.)
    for run_dir in sorted(result_dir.glob('run_*')):
        try:
            run_num = int(run_dir.name.split('_')[1])
        except (IndexError, ValueError):
            continue

        run_data = {'run_number': run_num}

        # Get sel4.log or kernel.log
        if (run_dir / 'sel4.log').exists():
            run_data['sel4_log'] = (run_dir / 'sel4.log').read_text()
        elif (run_dir / 'kernel.log').exists():
            run_data['kernel_log'] = (run_dir / 'kernel.log').read_text()

        # Get raw log
        if (run_dir / 'uart-raw.log').exists():
            run_data['raw_log'] = (run_dir / 'uart-raw.log').read_text()

        # Get error if present
        if (run_dir / 'error.txt').exists():
            run_data['error'] = (run_dir / 'error.txt').read_text()

        runs.append(run_data)

    # Get summary if available
    summary = None
    summary_file = result_dir / 'summary.json'
    if summary_file.exists():
        try:
            summary = json.loads(summary_file.read_text())
        except json.JSONDecodeError:
            pass

    return {'runs': runs, 'summary': summary}


def get_status(timestamp: str, autopilot_dir: str = None) -> dict:
    """
    Get the current status of a test request.

    Args:
        timestamp: Request ID from submit_sel4_test()
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


def wait_for_result(timestamp: str, timeout: int = 600, poll_interval: int = 5,
                    autopilot_dir: str = None) -> dict:
    """
    Wait for test to complete and return results.

    Args:
        timestamp: Request ID from submit_sel4_test()
        timeout: Maximum time to wait in seconds (default: 600 = 10 minutes)
        poll_interval: How often to check status in seconds (default: 5)
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


def get_sel4_log(timestamp: str, autopilot_dir: str = None) -> str:
    """
    Read the seL4 console output (filtered, bootloader stripped).

    Args:
        timestamp: Request ID from submit_sel4_test()
        autopilot_dir: Optional override for autopilot working directory

    Returns:
        Filtered seL4 console output, or empty string if not available
    """
    paths = get_paths(autopilot_dir)
    log_file = paths['results'] / timestamp / 'sel4.log'
    if log_file.exists():
        return log_file.read_text()
    return ''


def get_raw_log(timestamp: str, autopilot_dir: str = None) -> str:
    """
    Read the raw UART output (includes bootloader/UEFI).

    Args:
        timestamp: Request ID from submit_sel4_test()
        autopilot_dir: Optional override for autopilot working directory

    Returns:
        Raw UART output, or empty string if not available
    """
    paths = get_paths(autopilot_dir)
    log_file = paths['results'] / timestamp / 'uart-raw.log'
    if log_file.exists():
        return log_file.read_text()
    return ''


def get_request_info(timestamp: str, autopilot_dir: str = None) -> Optional[dict]:
    """
    Get the original request metadata.

    Args:
        timestamp: Request ID from submit_sel4_test()
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
                'log_path': sess['log_path']
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
    submit_parser.add_argument('--profile', default='sel4-efi', help='Profile name (default: sel4-efi)')
    submit_parser.add_argument('--wait', action='store_true', help='Wait for result')
    # ARM_HYP configuration (mutually exclusive, one required)
    hyp_group = submit_parser.add_mutually_exclusive_group(required=True)
    hyp_group.add_argument('--arm-hyp', dest='arm_hyp', action='store_true',
                           help='Built with ARM_HYPERVISOR_SUPPORT=ON')
    hyp_group.add_argument('--no-arm-hyp', dest='arm_hyp', action='store_false',
                           help='Built with ARM_HYPERVISOR_SUPPORT=OFF')
    submit_parser.add_argument('--platform', default='orinagx', help='Platform name (default: orinagx)')

    # status command
    status_parser = subparsers.add_parser('status', help='Check test status')
    status_parser.add_argument('timestamp', help='Request timestamp')

    # log command
    log_parser = subparsers.add_parser('log', help='Get test output')
    log_parser.add_argument('timestamp', help='Request timestamp')
    log_parser.add_argument('--raw', action='store_true', help='Show raw output')

    # list command
    list_parser = subparsers.add_parser('list', help='List requests')
    list_parser.add_argument('--pending', action='store_true', help='Show pending')
    list_parser.add_argument('--completed', action='store_true', help='Show completed')
    list_parser.add_argument('--failed', action='store_true', help='Show failed')

    # submit-multi command
    multi_parser = subparsers.add_parser('submit-multi', help='Submit multi-run test')
    multi_parser.add_argument('binary_path', help='Path to EFI binary')
    multi_parser.add_argument('--name', default='sel4test.efi', help='Binary name on target')
    multi_parser.add_argument('--runs', type=int, default=5, help='Number of boot iterations')
    multi_parser.add_argument('--type', default='sel4', choices=['sel4', 'linux'], help='Test type')
    multi_parser.add_argument('--desc', default='', help='Description')
    multi_parser.add_argument('--profile', default=None, help='Profile name override')
    multi_parser.add_argument('--wait', action='store_true', help='Wait for result')
    # ARM_HYP configuration (required for sel4 tests)
    multi_hyp_group = multi_parser.add_mutually_exclusive_group()
    multi_hyp_group.add_argument('--arm-hyp', dest='arm_hyp', action='store_true', default=None,
                                  help='Built with ARM_HYPERVISOR_SUPPORT=ON (required for sel4)')
    multi_hyp_group.add_argument('--no-arm-hyp', dest='arm_hyp', action='store_false',
                                  help='Built with ARM_HYPERVISOR_SUPPORT=OFF')
    multi_parser.add_argument('--platform', default='orinagx', help='Platform name (default: orinagx)')

    # logs-multi command
    logs_multi_parser = subparsers.add_parser('logs-multi', help='Get multi-run logs')
    logs_multi_parser.add_argument('timestamp', help='Request timestamp')
    logs_multi_parser.add_argument('--summary', action='store_true', help='Show summary only')

    # submit-vm command for vm_minimal tests
    vm_parser = subparsers.add_parser('submit-vm', help='Submit vm_minimal test')
    vm_parser.add_argument('binary_path', help='Path to capdl-loader EFI binary')
    vm_parser.add_argument('--name', default='capdl-vm_minimal.efi', help='Binary name on target')
    vm_parser.add_argument('--desc', default='', help='Description')
    vm_parser.add_argument('--profile', default='vm-minimal', help='Profile name (default: vm-minimal)')
    vm_parser.add_argument('--wait', action='store_true', help='Wait for result')
    vm_parser.add_argument('--platform', default='orinagx', help='Platform name (default: orinagx)')

    # vm-logs command
    vm_logs_parser = subparsers.add_parser('vm-logs', help='Get vm_minimal logs')
    vm_logs_parser.add_argument('timestamp', help='Request timestamp')
    vm_logs_parser.add_argument('--sel4', action='store_true', help='Show seL4/capdl-loader log only')
    vm_logs_parser.add_argument('--vm', action='store_true', help='Show VM console log only')

    args = parser.parse_args()

    if args.command == 'submit':
        build_config = {
            'arm_hyp': args.arm_hyp,
            'platform': args.platform
        }
        ts = submit_sel4_test(
            args.binary_path,
            binary_name=args.name,
            description=args.desc,
            build_config=build_config,
            profile=args.profile
        )
        print(f"Submitted: {ts}")
        print(f"  ARM_HYPERVISOR_SUPPORT: {'ON' if args.arm_hyp else 'OFF'}")
        print(f"  Platform: {args.platform}")
        if args.wait:
            print("Waiting for result...")
            result = wait_for_result(ts)
            print(f"Status: {result['status']}")
            if result['status'] == 'completed':
                print(get_sel4_log(ts))

    elif args.command == 'status':
        result = get_status(args.timestamp)
        print(f"Status: {result['status']}")
        if 'result_dir' in result:
            print(f"Results: {result['result_dir']}")

    elif args.command == 'log':
        if args.raw:
            print(get_raw_log(args.timestamp))
        else:
            print(get_sel4_log(args.timestamp))

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

    elif args.command == 'submit-multi':
        # Build config (required for sel4, optional for linux)
        build_config = None
        if args.type == 'sel4':
            if args.arm_hyp is None:
                parser.error("--arm-hyp or --no-arm-hyp is required for seL4 tests")
            build_config = {
                'arm_hyp': args.arm_hyp,
                'platform': args.platform
            }
        elif args.arm_hyp is not None:
            # Linux test with explicit arm_hyp (optional but allowed)
            build_config = {
                'arm_hyp': args.arm_hyp,
                'platform': args.platform
            }

        ts = submit_multi_run_test(
            args.binary_path,
            run_count=args.runs,
            binary_name=args.name,
            test_type=args.type,
            description=args.desc,
            build_config=build_config,
            profile=args.profile
        )
        print(f"Submitted multi-run test: {ts} ({args.runs} runs)")
        if build_config:
            print(f"  ARM_HYPERVISOR_SUPPORT: {'ON' if build_config['arm_hyp'] else 'OFF'}")
            print(f"  Platform: {build_config['platform']}")
        if args.wait:
            print("Waiting for result...")
            # Scale timeout for multiple runs
            result = wait_for_result(ts, timeout=300 * args.runs)
            print(f"Status: {result['status']}")
            if result['status'] == 'completed':
                logs = get_multi_run_logs(ts)
                if logs['summary']:
                    s = logs['summary']
                    print(f"Summary: {s.get('completed_runs', '?')}/{s.get('total_runs', '?')} runs completed")
                for run in logs['runs']:
                    print(f"\n--- Run {run['run_number']} ---")
                    if 'error' in run:
                        print(f"Error: {run['error']}")
                    elif 'sel4_log' in run:
                        print(run['sel4_log'][:2000])
                    elif 'kernel_log' in run:
                        print(run['kernel_log'][:2000])

    elif args.command == 'logs-multi':
        logs = get_multi_run_logs(args.timestamp)
        if not logs['runs']:
            print(f"No multi-run logs found for {args.timestamp}")
        elif args.summary:
            if logs['summary']:
                s = logs['summary']
                print(f"Total runs: {s.get('total_runs', '?')}")
                print(f"Completed: {s.get('completed_runs', '?')}")
                print(f"Failed: {s.get('failed_runs', '?')}")
            else:
                print(f"Found {len(logs['runs'])} runs (no summary available)")
        else:
            for run in logs['runs']:
                print(f"\n=== Run {run['run_number']} ===")
                if 'error' in run:
                    print(f"Error: {run['error']}")
                elif 'sel4_log' in run:
                    print(run['sel4_log'])
                elif 'kernel_log' in run:
                    print(run['kernel_log'])

    elif args.command == 'submit-vm':
        build_config = {
            'arm_hyp': True,  # vm_minimal always uses hypervisor mode
            'platform': args.platform
        }
        ts = submit_vm_minimal_test(
            args.binary_path,
            binary_name=args.name,
            description=args.desc,
            build_config=build_config,
            profile=args.profile
        )
        print(f"Submitted vm_minimal test: {ts}")
        print(f"  Platform: {args.platform}")
        if args.wait:
            print("Waiting for result...")
            result = wait_for_result(ts)
            print(f"Status: {result['status']}")
            if result['status'] == 'completed':
                logs = get_vm_logs(ts)
                if logs['sel4_log']:
                    print("\n=== seL4/capdl-loader output ===")
                    print(logs['sel4_log'])
                if logs['vm_log']:
                    print("\n=== VM console output ===")
                    print(logs['vm_log'])

    elif args.command == 'vm-logs':
        logs = get_vm_logs(args.timestamp)
        if not logs['sel4_log'] and not logs['vm_log']:
            print(f"No vm_minimal logs found for {args.timestamp}")
        elif args.sel4:
            if logs['sel4_log']:
                print(logs['sel4_log'])
            else:
                print("No seL4 log available")
        elif args.vm:
            if logs['vm_log']:
                print(logs['vm_log'])
            else:
                print("No VM log available")
        else:
            # Show both
            if logs['sel4_log']:
                print("=== seL4/capdl-loader output ===")
                print(logs['sel4_log'])
            if logs['vm_log']:
                print("\n=== VM console output ===")
                print(logs['vm_log'])

    else:
        parser.print_help()
