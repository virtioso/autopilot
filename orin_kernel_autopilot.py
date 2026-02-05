#! /usr/bin/env python3

import json
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

import BoardControl
from chain_runtime import ChainRecorder, ChainRunner, SourceManager, TUIManager
from console_sessions import ConsoleManager

SCRIPT_DIR = Path(__file__).resolve().parent
AUTOPILOT_DIR = Path(os.environ.get("AUTOPILOT_DIR", str(SCRIPT_DIR)))

WORKSPACE = Path(os.environ.get("WORKSPACE", "/home/hlyytine/pkvm"))
KERNEL_DIR = WORKSPACE / "Linux_for_Tegra/source/kernel/linux"
KERNEL_IMAGE = KERNEL_DIR / "arch/arm64/boot/Image"
KERNEL_RELEASE_FILE = KERNEL_DIR / "include/config/kernel.release"

TARGET_IP = os.environ.get("AUTOPILOT_TARGET_IP", "192.168.101.112")
DEFAULT_TTY0 = os.environ.get("AUTOPILOT_TTY0", "/dev/ttyACM0")
DEFAULT_TTY1 = os.environ.get("AUTOPILOT_TTY1", "/dev/ttyACM1")

PENDING_DIR = AUTOPILOT_DIR / "requests" / "pending"
PROCESSING_DIR = AUTOPILOT_DIR / "requests" / "processing"
COMPLETED_DIR = AUTOPILOT_DIR / "requests" / "completed"
FAILED_DIR = AUTOPILOT_DIR / "requests" / "failed"
RESULTS_DIR = AUTOPILOT_DIR / "results"
PROFILES_DIR = AUTOPILOT_DIR / "profiles"
RUNTIME_DIR = AUTOPILOT_DIR / "runtime"


def ensure_dirs() -> None:
    for d in [PENDING_DIR, PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR, RESULTS_DIR, PROFILES_DIR, RUNTIME_DIR]:
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


def load_profile(profile_name: str) -> dict:
    profile_path = PROFILES_DIR / f"{profile_name}.json"
    if not profile_path.exists():
        raise ValueError(f"profile not found: {profile_name}")
    return json.loads(profile_path.read_text())


def run_chain(chain: dict, ctx: dict, recorder: ChainRecorder) -> str:
    runner = ChainRunner(chain, ctx, recorder)
    return runner.run()


def poll_idle_events(event_queue: queue.Queue, tui: TUIManager, exit_flag: threading.Event) -> None:
    while True:
        try:
            event = event_queue.get_nowait()
        except queue.Empty:
            break
        if event.kind == "exit":
            exit_flag.set()
        elif event.kind in ("switch_window", "list_windows"):
            tui.handle_event(event)


def main() -> None:
    ensure_dirs()

    board = BoardControl.BoardControlLocal()
    console_manager = ConsoleManager(AUTOPILOT_DIR)

    event_queue: queue.Queue = queue.Queue()
    exit_flag = threading.Event()
    cancel_flag = threading.Event()

    tui = TUIManager()
    tui.start(event_queue)

    source_manager = SourceManager(RESULTS_DIR, tui=tui)
    tui.set_input_handler(lambda source, ch: source_manager.get(source).write(ch) if source_manager.get(source) else None)

    print(f"Watching: {PENDING_DIR}", flush=True)
    print(f"Results:  {RESULTS_DIR}", flush=True)

    # Startup chain (optional)
    startup_profile = PROFILES_DIR / "startup.json"
    if startup_profile.exists():
        startup_chain = json.loads(startup_profile.read_text()).get("chain")
        if startup_chain:
            startup_dir = RUNTIME_DIR / "startup"
            startup_dir.mkdir(parents=True, exist_ok=True)
            source_manager.set_result_dir(startup_dir)
            ctx = {
                "board": board,
                "sources": source_manager,
                "tui": tui,
                "event_queue": event_queue,
                "cancel_flag": cancel_flag,
                "result_dir": startup_dir,
                "request_id": "startup",
                "request": {},
                "profile": "startup",
                "chain_name": "startup",
                "request_start": time.time(),
                "target_ip": TARGET_IP,
                "kernel_image": KERNEL_IMAGE,
                "kernel_release": KERNEL_RELEASE_FILE.read_text().strip() if KERNEL_RELEASE_FILE.exists() else "unknown",
                "forks": {},
                "fork_recorders": {},
                "console_manager": console_manager,
                "abort_recovery_chain": "recovery_boot",
                "default_ttys": {"tty0": DEFAULT_TTY0, "tty1": DEFAULT_TTY1},
                "exit_flag": exit_flag,
            }
            recorder = ChainRecorder(startup_dir)
            try:
                run_chain(startup_chain, ctx, recorder)
            except Exception as exc:
                print(f"Startup chain failed: {exc}", flush=True)

    # Main loop
    while not exit_flag.is_set():
        poll_idle_events(event_queue, tui, exit_flag)
        if exit_flag.is_set():
            break

        requests = sorted(PENDING_DIR.glob("*.request"))
        if not requests:
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
            profile = load_profile(profile_name)
        except Exception as exc:
            (result_dir / "error.txt").write_text(str(exc))
            processing_file.rename(FAILED_DIR / request_file.name)
            print(f"ERROR: {exc}", flush=True)
            continue

        chain = profile.get("chain")
        if not chain:
            err = f"profile {profile_name} missing chain"
            (result_dir / "error.txt").write_text(err)
            processing_file.rename(FAILED_DIR / request_file.name)
            print(f"ERROR: {err}", flush=True)
            continue

        ctx = {
            "board": board,
            "sources": source_manager,
            "tui": tui,
            "event_queue": event_queue,
            "cancel_flag": threading.Event(),
            "result_dir": result_dir,
            "request_id": timestamp,
            "request": request_data,
            "profile": profile_name,
            "chain_name": profile_name,
            "request_start": time.time(),
            "target_ip": TARGET_IP,
            "kernel_image": KERNEL_IMAGE,
            "kernel_release": KERNEL_RELEASE_FILE.read_text().strip() if KERNEL_RELEASE_FILE.exists() else "unknown",
            "forks": {},
            "fork_recorders": {},
            "console_manager": console_manager,
            "abort_recovery_chain": "recovery_boot",
            "default_ttys": {"tty0": DEFAULT_TTY0, "tty1": DEFAULT_TTY1},
            "exit_flag": exit_flag,
        }
        recorder = ChainRecorder(result_dir)

        status = "failed"
        try:
            status = run_chain(chain, ctx, recorder)
        except Exception as exc:
            (result_dir / "error.txt").write_text(str(exc))
            status = "failed"

        if status == "pass":
            processing_file.rename(COMPLETED_DIR / request_file.name)
        else:
            processing_file.rename(FAILED_DIR / request_file.name)

        print(f"=== {timestamp} completed: {status} ===", flush=True)

    tui.stop()
    cleanup()


if __name__ == "__main__":
    main()
