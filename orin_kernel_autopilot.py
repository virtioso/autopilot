#! /usr/bin/env python3

import json
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
from typing import List, Dict

import BoardControl
import BootHarness
import seL4BootHarness
from console_sessions import ConsoleManager

SCRIPT_DIR = Path(__file__).resolve().parent
# Use AUTOPILOT_DIR env var if set, otherwise use script directory
AUTOPILOT_DIR = Path(os.environ.get('AUTOPILOT_DIR', str(SCRIPT_DIR)))

# Target board IP for SSH
TARGET_IP = '192.168.101.112'


def try_ssh_reboot(timeout: int = 10) -> bool:
    """
    Attempt to reboot the board via SSH. Returns True if successful.

    Checks if SSH port is open, then sends reboot command.
    Used for Linux multi-run tests when board boots successfully.
    """
    import socket

    # Quick TCP check for SSH port
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        if sock.connect_ex((TARGET_IP, 22)) != 0:
            sock.close()
            return False
        sock.close()
    except Exception:
        return False

    # Send reboot command
    try:
        subprocess.run(
            ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=5',
             f'root@{TARGET_IP}', 'reboot'],
            capture_output=True, timeout=timeout
        )
        return True
    except Exception:
        return False

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
PROFILES_DIR = AUTOPILOT_DIR / "profiles"

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
for d in [PENDING_DIR, PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR, RESULTS_DIR, PROFILES_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# === STARTUP CLEANUP: Fail any leftover pending requests from previous run ===
# When autopilot is force-quit, pending requests remain. Mark them as failed.
for stale_request in list(PENDING_DIR.glob("*.request")):
    stale_timestamp = stale_request.stem
    print(f"Found stale pending request: {stale_timestamp}", flush=True)

    # Create results directory and write error
    stale_results_dir = RESULTS_DIR / stale_timestamp
    stale_results_dir.mkdir(parents=True, exist_ok=True)
    (stale_results_dir / "error.txt").write_text("autopilot restarted\n")

    # Move to failed
    stale_request.rename(FAILED_DIR / stale_request.name)
    print(f"  -> Marked as failed: autopilot restarted", flush=True)

# Initialize status line (row 1 fixed, rows 2-N scroll)
BootHarness.init_status_line()

# Interactive console manager
console_manager = ConsoleManager(AUTOPILOT_DIR)

def write_console_manifest(result_dir: Path, sessions: List[Dict]) -> None:
    console_dir = result_dir / "console"
    console_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "request_id": result_dir.name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "active",
        "sessions": sessions
    }
    (console_dir / "sessions.json").write_text(json.dumps(manifest, indent=2))

def update_console_manifest_status(result_dir: Path, status_value: str) -> None:
    manifest_path = result_dir / "console" / "sessions.json"
    if not manifest_path.exists():
        return
    try:
        data = json.loads(manifest_path.read_text())
        data["status"] = status_value
        manifest_path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

