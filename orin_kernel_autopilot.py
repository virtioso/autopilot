#! /usr/bin/env python3

import os
import select
import signal
import sys
import subprocess
import time
import traceback
import threading

import serial
from pathlib import Path

import BoardControl
import BootHarness

SCRIPT_DIR = Path(__file__).resolve().parent
# Use SCRIPT_DIR as base (script is at ${WORKSPACE}/autopilot/)
AUTOPILOT_DIR = SCRIPT_DIR

# Kernel paths
WORKSPACE = Path(os.environ.get('WORKSPACE', '/home/hlyytine/pkvm'))
KERNEL_DIR = WORKSPACE / 'Linux_for_Tegra/source/kernel/linux'
KERNEL_IMAGE = KERNEL_DIR / 'arch/arm64/boot/Image'
KERNEL_RELEASE_FILE = KERNEL_DIR / 'include/config/kernel.release'

# Directory structure for request queue
PENDING_DIR = AUTOPILOT_DIR / "requests" / "pending"
PROCESSING_DIR = AUTOPILOT_DIR / "requests" / "processing"
COMPLETED_DIR = AUTOPILOT_DIR / "requests" / "completed"
FAILED_DIR = AUTOPILOT_DIR / "requests" / "failed"
RESULTS_DIR = AUTOPILOT_DIR / "results"

def cleanup():
    """Move any processing requests back to pending on shutdown"""
    # Reset terminal scroll region
    BootHarness.reset_status_line()
    print("\nCleaning up...", flush=True)
    if PROCESSING_DIR.exists():
        for request_file in PROCESSING_DIR.glob("*.request"):
            try:
                request_file.rename(PENDING_DIR / request_file.name)
                print(f"Moved {request_file.name} back to pending", flush=True)
            except Exception as e:
                print(f"Error moving {request_file.name}: {e}", flush=True)

def handle_signal(signum, frame):
    cleanup()
    sys.exit(0)

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

