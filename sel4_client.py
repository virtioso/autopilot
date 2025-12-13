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
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Directory paths
AUTOPILOT_DIR = Path('/home/hlyytine/pkvm/autopilot')
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
    copy_to_staging: bool = True
) -> str:
    """
    Submit a seL4 EFI binary for testing.

    Args:
        binary_path: Path to the EFI binary to test
        binary_name: Name to use for the binary on target (default: sel4test.efi)
        description: Optional description of the test
        copy_to_staging: If True, copy binary to staging area (default: True)

    Returns:
        timestamp: Request ID that can be used to check status/get results
    """
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')

    # Ensure directories exist
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    BINARIES_DIR.mkdir(parents=True, exist_ok=True)

    # Resolve and validate binary path
    binary_src = Path(binary_path).resolve()
    if not binary_src.exists():
        raise FileNotFoundError(f"Binary not found: {binary_src}")

    # Determine binary path for request
    if copy_to_staging:
        staged_binary = BINARIES_DIR / binary_name
        shutil.copy(binary_src, staged_binary)
        request_binary_path = str(staged_binary)
    else:
        request_binary_path = str(binary_src)

    # Create request file
    request = {
        'type': 'sel4',
        'binary_path': request_binary_path,
        'binary_name': binary_name,
        'description': description,
        'submitted_at': timestamp,
        'original_binary': str(binary_src)
    }

    request_file = PENDING_DIR / f'{timestamp}.request'
    request_file.write_text(json.dumps(request, indent=2))

    return timestamp


def submit_multi_run_test(
    binary_path: str,
    run_count: int = 5,
    binary_name: str = 'sel4test.efi',
    test_type: str = 'sel4',
    description: str = '',
    copy_to_staging: bool = True
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

    Returns:
        timestamp: Request ID that can be used to check status/get results
    """
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')

    # Ensure directories exist
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    BINARIES_DIR.mkdir(parents=True, exist_ok=True)

    # Resolve and validate binary path
    binary_src = Path(binary_path).resolve()
    if not binary_src.exists():
        raise FileNotFoundError(f"Binary not found: {binary_src}")

    # Determine binary path for request
    if copy_to_staging:
        staged_binary = BINARIES_DIR / binary_name
        shutil.copy(binary_src, staged_binary)
        request_binary_path = str(staged_binary)
    else:
        request_binary_path = str(binary_src)

    # Create request file
    request = {
        'type': test_type,
        'binary_path': request_binary_path,
        'binary_name': binary_name,
        'description': description,
        'submitted_at': timestamp,
        'original_binary': str(binary_src),
        'multi_run': True,
        'run_count': run_count
    }

    request_file = PENDING_DIR / f'{timestamp}.request'
    request_file.write_text(json.dumps(request, indent=2))

    return timestamp


def get_multi_run_logs(timestamp: str) -> dict:
    """
    Get all logs from a multi-run test.

    Args:
        timestamp: Request ID from submit_multi_run_test()

    Returns:
        dict with:
            - 'runs': list of dicts with run_number, sel4_log/kernel_log, raw_log, error
            - 'summary': dict with total_runs, completed_runs, failed_runs (if available)
    """
    result_dir = RESULTS_DIR / timestamp
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


def get_status(timestamp: str) -> dict:
    """
    Get the current status of a test request.

    Args:
        timestamp: Request ID from submit_sel4_test()

    Returns:
        dict with 'status' key: 'pending', 'processing', 'completed', 'failed', or 'not_found'
    """
    request_name = f'{timestamp}.request'

    if (COMPLETED_DIR / request_name).exists():
        return {
            'status': 'completed',
            'result_dir': RESULTS_DIR / timestamp
        }
    elif (FAILED_DIR / request_name).exists():
        return {
            'status': 'failed',
            'result_dir': RESULTS_DIR / timestamp
        }
    elif (PROCESSING_DIR / request_name).exists():
        return {'status': 'processing'}
    elif (PENDING_DIR / request_name).exists():
        return {'status': 'pending'}
    else:
        return {'status': 'not_found'}


def wait_for_result(timestamp: str, timeout: int = 600, poll_interval: int = 5) -> dict:
    """
    Wait for test to complete and return results.

    Args:
        timestamp: Request ID from submit_sel4_test()
        timeout: Maximum time to wait in seconds (default: 600 = 10 minutes)
        poll_interval: How often to check status in seconds (default: 5)

    Returns:
        dict with 'status' key: 'completed', 'failed', or 'timeout'
              and 'result_dir' key if completed/failed
    """
    start = time.time()
    while time.time() - start < timeout:
        status = get_status(timestamp)
        if status['status'] in ('completed', 'failed'):
            return status
        time.sleep(poll_interval)

    return {'status': 'timeout'}


def get_sel4_log(timestamp: str) -> str:
    """
    Read the seL4 console output (filtered, bootloader stripped).

    Args:
        timestamp: Request ID from submit_sel4_test()

    Returns:
        Filtered seL4 console output, or empty string if not available
    """
    log_file = RESULTS_DIR / timestamp / 'sel4.log'
    if log_file.exists():
        return log_file.read_text()
    return ''


def get_raw_log(timestamp: str) -> str:
    """
    Read the raw UART output (includes bootloader/UEFI).

    Args:
        timestamp: Request ID from submit_sel4_test()

    Returns:
        Raw UART output, or empty string if not available
    """
    log_file = RESULTS_DIR / timestamp / 'uart-raw.log'
    if log_file.exists():
        return log_file.read_text()
    return ''


def get_request_info(timestamp: str) -> Optional[dict]:
    """
    Get the original request metadata.

    Args:
        timestamp: Request ID from submit_sel4_test()

    Returns:
        Request dict, or None if not found
    """
    for directory in [COMPLETED_DIR, FAILED_DIR, PROCESSING_DIR, PENDING_DIR]:
        request_file = directory / f'{timestamp}.request'
        if request_file.exists():
            return json.loads(request_file.read_text())
    return None


def list_pending() -> list:
    """List all pending request timestamps."""
    return sorted([f.stem for f in PENDING_DIR.glob('*.request')])


def list_completed() -> list:
    """List all completed request timestamps."""
    return sorted([f.stem for f in COMPLETED_DIR.glob('*.request')])


def list_failed() -> list:
    """List all failed request timestamps."""
    return sorted([f.stem for f in FAILED_DIR.glob('*.request')])


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
    submit_parser.add_argument('--wait', action='store_true', help='Wait for result')

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
    multi_parser.add_argument('--wait', action='store_true', help='Wait for result')

    # logs-multi command
    logs_multi_parser = subparsers.add_parser('logs-multi', help='Get multi-run logs')
    logs_multi_parser.add_argument('timestamp', help='Request timestamp')
    logs_multi_parser.add_argument('--summary', action='store_true', help='Show summary only')

    args = parser.parse_args()

    if args.command == 'submit':
        ts = submit_sel4_test(args.binary_path, args.name, args.desc)
        print(f"Submitted: {ts}")
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
        ts = submit_multi_run_test(
            args.binary_path,
            run_count=args.runs,
            binary_name=args.name,
            test_type=args.type,
            description=args.desc
        )
        print(f"Submitted multi-run test: {ts} ({args.runs} runs)")
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

    else:
        parser.print_help()