def run_interactive_phase(result_dir: Path, interactive_cfg: dict) -> None:
    sessions_cfg = interactive_cfg.get("sessions", [])
    if not sessions_cfg:
        raise ValueError("interactive.enabled set but no sessions configured")

    console_dir = result_dir / "console"
    console_dir.mkdir(parents=True, exist_ok=True)

    sessions_meta = []
    sessions = []
    for sess in sessions_cfg:
        name = sess.get("name")
        port = sess.get("port")
        profile_name = sess.get("profile", "linux-yocto")
        if not name or not port:
            raise ValueError("interactive session requires name and port")

        session = console_manager.create_session(
            request_id=result_dir.name,
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
            "events_path": str(session.events_path)
        })

    write_console_manifest(result_dir, sessions_meta)

    # Attempt auto-login per profile (best-effort)
    for session in sessions:
        try:
            session.perform_login(timeout_s=60)
        except Exception:
            pass

    idle_timeout = int(interactive_cfg.get("idle_timeout_s", 900))
    last_activity = time.time()

    try:
        while True:
            active_sessions = 0
            for session in list(sessions):
                runtime_dir = session.runtime_dir
                close_flag = runtime_dir / "close"
                if close_flag.exists():
                    try:
                        close_flag.unlink()
                    except Exception:
                        pass
                    console_manager.close_session(session.session_id)
                    sessions.remove(session)
                    continue

                cmd_dir = runtime_dir / "cmd"
                resp_dir = runtime_dir / "resp"
                if cmd_dir.exists():
                    for cmd_file in sorted(cmd_dir.glob("*.json")):
                        try:
                            cmd_data = json.loads(cmd_file.read_text())
                        except Exception as e:
                            resp = {"error": f"invalid command file: {e}"}
                            resp_path = resp_dir / f"{cmd_file.stem}.json"
                            resp_path.write_text(json.dumps(resp, indent=2))
                            cmd_file.unlink(missing_ok=True)
                            continue

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
                        resp_path = resp_dir / f"{cmd_id}.json"
                        resp_path.write_text(json.dumps(result, indent=2))
                        cmd_file.unlink(missing_ok=True)
                        last_activity = time.time()

                active_sessions += 1

            if active_sessions == 0:
                update_console_manifest_status(result_dir, "closed")
                break

            if idle_timeout > 0 and (time.time() - last_activity) > idle_timeout:
                for session in list(sessions):
                    console_manager.close_session(session.session_id)
                    sessions.remove(session)
                update_console_manifest_status(result_dir, "idle_timeout")
                break

            time.sleep(0.2)
    finally:
        for session in list(sessions):
            console_manager.close_session(session.session_id)

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

