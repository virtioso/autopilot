from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

DEFAULT_AUTOPILOT_DIR = Path("/home/hlyytine/tii-sel4/autopilot")
DEFAULT_TTY0 = "/dev/ttyACM0"
DEFAULT_TTY1 = "/dev/ttyACM1"
DEFAULT_TARGET_IP = "192.168.101.112"
DEFAULT_TARGET_USER = "root"
DEFAULT_BOOT_CONTROL_HOST = "192.168.101.110"


def get_code_root() -> Path:
    override = os.environ.get("AUTOPILOT_CODE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent


def get_autopilot_dir(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env_dir = os.environ.get("AUTOPILOT_DIR")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_AUTOPILOT_DIR


def get_default_ttys() -> tuple[str, str]:
    tty0 = (os.environ.get("AUTOPILOT_TTY0") or "").strip() or DEFAULT_TTY0
    tty1 = (os.environ.get("AUTOPILOT_TTY1") or "").strip() or DEFAULT_TTY1
    return tty0, tty1


def get_paths(autopilot_dir: str | None = None) -> Dict[str, Path]:
    base = get_autopilot_dir(autopilot_dir)
    return {
        "autopilot": base,
        "pending": base / "requests" / "pending",
        "processing": base / "requests" / "processing",
        "completed": base / "requests" / "completed",
        "failed": base / "requests" / "failed",
        "results": base / "results",
        "binaries": base / "binaries",
        "runtime": base / "runtime",
    }


def get_target_ip() -> str:
    return (os.environ.get("AUTOPILOT_TARGET_IP") or "").strip() or DEFAULT_TARGET_IP


def get_target_user() -> str:
    return (os.environ.get("AUTOPILOT_TARGET_USER") or "").strip() or DEFAULT_TARGET_USER


def get_boot_control_host() -> str:
    return (os.environ.get("AUTOPILOT_BOOT_CONTROL_HOST") or "").strip() or DEFAULT_BOOT_CONTROL_HOST