# Create directory structure
for d in [PENDING_DIR, PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR, RESULTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Initialize status line (row 1 fixed, rows 2-N scroll)
BootHarness.init_status_line()

def status(msg):
    """Update the status line."""
    BootHarness.set_status(f'[AUTOPILOT] {msg}')

status('Service started')
print(f"Watching: {PENDING_DIR}", flush=True)
print(f"Results:  {RESULTS_DIR}", flush=True)

# === STARTUP: Boot to ready state ===
status('Booting board to ready state...')
board = BoardControl.BoardControlLocal()
ready = BootHarness.ReadyBootHarness(
    board,
    '/dev/ttyACM0',
    str(AUTOPILOT_DIR / 'startup.log'),
    None,
    None
)
ready.run()
status('Waiting for requests...')

while True:
    # Find pending requests (oldest first)
    requests = sorted(PENDING_DIR.glob("*.request"))

    if not requests:
        # Check for user input while waiting (interactive mode)
        if BootHarness.check_stdin_ready():
            line = sys.stdin.readline()
            if line:
                status('Interactive mode (Ctrl+C to exit)')
                # Create serial connection for interactive mode
                ser = serial.Serial('/dev/ttyACM0', 115200, timeout=0.2)
                ser.write(line.encode('utf-8'))  # Forward the initial input
                BootHarness.enter_interactive_mode(ser)
                ser.close()
                status('Waiting for requests...')
        time.sleep(1)
        continue

    # Process the oldest request
    request_file = requests[0]
    timestamp = request_file.stem

    status(f'Processing request: {timestamp}')
    print(f"\n=== New request: {timestamp} ===", flush=True)

    # Move to processing directory (atomic operation)
    processing_file = PROCESSING_DIR / request_file.name
    try:
        request_file.rename(processing_file)
    except Exception as e:
        print(f"ERROR: Failed to move request to processing: {e}", flush=True)
        time.sleep(1)
        continue

    # Create results directory
    result_dir = RESULTS_DIR / timestamp
    try:
        result_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"ERROR: Failed to create results directory: {e}", flush=True)
        processing_file.rename(FAILED_DIR / request_file.name)
        continue

    print(f"Results: {result_dir}/", flush=True)

    # Process the request
    try:
        # Read kernel version
        kernel_version = KERNEL_RELEASE_FILE.read_text().strip()
        print(f"Kernel: {kernel_version}", flush=True)

        # Start centralized hyp logging ONCE - continues across both boot harnesses
        # This ensures we capture ALL hyp output including early boot after reboot
        hyp_stop_evt = threading.Event()
        hyp_log_thread = threading.Thread(
            target=BootHarness.log_port,
            args=('/dev/ttyACM1', str(result_dir / 'uarti.log'), hyp_stop_evt),
            daemon=True
        )
        hyp_log_thread.start()

        status(f'{timestamp}: Uploading kernel...')
        seq = BootHarness.UpdateBootHarness(
            board,
            '/dev/ttyACM0',
            str(result_dir / 'kernel-update.log'),
            None,  # No local hyp logging - using centralized
            None,
            str(KERNEL_IMAGE),
            kernel_version
        )
        seq.run()

        status(f'{timestamp}: Booting test kernel...')
        seq = BootHarness.PanicBootHarness(
            board,
            '/dev/ttyACM0',
            str(result_dir / 'uart-raw.log'),
            None,  # No local hyp logging - using centralized
            None
        )
        seq.run()
        fault_type = seq.fault_type
        user_interrupted = seq.user_interrupted
        print(f"Test result: {fault_type}", flush=True)

        # Stop centralized hyp logging
        hyp_stop_evt.set()
        hyp_log_thread.join(timeout=1.0)

        # === CONDITIONAL RECOVERY ===
        # If boot was successful or user interrupted, skip recovery - board is usable
        # Otherwise, start recovery boot in background
        recovery_exception = [None]  # Mutable container for thread exception
        recovery_thread = None

        if fault_type in ('success', 'user_interrupted'):
            status(f'{timestamp}: Processing logs...')
        else:
            status(f'{timestamp}: Recovery + processing logs...')

            def recovery_thread_fn():
                try:
                    ready = BootHarness.ReadyBootHarness(
                        board,
                        '/dev/ttyACM0',
                        str(result_dir / 'recovery.log'),
                        None,
                        None
                    )
                    ready.run()
                except Exception as e:
                    recovery_exception[0] = e

            recovery_thread = threading.Thread(target=recovery_thread_fn, daemon=True)
            recovery_thread.start()

        # === LOG FILTERING (parallel with recovery boot) ===
        with open(result_dir / 'uart-raw.log', "rb") as fin, \
             open(result_dir / 'uart.log', "wb") as fout:
            subprocess.run(
                SCRIPT_DIR / 'filter_mb1_start.py',
                stdin=fin,
                stdout=fout,
                check=True
            )

        with open(result_dir / 'uart.log', "rb") as fin, \
             open(result_dir / 'kernel.log', "wb") as fout:
            subprocess.run(
                SCRIPT_DIR / 'filter_kernel_start.py',
                stdin=fin,
                stdout=fout,
                check=True
            )

        with open(result_dir / 'kernel.log', "rb") as fin, \
             open(result_dir / 'panic.log', "wb") as fout:
            subprocess.run(
                SCRIPT_DIR / 'filter_nvhe_bug.py',
                stdin=fin,
                stdout=fout,
                check=True
            )

        with open(result_dir / 'uarti.log', "rb") as fin, \
             open(result_dir / 'hyp.log', "wb") as fout:
            subprocess.run(
                SCRIPT_DIR / 'filter_hyp_output.py',
                stdin=fin,
                stdout=fout,
                check=True
            )

        # Extract SMMU faults if detected
        if fault_type == 'smmu_fault':
            with open(result_dir / 'kernel.log', "rb") as fin, \
                 open(result_dir / 'smmu_faults.log', "wb") as fout:
                subprocess.run(
                    SCRIPT_DIR / 'filter_smmu_faults.py',
                    stdin=fin,
                    stdout=fout,
                    check=True
                )

        # Only disassemble if we have a panic (not for SMMU faults)
        if fault_type == 'panic':
            with open(result_dir / 'disassembly.log', "wb") as fout:
                subprocess.run(
                    [SCRIPT_DIR / 'disasm_2nd_frame.py', str(result_dir / 'kernel.log')],
                    stdout=fout,
                    check=True
                )
        else:
            # Create empty disassembly.log for consistency
            (result_dir / 'disassembly.log').write_text(
                f"No disassembly available (fault_type: {fault_type})\n"
            )

        # Success - move to completed (don't wait for recovery)
        processing_file.rename(COMPLETED_DIR / request_file.name)

        print(f"\n=== {timestamp} completed: {fault_type} ===", flush=True)
        print(f"Results: {result_dir}/", flush=True)

        # === WAIT FOR RECOVERY IF NEEDED ===
        if recovery_thread:
            status(f'{timestamp}: Waiting for recovery...')
            recovery_thread.join()
            if recovery_exception[0]:
                raise recovery_exception[0]

        # === ENTER INTERACTIVE MODE IF USER INTERRUPTED ===
        if user_interrupted:
            status('Interactive mode (Ctrl+C to exit)')
            # Reopen serial for interactive use
            ser = serial.Serial('/dev/ttyACM0', 115200, timeout=0.2)
            BootHarness.enter_interactive_mode(ser)
            ser.close()

        status('Waiting for requests...')

    except Exception as e:
        # Failure - move to failed directory
        processing_file.rename(FAILED_DIR / request_file.name)

        # Cleanup hyp logging if it was started
        try:
            hyp_stop_evt.set()
            hyp_log_thread.join(timeout=1.0)
        except NameError:
            pass  # hyp logging wasn't started yet

        status(f'{timestamp}: FAILED - recovering...')
        print(f"\n=== {timestamp} FAILED ===", flush=True)
        print(f"Error: {e}", flush=True)
        traceback.print_exc()

        # Try to recover even on failure
        recovery_log = str(result_dir / 'recovery.log') if result_dir.exists() else str(AUTOPILOT_DIR / 'recovery.log')
        recovery_exception = [None]

        def recovery_thread_fn():
            try:
                ready = BootHarness.ReadyBootHarness(
                    board,
                    '/dev/ttyACM0',
                    recovery_log,
                    None,
                    None
                )
                ready.run()
            except Exception as re:
                recovery_exception[0] = re

        recovery_thread = threading.Thread(target=recovery_thread_fn, daemon=True)
        recovery_thread.start()
        recovery_thread.join()

        if recovery_exception[0]:
            status('Recovery FAILED - manual intervention needed')
            print(f"Recovery failed: {recovery_exception[0]}", flush=True)
        else:
            status('Waiting for requests...')
