from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from config import get_paths

STARTUP_CLEANUP_POLICY = "clear-pending-and-processing-on-start"


def _utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return path.with_name(f"{path.stem}.startup-cleared-{stamp}{path.suffix}")


def clear_startup_requests(autopilot_dir: str, reason: str = "daemon_start") -> dict:
    """Clear pending/processing requests before a fresh daemon accepts work."""
    paths = get_paths(autopilot_dir)
    for key in ("pending", "processing", "failed", "results", "runtime"):
        paths[key].mkdir(parents=True, exist_ok=True)

    cleared: dict[str, object] = {
        "policy": STARTUP_CLEANUP_POLICY,
        "reason": reason,
        "at": _utc_now(),
        "pending_cleared": [],
        "processing_cleared": [],
    }

    for state in ("pending", "processing"):
        key = f"{state}_cleared"
        state_cleared = cleared[key]
        assert isinstance(state_cleared, list)
        for request_file in sorted(paths[state].glob("*.request")):
            request_id = request_file.stem
            target = _unique_target(paths["failed"] / request_file.name)
            request_file.rename(target)
            state_cleared.append(request_id)

            result_dir = paths["results"] / request_id
            result_dir.mkdir(parents=True, exist_ok=True)
            detail = {
                "request_id": request_id,
                "from_state": state,
                "moved_to": str(target),
                "policy": STARTUP_CLEANUP_POLICY,
                "reason": reason,
                "at": cleared["at"],
            }
            (result_dir / "startup-cleared.json").write_text(
                json.dumps(detail, indent=2) + "\n",
                encoding="utf-8",
            )
            (result_dir / "error.txt").write_text(
                f"Request cleared by Autopilot startup policy: {reason}\n",
                encoding="utf-8",
            )

    report_path = paths["runtime"] / "startup_cleanup.json"
    report_path.write_text(json.dumps(cleared, indent=2) + "\n", encoding="utf-8")
    return cleared


def read_startup_cleanup(autopilot_dir: str) -> dict | None:
    path = get_paths(autopilot_dir)["runtime"] / "startup_cleanup.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "policy": STARTUP_CLEANUP_POLICY,
            "error": "failed_to_parse_startup_cleanup",
        }