# Track board state: 'stock_linux' means we're at stock Jetson Linux shell
# This allows seL4 tests to skip unnecessary reboots
board_state = 'stock_linux'

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

    # Read request data (JSON if present, otherwise assume Linux kernel test)
    try:
        request_data = json.loads(processing_file.read_text())
    except (json.JSONDecodeError, Exception):
        request_data = {'type': 'linux'}  # Default to Linux kernel test

    request_type = request_data.get('type', 'linux')
    is_multi_run = request_data.get('multi_run', False)
    run_count = request_data.get('run_count', 1)
    build_config = request_data.get('build_config')
    print(f"Request type: {request_type}", flush=True)
    if is_multi_run:
        print(f"Multi-run mode: {run_count} iterations", flush=True)

    # Write build configuration to results directory
    if build_config:
        config_file = result_dir / 'config.json'
        config_file.write_text(json.dumps(build_config, indent=2))
        print(f"Build config: ARM_HYP={'ON' if build_config.get('arm_hyp') else 'OFF'}, platform={build_config.get('platform', 'unknown')}", flush=True)

    # Process the request
    try:
        if request_type == 'boot_interactive':
            interactive_cfg = request_data.get('interactive', {})
            if not interactive_cfg.get('enabled', False):
                raise ValueError("boot_interactive requires interactive.enabled=true")

            boot_target = request_data.get('boot_target', 'stock_linux')
            binary_path = request_data.get('binary_path')
            binary_name = request_data.get('binary_name', 'sel4test.efi')

            if boot_target == 'stock_linux':
                if board_state != 'stock_linux':
                    status(f'{timestamp}: Booting to stock Linux...')
                    ready = BootHarness.ReadyBootHarness(
                        board,
                        '/dev/ttyACM0',
                        str(result_dir / 'recovery.log'),
                        None,
                        None
                    )
                    ready.run()
                    board_state = 'stock_linux'
            else:
                if not binary_path:
                    raise ValueError("boot_interactive for EFI requires 'binary_path'")

                status(f'{timestamp}: Uploading EFI binary...')
                if board_state == 'stock_linux':
                    upload = seL4BootHarness.SeL4UploadOnlyHarness(
                        binary_path,
                        binary_name
                    )
                else:
                    upload = seL4BootHarness.SeL4UploadHarness(
                        board,
                        '/dev/ttyACM0',
                        str(result_dir / 'upload.log'),
                        binary_path,
                        binary_name
                    )
                upload.run()
                board_state = 'rebooting'

                status(f'{timestamp}: Booting EFI binary (interactive)...')
                runner = seL4BootHarness.SeL4RunInteractiveHarness(
                    board,
                    '/dev/ttyACM0',
                    str(result_dir / 'uart-raw.log'),
                    binary_name
                )
                runner.run()
                board_state = 'unknown'

            status(f'{timestamp}: Interactive sessions active...')
            run_interactive_phase(result_dir, interactive_cfg)

            processing_file.rename(COMPLETED_DIR / request_file.name)
            print(f"\n=== {timestamp} completed: boot_interactive ===", flush=True)
            print(f"Results: {result_dir}/", flush=True)
            status('Waiting for requests...')

        elif is_multi_run and request_type == 'sel4':
            # === seL4 MULTI-RUN TEST FLOW ===
            binary_path = request_data.get('binary_path')
            binary_name = request_data.get('binary_name', 'sel4test.efi')

            if not binary_path:
                raise ValueError("seL4 request missing 'binary_path'")

            print(f"seL4 binary: {binary_path} -> {binary_name}", flush=True)
            completed_runs = 0

            for run_num in range(1, run_count + 1):
                run_dir = result_dir / f'run_{run_num}'
                run_dir.mkdir(parents=True, exist_ok=True)

                status(f'{timestamp}: Run {run_num}/{run_count} - starting...')
                print(f"\n--- Run {run_num}/{run_count} ---", flush=True)

                try:
                    if run_num == 1:
                        # First run: upload binary
                        status(f'{timestamp}: Run {run_num}/{run_count} - uploading...')
                        if board_state == 'stock_linux':
                            print("Board already at stock Linux, skipping boot", flush=True)
                            upload = seL4BootHarness.SeL4UploadOnlyHarness(
                                binary_path,
                                binary_name
                            )
                        else:
                            upload = seL4BootHarness.SeL4UploadHarness(
                                board,
                                '/dev/ttyACM0',
                                str(run_dir / 'upload.log'),
                                binary_path,
                                binary_name
                            )
                        upload.run()
                    else:
                        # Subsequent runs: just hardware reboot
                        status(f'{timestamp}: Run {run_num}/{run_count} - rebooting...')
                        print("Hardware reboot for next run", flush=True)
                        board.boot(False)

                    board_state = 'rebooting'

                    # Run seL4 binary
                    status(f'{timestamp}: Run {run_num}/{run_count} - running seL4...')
                    runner = seL4BootHarness.SeL4RunHarness(
                        board,
                        '/dev/ttyACM0',
                        str(run_dir / 'uart-raw.log'),
                        binary_name
                    )
                    runner.run()
                    board_state = 'unknown'

                    # Filter logs
                    with open(run_dir / 'uart-raw.log', "rb") as fin, \
                         open(run_dir / 'sel4.log', "wb") as fout:
                        subprocess.run(
                            [str(SCRIPT_DIR / 'filter_sel4_start.py'), binary_name],
                            stdin=fin,
                            stdout=fout,
                            check=True
                        )

                    # Extract ftrace if present (non-fatal)
                    subprocess.run(
                        [str(SCRIPT_DIR / 'extract_ftrace.py'),
                         str(run_dir / 'sel4.log'),
                         str(run_dir)],
                        check=False
                    )

                    completed_runs += 1
                    print(f"Run {run_num} completed", flush=True)

                except Exception as e:
                    print(f"Run {run_num} failed: {e}", flush=True)
                    (run_dir / 'error.txt').write_text(str(e))

            # Write summary
            summary = {
                'total_runs': run_count,
                'completed_runs': completed_runs,
                'failed_runs': run_count - completed_runs
            }
            (result_dir / 'summary.json').write_text(json.dumps(summary, indent=2))

            # Final recovery
            status(f'{timestamp}: Final recovery...')
            ready = BootHarness.ReadyBootHarness(
                board,
                '/dev/ttyACM0',
                str(result_dir / 'recovery.log'),
                None,
                None
            )
            ready.run()
            board_state = 'stock_linux'

            # Success - move to completed
            processing_file.rename(COMPLETED_DIR / request_file.name)
            print(f"\n=== {timestamp} completed: {completed_runs}/{run_count} runs ===", flush=True)
            print(f"Results: {result_dir}/", flush=True)
            status('Waiting for requests...')

        elif request_type == 'sel4':
            # === seL4 EFI BINARY TEST FLOW (single run) ===
            binary_path = request_data.get('binary_path')
            binary_name = request_data.get('binary_name', 'sel4test.efi')

            if not binary_path:
                raise ValueError("seL4 request missing 'binary_path'")

            print(f"seL4 binary: {binary_path} -> {binary_name}", flush=True)

            # Upload binary - skip boot if already at stock Linux
            status(f'{timestamp}: Uploading seL4 binary...')
            if board_state == 'stock_linux':
                print("Board already at stock Linux, skipping boot", flush=True)
                upload = seL4BootHarness.SeL4UploadOnlyHarness(
                    binary_path,
                    binary_name
                )
            else:
                upload = seL4BootHarness.SeL4UploadHarness(
                    board,
                    '/dev/ttyACM0',
                    str(result_dir / 'upload.log'),
                    binary_path,
                    binary_name
                )
            upload.run()
            board_state = 'rebooting'  # Board is now rebooting

            status(f'{timestamp}: Running seL4 binary...')
            runner = seL4BootHarness.SeL4RunHarness(
                board,
                '/dev/ttyACM0',
                str(result_dir / 'uart-raw.log'),
                binary_name
            )
            runner.run()
            board_state = 'unknown'  # seL4 ran, board state unknown

            # === START ASYNC RECOVERY ===
            # Start recovery in background while we filter logs
            status(f'{timestamp}: Recovery + filtering logs...')
            recovery_exception = [None]

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

            # === FILTER LOGS (parallel with recovery) ===
            with open(result_dir / 'uart-raw.log', "rb") as fin, \
                 open(result_dir / 'sel4.log', "wb") as fout:
                subprocess.run(
                    [str(SCRIPT_DIR / 'filter_sel4_start.py'), binary_name],
                    stdin=fin,
                    stdout=fout,
                    check=True
                )

            # Extract ftrace if present (non-fatal)
            subprocess.run(
                [str(SCRIPT_DIR / 'extract_ftrace.py'),
                 str(result_dir / 'sel4.log'),
                 str(result_dir)],
                check=False
            )

            # Success - move to completed (don't wait for recovery)
            processing_file.rename(COMPLETED_DIR / request_file.name)
            print(f"\n=== {timestamp} completed: seL4 test ===", flush=True)
            print(f"Results: {result_dir}/", flush=True)

            # === WAIT FOR RECOVERY ===
            status(f'{timestamp}: Waiting for recovery...')
            recovery_thread.join()
            if recovery_exception[0]:
                raise recovery_exception[0]

            board_state = 'stock_linux'  # Recovery complete
            status('Waiting for requests...')

        elif request_type == 'vm_minimal':
            # === VM_MINIMAL CAPDL-LOADER TEST FLOW ===
            # Captures both ttyACM0 (seL4/capdl) and ttyACM1 (VM console)
            # Phase 1: Wait up to 60s for capdl-loader to boot (until VM console produces output)
            # Phase 2: Wait for 5 seconds of quiescence on ttyACM1 before stopping
            binary_path = request_data.get('binary_path')
            binary_name = request_data.get('binary_name', 'capdl-vm_minimal.efi')

            if not binary_path:
                raise ValueError("vm_minimal request missing 'binary_path'")

            print(f"vm_minimal binary: {binary_path} -> {binary_name}", flush=True)

            # Upload binary - skip boot if already at stock Linux
            status(f'{timestamp}: Uploading vm_minimal binary...')
            if board_state == 'stock_linux':
                print("Board already at stock Linux, skipping boot", flush=True)
                upload = seL4BootHarness.SeL4UploadOnlyHarness(
                    binary_path,
                    binary_name
                )
            else:
                upload = seL4BootHarness.SeL4UploadHarness(
                    board,
                    '/dev/ttyACM0',
                    str(result_dir / 'upload.log'),
                    binary_path,
                    binary_name
                )
            upload.run()
            board_state = 'rebooting'

            status(f'{timestamp}: Running vm_minimal (120s boot + 5s VM quiescence)...')

            # Run using VMMinimalRunHarness which handles dual UART capture
            # Phase 1: Wait up to 120s for capdl-loader boot (until VM console produces output)
            # Phase 2: Wait for 5s of quiescence on ttyACM1
            runner = seL4BootHarness.VMMinimalRunHarness(
                board,
                '/dev/ttyACM0',
                str(result_dir / 'uart-raw.log'),
                '/dev/ttyACM1',
                str(result_dir / 'vm-uart-raw.log'),
                binary_name,
                sel4_boot_timeout=120,
                vm_quiescence_timeout=90  # Wait for BPMP 60s timeout
            )
            runner.run()
            board_state = 'unknown'

            # === START ASYNC RECOVERY ===
            status(f'{timestamp}: Recovery + filtering logs...')
            recovery_exception = [None]

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

            # === FILTER LOGS (parallel with recovery) ===
            # Filter seL4/capdl-loader log (ttyACM0)
            with open(result_dir / 'uart-raw.log', "rb") as fin, \
                 open(result_dir / 'sel4.log', "wb") as fout:
                subprocess.run(
                    [str(SCRIPT_DIR / 'filter_capdl_start.py')],
                    stdin=fin,
                    stdout=fout,
                    check=True
                )

            # Filter VM console log (ttyACM1)
            if (result_dir / 'vm-uart-raw.log').exists():
                with open(result_dir / 'vm-uart-raw.log', "rb") as fin, \
                     open(result_dir / 'vm.log', "wb") as fout:
                    subprocess.run(
                        [str(SCRIPT_DIR / 'filter_vm_console.py')],
                        stdin=fin,
                        stdout=fout,
                        check=True
                    )

            # Success - move to completed
            processing_file.rename(COMPLETED_DIR / request_file.name)
            print(f"\n=== {timestamp} completed: vm_minimal test ===", flush=True)
            print(f"Results: {result_dir}/", flush=True)
            print(f"  sel4.log: seL4/capdl-loader output", flush=True)
            print(f"  vm.log: VM console output", flush=True)

            # === WAIT FOR RECOVERY ===
            status(f'{timestamp}: Waiting for recovery...')
            recovery_thread.join()
            if recovery_exception[0]:
                raise recovery_exception[0]

            board_state = 'stock_linux'
            status('Waiting for requests...')

        elif is_multi_run and request_type == 'linux':
            # === LINUX MULTI-RUN TEST FLOW ===
            kernel_version = KERNEL_RELEASE_FILE.read_text().strip()
            print(f"Kernel: {kernel_version}", flush=True)
            completed_runs = 0

            for run_num in range(1, run_count + 1):
                run_dir = result_dir / f'run_{run_num}'
                run_dir.mkdir(parents=True, exist_ok=True)

                status(f'{timestamp}: Run {run_num}/{run_count} - starting...')
                print(f"\n--- Run {run_num}/{run_count} ---", flush=True)

                try:
                    # Start hyp logging for this run
                    hyp_stop_evt = threading.Event()
                    hyp_log_thread = threading.Thread(
                        target=BootHarness.log_port,
                        args=('/dev/ttyACM1', str(run_dir / 'uarti.log'), hyp_stop_evt),
                        daemon=True
                    )
                    hyp_log_thread.start()

                    if run_num == 1:
                        # First run: upload kernel
                        status(f'{timestamp}: Run {run_num}/{run_count} - uploading kernel...')
                        seq = BootHarness.UpdateBootHarness(
                            board,
                            '/dev/ttyACM0',
                            str(run_dir / 'kernel-update.log'),
                            None,
                            None,
                            str(KERNEL_IMAGE),
                            kernel_version
                        )
                        seq.run()
                    else:
                        # Subsequent runs: try SSH reboot, fallback to hardware
                        status(f'{timestamp}: Run {run_num}/{run_count} - rebooting...')
                        if try_ssh_reboot():
                            print("SSH reboot initiated", flush=True)
                        else:
                            print("SSH failed, using hardware reboot", flush=True)
                            board.boot(False)

                    # Boot and capture
                    status(f'{timestamp}: Run {run_num}/{run_count} - booting kernel...')
                    seq = BootHarness.PanicBootHarness(
                        board,
                        '/dev/ttyACM0',
                        str(run_dir / 'uart-raw.log'),
                        None,
                        None
                    )
                    seq.run()
                    fault_type = seq.fault_type
                    print(f"Run {run_num} result: {fault_type}", flush=True)

                    # Stop hyp logging
                    hyp_stop_evt.set()
                    hyp_log_thread.join(timeout=1.0)

                    # Filter logs
                    with open(run_dir / 'uart-raw.log', "rb") as fin, \
                         open(run_dir / 'uart.log', "wb") as fout:
                        subprocess.run(
                            SCRIPT_DIR / 'filter_mb1_start.py',
                            stdin=fin, stdout=fout, check=True
                        )

                    with open(run_dir / 'uart.log', "rb") as fin, \
                         open(run_dir / 'kernel.log', "wb") as fout:
                        subprocess.run(
                            SCRIPT_DIR / 'filter_kernel_start.py',
                            stdin=fin, stdout=fout, check=True
                        )

                    with open(run_dir / 'uarti.log', "rb") as fin, \
                         open(run_dir / 'hyp.log', "wb") as fout:
                        subprocess.run(
                            SCRIPT_DIR / 'filter_hyp_output.py',
                            stdin=fin, stdout=fout, check=True
                        )

                    completed_runs += 1

                except Exception as e:
                    print(f"Run {run_num} failed: {e}", flush=True)
                    (run_dir / 'error.txt').write_text(str(e))
                    # Stop hyp logging if still running
                    try:
                        hyp_stop_evt.set()
                        hyp_log_thread.join(timeout=1.0)
                    except:
                        pass

            # Write summary
            summary = {
                'total_runs': run_count,
                'completed_runs': completed_runs,
                'failed_runs': run_count - completed_runs
            }
            (result_dir / 'summary.json').write_text(json.dumps(summary, indent=2))

            # Final recovery
            status(f'{timestamp}: Final recovery...')
            ready = BootHarness.ReadyBootHarness(
                board,
                '/dev/ttyACM0',
                str(result_dir / 'recovery.log'),
                None,
                None
            )
            ready.run()
            board_state = 'stock_linux'

            # Success - move to completed
            processing_file.rename(COMPLETED_DIR / request_file.name)
            print(f"\n=== {timestamp} completed: {completed_runs}/{run_count} runs ===", flush=True)
            print(f"Results: {result_dir}/", flush=True)
            status('Waiting for requests...')

        else:
            # === LINUX KERNEL TEST FLOW (single run) ===
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
                board_state = 'stock_linux'  # Recovery complete
            else:
                # No recovery needed - board stayed in test kernel or success state
                board_state = 'unknown'

            # === ENTER INTERACTIVE MODE IF USER INTERRUPTED ===
            if user_interrupted:
                status('Interactive mode (Ctrl+C to exit)')
                # Reopen serial for interactive use
                ser = serial.Serial('/dev/ttyACM0', 115200, timeout=0.2)
                BootHarness.enter_interactive_mode(ser)
                ser.close()
                board_state = 'unknown'  # User may have done anything

            status('Waiting for requests...')

    except Exception as e:
        # Failure - move to failed directory
        processing_file.rename(FAILED_DIR / request_file.name)

        # Write error to results directory for client retrieval
        if result_dir.exists():
            (result_dir / 'error.txt').write_text(str(e))

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
            board_state = 'unknown'
        else:
            board_state = 'stock_linux'
            status('Waiting for requests...')
