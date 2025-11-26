#! /usr/bin/env python3

import os
import signal
import sys
import subprocess
import time
import traceback

from pathlib import Path

import BoardControl
import BootHarness

SCRIPT_DIR = Path(__file__).resolve().parent
# Use SCRIPT_DIR as base (script is at ${WORKSPACE}/autopilot/)
AUTOPILOT_DIR = SCRIPT_DIR

# Directory structure for request queue
PENDING_DIR = AUTOPILOT_DIR / "requests" / "pending"
PROCESSING_DIR = AUTOPILOT_DIR / "requests" / "processing"
COMPLETED_DIR = AUTOPILOT_DIR / "requests" / "completed"
FAILED_DIR = AUTOPILOT_DIR / "requests" / "failed"
RESULTS_DIR = AUTOPILOT_DIR / "results"

def cleanup():
    """Move any processing requests back to pending on shutdown"""
    print("[AUTOPILOT] Cleaning up...", flush=True)
    if PROCESSING_DIR.exists():
        for request_file in PROCESSING_DIR.glob("*.request"):
            try:
                request_file.rename(PENDING_DIR / request_file.name)
                print(f"[AUTOPILOT] Moved {request_file.name} back to pending", flush=True)
            except Exception as e:
                print(f"[AUTOPILOT] Error moving {request_file.name}: {e}", flush=True)

def handle_signal(signum, frame):
    cleanup()
    sys.exit(0)

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

# Create directory structure
for d in [PENDING_DIR, PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR, RESULTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)
    print(f"[AUTOPILOT] Created directory: {d}", flush=True)

print(f"[AUTOPILOT] Autopilot service started", flush=True)
print(f"[AUTOPILOT] Watching for requests in: {PENDING_DIR}", flush=True)
print(f"[AUTOPILOT] Results will be written to: {RESULTS_DIR}", flush=True)

while True:
    # Find pending requests (oldest first)
    requests = sorted(PENDING_DIR.glob("*.request"))

    if not requests:
        # No pending requests, sleep and check again
        time.sleep(1)
        continue

    # Process the oldest request
    request_file = requests[0]
    timestamp = request_file.stem

    print(f"\n[AUTOPILOT] ========================================", flush=True)
    print(f"[AUTOPILOT] Found new request: {timestamp}", flush=True)
    print(f"[AUTOPILOT] ========================================", flush=True)

    # Move to processing directory (atomic operation)
    processing_file = PROCESSING_DIR / request_file.name
    try:
        request_file.rename(processing_file)
    except Exception as e:
        print(f"[AUTOPILOT] ERROR: Failed to move request to processing: {e}", flush=True)
        time.sleep(1)
        continue

    # Create results directory
    result_dir = RESULTS_DIR / timestamp
    try:
        result_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[AUTOPILOT] ERROR: Failed to create results directory: {e}", flush=True)
        processing_file.rename(FAILED_DIR / request_file.name)
        continue

    print(f"[AUTOPILOT] Results will be saved to: {result_dir}", flush=True)

    # Process the request
    try:
        print(f"[AUTOPILOT] Initializing board controller...", flush=True)
        board = BoardControl.BoardControlRemote()

        print(f"[AUTOPILOT] Starting kernel update harness...", flush=True)
        seq = BootHarness.UpdateBootHarness(
            board,
            '/tmp/ttyACM0',
            str(result_dir / 'kernel-update.log'),
            '/tmp/ttyACM1',
            str(result_dir / 'uarti-dummy.log')
        )
        seq.run()
        print(f"[AUTOPILOT] Kernel update completed", flush=True)

        print(f"[AUTOPILOT] Starting panic boot harness...", flush=True)
        seq = BootHarness.PanicBootHarness(
            board,
            '/tmp/ttyACM0',
            str(result_dir / 'kernel.log'),
            '/tmp/ttyACM1',
            str(result_dir / 'uarti.log')
        )
        seq.run()
        fault_type = seq.fault_type
        print(f"[AUTOPILOT] Panic boot harness completed (fault_type: {fault_type})", flush=True)

        print(f"[AUTOPILOT] Filtering kernel panic log...", flush=True)
        with open(result_dir / 'kernel.log', "rb") as fin, \
             open(result_dir / 'panic.log', "wb") as fout:
            subprocess.run(
                SCRIPT_DIR / 'filter_nvhe_bug.py',
                stdin=fin,
                stdout=fout,
                check=True
            )

        print(f"[AUTOPILOT] Filtering hypervisor log...", flush=True)
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
            print(f"[AUTOPILOT] Extracting SMMU fault details...", flush=True)
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
            print(f"[AUTOPILOT] Disassembling crash site...", flush=True)
            with open(result_dir / 'disassembly.log', "wb") as fout:
                subprocess.run(
                    [SCRIPT_DIR / 'disasm_2nd_frame.py', str(result_dir / 'kernel.log')],
                    stdout=fout,
                    check=True
                )
        else:
            print(f"[AUTOPILOT] Skipping disassembly (no panic detected)", flush=True)
            # Create empty disassembly.log for consistency
            (result_dir / 'disassembly.log').write_text(
                f"No disassembly available (fault_type: {fault_type})\n"
            )

        # Success - move to completed
        processing_file.rename(COMPLETED_DIR / request_file.name)

        print(f"\n[AUTOPILOT] ========================================", flush=True)
        print(f"[AUTOPILOT] Request {timestamp} completed successfully!", flush=True)
        print(f"[AUTOPILOT] Fault type: {fault_type}", flush=True)
        print(f"[AUTOPILOT] ========================================", flush=True)
        print(f"[AUTOPILOT] Results location: {result_dir}/", flush=True)
        print(f"[AUTOPILOT]   - Full kernel log: kernel.log", flush=True)
        print(f"[AUTOPILOT]   - Panic message:   panic.log", flush=True)
        print(f"[AUTOPILOT]   - Hypervisor log:  hyp.log", flush=True)
        if fault_type == 'smmu_fault':
            print(f"[AUTOPILOT]   - SMMU faults:    smmu_faults.log", flush=True)
        if fault_type == 'panic':
            print(f"[AUTOPILOT]   - Disassembly:    disassembly.log", flush=True)
        print(f"[AUTOPILOT] ========================================\n", flush=True)

    except Exception as e:
        # Failure - move to failed directory
        processing_file.rename(FAILED_DIR / request_file.name)

        print(f"\n[AUTOPILOT] ========================================", flush=True)
        print(f"[AUTOPILOT] Request {timestamp} FAILED", flush=True)
        print(f"[AUTOPILOT] ========================================", flush=True)
        print(f"[AUTOPILOT] Error: {e}", flush=True)
        print(f"[AUTOPILOT] Traceback:", flush=True)
        traceback.print_exc()
        print(f"[AUTOPILOT] Partial results may be in: {result_dir}/", flush=True)
        print(f"[AUTOPILOT] ========================================\n", flush=True)
