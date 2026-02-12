#!/usr/bin/env python3
"""
seL4 Autopilot MCP Server

An MCP (Model Context Protocol) server that provides tools for testing and
observing seL4 EFI binaries on NVIDIA Orin AGX hardware.

Tools:
- test_sel4_efi: Submit a binary, wait for completion, return results
- check_sel4_test: Check status of a submitted test
- get_logs: List console logs for a completed test
- list_sel4_tests: List pending/completed/failed tests

Usage:
    # Start the server
    python3 sel4_mcp_server.py

    # Or with uvx (recommended)
    uvx mcp run sel4_mcp_server.py

Configuration for Claude Code (~/.claude/settings.json):
    {
      "mcpServers": {
        "sel4-autopilot": {
          "command": "python3",
          "args": ["/home/hlyytine/autopilot/sel4_mcp_server.py"]
        }
      }
    }
"""

import json
import importlib
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DEFAULT_TTY0, DEFAULT_TTY1, get_default_ttys

# Autopilot process management (hot-reloaded to pick up local fixes without
# restarting the MCP server process).
import autopilot_manager as autopilot_manager_mod
# Import the sel4_client library
# Use script directory to find sel4_client, not hardcoded path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from sel4_client import (
    submit_sel4_efi_test,
    wait_for_result,
    get_status,
    get_logs,
    get_request_info,
    list_pending,
    list_completed,
    list_failed,
    get_paths,
    get_autopilot_status,
    get_test_status,
    cancel_test,
    QueueNotEmptyError,
    get_console_manifest,
    open_console_session,
    read_console_output,
    send_console_command,
    close_console_session,
)

_AUTOPILOT_MANAGER_MTIME_NS = None


def _get_autopilot_manager():
    global autopilot_manager_mod, _AUTOPILOT_MANAGER_MTIME_NS
    mod_path = Path(autopilot_manager_mod.__file__).resolve()
    try:
        mtime_ns = mod_path.stat().st_mtime_ns
    except OSError:
        return autopilot_manager_mod

    if _AUTOPILOT_MANAGER_MTIME_NS is None:
        _AUTOPILOT_MANAGER_MTIME_NS = mtime_ns
        return autopilot_manager_mod

    if mtime_ns != _AUTOPILOT_MANAGER_MTIME_NS:
        autopilot_manager_mod = importlib.reload(autopilot_manager_mod)
        _AUTOPILOT_MANAGER_MTIME_NS = mtime_ns
    return autopilot_manager_mod

# MCP Protocol implementation
# Using stdio transport with JSON-RPC 2.0

def send_response(id: Any, result: Any = None, error: Any = None):
    """Send a JSON-RPC response."""
    response = {"jsonrpc": "2.0", "id": id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()


def send_notification(method: str, params: Any = None):
    """Send a JSON-RPC notification."""
    notification = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        notification["params"] = params
    sys.stdout.write(json.dumps(notification) + "\n")
    sys.stdout.flush()


# Common autopilot_dir property for all tools
AUTOPILOT_DIR_PROP = {
    "type": "string",
    "description": "Override autopilot working directory (default: $AUTOPILOT_DIR or /home/hlyytine/tii-sel4/autopilot)"
}

# Tool definitions
TOOLS = [
    {
        "name": "test_sel4_efi",
        "description": """Test a seL4 EFI binary on NVIDIA Orin AGX hardware.

Submits the binary to the autopilot service and returns immediately.

The binary deployment and boot flow are defined by the selected profile chain
and platform overrides (for example, SSH upload or host-local TFTP copy).
Console output is captured according to the selected profile chain.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "binary_path": {
                    "type": "string",
                    "description": "Absolute path to the seL4 EFI binary (e.g., /home/hlyytine/tii-sel4/orinagx_sel4test/images/sel4test-driver-image-arm-orinagx)"
                },
                "profile": {
                    "type": "string",
                    "description": "Profile name that defines the chain to run",
                    "default": "sel4test"
                },
                "description": {
                    "type": "string",
                    "description": "Optional description of what's being tested",
                    "default": ""
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["binary_path"]
        }
    },
    {
        "name": "check_sel4_test",
        "description": """Check the status of a previously submitted seL4 test.

Returns the current status: pending, processing, completed, failed, or not_found.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "Request ID (timestamp) from a previous test submission"
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["request_id"]
        }
    },
    {
        "name": "list_sel4_tests",
        "description": """List seL4 test requests.

Returns lists of pending, completed, and/or failed test request IDs.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pending": {
                    "type": "boolean",
                    "description": "Include pending tests",
                    "default": True
                },
                "completed": {
                    "type": "boolean",
                    "description": "Include completed tests",
                    "default": False
                },
                "failed": {
                    "type": "boolean",
                    "description": "Include failed tests",
                    "default": False
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            }
        }
    },
    {
        "name": "get_logs",
        "description": """List console logs for a completed test.

Returns paths (and optionally contents) for files under results/<id>/console/.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "Request ID (timestamp) from a previous test submission"
                },
                "include_contents": {
                    "type": "boolean",
                    "description": "If true, include file contents in the response",
                    "default": False
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["request_id"]
        }
    },
    {
        "name": "query_ftrace",
        "description": """Query indexed ftrace data from a test run.

Queries the indexed ftrace file (ftrace.idx) using the fast query tool.
Only works if ftrace was enabled during the test (el2-ftrace mode).

Supports:
- Random access to specific events by index
- Filtering by event type (KERNEL_ENTRY, KERNEL_EXIT, SAFE_PTE, etc.)
- Context around specific events
- Summary statistics""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "Request ID from a previous test submission"
                },
                "event_index": {
                    "type": "integer",
                    "description": "Get specific event by index (O(1) access)"
                },
                "event_type": {
                    "type": "string",
                    "enum": ["KERNEL_ENTRY", "KERNEL_EXIT", "SAFE_PTE", "SYSCALL", "VSPACE", "THREAD", "INIT_PT", "CREATE_OBJ", "PT_MAP", "VMID"],
                    "description": "Filter by event type"
                },
                "context": {
                    "type": "integer",
                    "description": "Show N events before/after event_index",
                    "default": 0
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of events to return",
                    "default": 50
                },
                "summary": {
                    "type": "boolean",
                    "description": "Show summary statistics only",
                    "default": False
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["request_id"]
        }
    },
    {
        "name": "autopilot_status",
        "description": "Get queue summary and current running test status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "autopilot_dir": AUTOPILOT_DIR_PROP
            }
        }
    },
    {
        "name": "get_test_status",
        "description": "Get detailed status for a specific request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "Request ID (timestamp) from a previous test submission"
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["request_id"]
        }
    },
    {
        "name": "wait_for_test",
        "description": "Wait briefly for a test to complete (short-blocking, capped under 60s).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "Request ID (timestamp) from a previous test submission"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Total time to wait in seconds (default: 300)",
                    "default": 300
                },
                "poll_interval": {
                    "type": "integer",
                    "description": "Polling interval in seconds (default: 1)",
                    "default": 1
                },
                "max_block_s": {
                    "type": "integer",
                    "description": "Max time this tool call can block (default: 30)",
                    "default": 30
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["request_id"]
        }
    },
    {
        "name": "cancel_test",
        "description": "Cancel a pending or running test (hard cancel).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "Request ID (timestamp) to cancel"
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["request_id"]
        }
    },
    {
        "name": "list_console_sessions",
        "description": """List interactive console sessions for a request.

Returns session names, IDs, and log paths if available.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "Request ID for a boot_interactive or interactive-enabled test"
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["request_id"]
        }
    },
    {
        "name": "open_console_session",
        "description": """Resolve a console session by name and return its session ID and current offset.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "Request ID containing the console sessions"
                },
                "session_name": {
                    "type": "string",
                    "description": "Session name (e.g., vm0, vm1)"
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["request_id", "session_name"]
        }
    },
    {
        "name": "send_console_command",
        "description": """Send a command to a console session and wait for prompt (optional).""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID from open_console_session"
                },
                "command": {
                    "type": "string",
                    "description": "Command text to send"
                },
                "append_newline": {
                    "type": "boolean",
                    "description": "Append newline to command (default: true)",
                    "default": True
                },
                "wait_for_prompt": {
                    "type": "boolean",
                    "description": "Wait for prompt after sending (default: true)",
                    "default": True
                },
                "prompt_override": {
                    "type": "string",
                    "description": "Optional prompt regex override"
                },
                "timeout_s": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 10)",
                    "default": 10
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["session_id", "command"]
        }
    },
    {
        "name": "read_console_output",
        "description": """Read console output from a session log using byte offsets.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID from open_console_session"
                },
                "offset": {
                    "type": "integer",
                    "description": "Byte offset to read from"
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum bytes to read (default: 4096)",
                    "default": 4096
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["session_id", "offset"]
        }
    },
    {
        "name": "close_console_session",
        "description": """Close an interactive console session.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID from open_console_session"
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["session_id"]
        }
    },
    {
        "name": "autopilot_start",
        "description": """Start the Autopilot daemon (optionally in a tmux session).""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command to start Autopilot (default: python3 /home/hlyytine/autopilot/orin_kernel_autopilot.py)"
                },
                "use_tmux": {
                    "type": "boolean",
                    "description": "Run Autopilot inside tmux for attachable TUI",
                    "default": True
                },
                "tmux_session": {
                    "type": "string",
                    "description": "tmux session name (default: autopilot)"
                },
                "tty0": {
                    "type": "string",
                    "description": "UART device for tty0 (required, e.g. /dev/ttyACM0)",
                    "default": DEFAULT_TTY0
                },
                "tty1": {
                    "type": "string",
                    "description": "UART device for tty1 (required, e.g. /dev/ttyACM1)",
                    "default": DEFAULT_TTY1
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["tty0", "tty1"]
        }
    },
    {
        "name": "autopilot_stop",
        "description": """Stop the Autopilot daemon.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "Send SIGKILL if SIGTERM does not stop the daemon",
                    "default": False
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            }
        }
    },
    {
        "name": "autopilot_restart",
        "description": """Restart the Autopilot daemon.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command to start Autopilot (default: python3 /home/hlyytine/autopilot/orin_kernel_autopilot.py)"
                },
                "use_tmux": {
                    "type": "boolean",
                    "description": "Run Autopilot inside tmux for attachable TUI",
                    "default": True
                },
                "tmux_session": {
                    "type": "string",
                    "description": "tmux session name (default: autopilot)"
                },
                "force": {
                    "type": "boolean",
                    "description": "Send SIGKILL if SIGTERM does not stop the daemon",
                    "default": False
                },
                "tty0": {
                    "type": "string",
                    "description": "UART device for tty0 (required, e.g. /dev/ttyACM0)",
                    "default": DEFAULT_TTY0
                },
                "tty1": {
                    "type": "string",
                    "description": "UART device for tty1 (required, e.g. /dev/ttyACM1)",
                    "default": DEFAULT_TTY1
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["tty0", "tty1"]
        }
    },
    {
        "name": "autopilot_status",
        "description": """Get Autopilot daemon status.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "autopilot_dir": AUTOPILOT_DIR_PROP
            }
        }
    }
]


def handle_tool_call(name: str, arguments: dict) -> dict:
    """Handle a tool call and return the result."""

    # Extract common autopilot_dir parameter
    autopilot_dir = arguments.get("autopilot_dir")
    paths = get_paths(autopilot_dir)
    apm = _get_autopilot_manager()

    if name == "test_sel4_efi":
        binary_path = arguments["binary_path"]
        profile = arguments.get("profile", "sel4test")
        description = arguments.get("description", "")
        chain_path = SCRIPT_DIR / "chains" / f"{profile}.json"

        if not chain_path.exists():
            available_chains = sorted(
                p.stem for p in (SCRIPT_DIR / "chains").glob("*.json")
            )
            payload = {
                "error": "chain_not_found",
                "message": f"chain not found: {profile}",
                "requested_profile": profile,
                "expected_path": str(chain_path),
                "available_chains": available_chains,
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                "isError": True,
            }

        # Ensure Autopilot daemon is running
        default_tty0, default_tty1 = get_default_ttys()
        daemon_status = apm.status_autopilot(autopilot_dir=str(paths["autopilot"]))
        if not daemon_status.get("running", False):
            start_result = apm.start_autopilot(
                autopilot_dir=str(paths["autopilot"]),
                tty0=default_tty0,
                tty1=default_tty1,
            )
            if start_result.get("status") == "error":
                return {
                    "content": [{"type": "text", "text": json.dumps(start_result, indent=2)}],
                    "isError": True
                }
        else:
            # Self-heal stale daemon sessions started without explicit tty/platform metadata.
            running_tty0 = (daemon_status.get("tty0") or "").strip()
            running_tty1 = (daemon_status.get("tty1") or "").strip()
            running_platform = (daemon_status.get("platform") or "").strip()
            expected_platform = (os.environ.get("AUTOPILOT_PLATFORM") or "").strip() or apm.DEFAULT_PLATFORM
            if (
                not running_tty0
                or not running_tty1
                or running_tty0 != default_tty0
                or running_tty1 != default_tty1
                or running_platform != expected_platform
            ):
                restart_result = apm.restart_autopilot(
                    autopilot_dir=str(paths["autopilot"]),
                    use_tmux=True,
                    tty0=default_tty0,
                    tty1=default_tty1,
                )
                if restart_result.get("status") == "error":
                    return {
                        "content": [{"type": "text", "text": json.dumps(restart_result, indent=2)}],
                        "isError": True
                    }

        # Generate timestamped binary name to detect upload failures
        binary_name = f"sel4test-{datetime.now().strftime('%Y%m%d-%H%M%S')}.efi"

        # Determine arm_hyp from build config
        # Check orinagx_sel4test/.config if it exists
        build_config_path = Path("/home/hlyytine/tii-sel4/orinagx_sel4test/.config")
        arm_hyp = True  # Default to hypervisor mode
        if build_config_path.exists():
            config_text = build_config_path.read_text()
            if "KernelArmHypervisorSupport=OFF" in config_text:
                arm_hyp = False

        # Submit the test
        try:
            request_id = submit_sel4_efi_test(
                binary_path=binary_path,
                binary_name=binary_name,
                description=description,
                build_config={'arm_hyp': arm_hyp, 'platform': 'orinagx'},
                profile=profile,
                autopilot_dir=autopilot_dir
            )
        except QueueNotEmptyError as e:
            payload = {
                "error": "queue_not_empty",
                "pending": e.pending,
                "processing": e.processing,
                "hint": "Investigate why a request is pending/processing (use autopilot_status/get_test_status).",
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                "isError": True
            }
        except FileNotFoundError as e:
            return {
                "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                "isError": True
            }

        result_dir = paths['results'] / request_id
        payload = {
            "status": "submitted",
            "request_id": request_id,
            "binary_path": binary_path,
            "binary_name": binary_name,
            "profile": profile,
            "result_dir": str(result_dir),
        }

        return {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": False
        }

    elif name == "check_sel4_test":
        request_id = arguments["request_id"]
        status = get_status(request_id, autopilot_dir=autopilot_dir)

        response_text = f"Status: {status['status']}"
        if "result_dir" in status:
            response_text += f"\nResults directory: {status['result_dir']}"

        # Include request info if available
        info = get_request_info(request_id, autopilot_dir=autopilot_dir)
        if info:
            response_text += f"\nDescription: {info.get('description', 'N/A')}"
            response_text += f"\nOriginal binary: {info.get('original_binary', info.get('binary_path', 'N/A'))}"

        return {
            "content": [{"type": "text", "text": response_text}],
            "isError": False
        }

    elif name == "list_sel4_tests":
        show_pending = arguments.get("pending", True)
        show_completed = arguments.get("completed", False)
        show_failed = arguments.get("failed", False)

        result_lines = []

        if show_pending:
            pending = list_pending(autopilot_dir=autopilot_dir)
            if pending:
                result_lines.append("Pending:")
                result_lines.extend(f"  {ts}" for ts in pending[-10:])  # Last 10

        if show_completed:
            completed = list_completed(autopilot_dir=autopilot_dir)
            if completed:
                result_lines.append("Completed:")
                result_lines.extend(f"  {ts}" for ts in completed[-10:])  # Last 10

        if show_failed:
            failed = list_failed(autopilot_dir=autopilot_dir)
            if failed:
                result_lines.append("Failed:")
                result_lines.extend(f"  {ts}" for ts in failed[-10:])  # Last 10

        if not result_lines:
            result_lines.append("No tests found matching criteria.")

        return {
            "content": [{"type": "text", "text": "\n".join(result_lines)}],
            "isError": False
        }

    elif name == "autopilot_status":
        status = get_autopilot_status(autopilot_dir=autopilot_dir)
        return {
            "content": [{"type": "text", "text": json.dumps(status, indent=2)}],
            "isError": False
        }

    elif name == "get_test_status":
        request_id = arguments["request_id"]
        status = get_test_status(request_id, autopilot_dir=autopilot_dir)
        return {
            "content": [{"type": "text", "text": json.dumps(status, indent=2)}],
            "isError": False
        }

    elif name == "wait_for_test":
        request_id = arguments["request_id"]
        timeout = int(arguments.get("timeout", 300))
        poll_interval = int(arguments.get("poll_interval", 1))
        max_block_s = int(arguments.get("max_block_s", 30))
        start = time.time()
        block_timeout = min(timeout, max_block_s)
        result = wait_for_result(
            request_id,
            timeout=block_timeout,
            poll_interval=poll_interval,
            autopilot_dir=autopilot_dir
        )

        if result["status"] in ("completed", "failed"):
            payload = {
                "status": result["status"],
                "request_id": request_id,
                "result_dir": str(result.get("result_dir")) if result.get("result_dir") else None,
                "elapsed_s": int(time.time() - start),
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                "isError": result["status"] == "failed"
            }

        current = get_status(request_id, autopilot_dir=autopilot_dir)
        remaining = max(timeout - int(time.time() - start), 0)
        payload = {
            "status": current.get("status", "processing"),
            "request_id": request_id,
            "elapsed_s": int(time.time() - start),
            "remaining_s": remaining,
            "next_poll_s": poll_interval,
        }
        return {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": False
        }

    elif name == "cancel_test":
        request_id = arguments["request_id"]
        result = cancel_test(request_id, autopilot_dir=autopilot_dir)
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }

    elif name == "get_logs":
        request_id = arguments["request_id"]
        include_contents = arguments.get("include_contents", False)
        logs = get_logs(request_id, autopilot_dir=autopilot_dir, include_contents=include_contents)
        return {
            "content": [{"type": "text", "text": json.dumps(logs, indent=2)}],
            "isError": False
        }

    elif name == "query_ftrace":
        request_id = arguments["request_id"]
        result_dir = paths['results'] / request_id
        idx_path = result_dir / 'ftrace.idx'

        if not idx_path.exists():
            # Check if ftrace.bin exists but wasn't indexed
            bin_path = result_dir / 'ftrace.bin'
            if bin_path.exists():
                return {
                    "content": [{"type": "text", "text": f"Ftrace data exists but not indexed.\nRun: ftrace-index-rs --binary {bin_path} --meta {result_dir/'ftrace.meta'} --output {idx_path} --build-index"}],
                    "isError": True
                }
            return {
                "content": [{"type": "text", "text": f"No ftrace data found for request {request_id}. Was ftrace enabled (el2-ftrace mode)?"}],
                "isError": True
            }

        # Build query command
        query_tool = Path('/home/hlyytine/tii-sel4/kernel/tools/ftrace_indexed.py')
        if not query_tool.exists():
            return {
                "content": [{"type": "text", "text": f"Query tool not found: {query_tool}"}],
                "isError": True
            }

        cmd = ['python3', str(query_tool), str(idx_path)]

        if arguments.get("summary", False):
            cmd.append('--summary')
        elif arguments.get("event_index") is not None:
            cmd.extend(['--event', str(arguments["event_index"])])
            if arguments.get("context", 0) > 0:
                cmd.extend(['--context', str(arguments["context"])])
        elif arguments.get("event_type"):
            cmd.extend(['--type', arguments["event_type"]])
            cmd.extend(['--limit', str(arguments.get("limit", 50))])
        else:
            # Default: show summary
            cmd.append('--summary')

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = result.stdout
            if result.returncode != 0:
                output = f"Error: {result.stderr}\n{result.stdout}"
            return {
                "content": [{"type": "text", "text": output}],
                "isError": result.returncode != 0
            }
        except subprocess.TimeoutExpired:
            return {
                "content": [{"type": "text", "text": "Query timed out after 30 seconds"}],
                "isError": True
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Query failed: {str(e)}"}],
                "isError": True
            }

    elif name == "list_console_sessions":
        request_id = arguments["request_id"]
        manifest = get_console_manifest(request_id, autopilot_dir=autopilot_dir)
        if not manifest:
            return {
                "content": [{"type": "text", "text": "No console sessions manifest found."}],
                "isError": True
            }
        sessions = manifest.get("sessions", [])
        lines = [f"Status: {manifest.get('status', 'unknown')}"]
        for sess in sessions:
            lines.append(f"- {sess.get('name')}: {sess.get('session_id')} ({sess.get('port')})")
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "isError": False
        }

    elif name == "open_console_session":
        request_id = arguments["request_id"]
        session_name = arguments["session_name"]
        try:
            info = open_console_session(request_id, session_name, autopilot_dir=autopilot_dir)
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True
            }
        return {
            "content": [{"type": "text", "text": json.dumps(info, indent=2)}],
            "isError": False
        }

    elif name == "send_console_command":
        session_id = arguments["session_id"]
        command = arguments["command"]
        append_newline = arguments.get("append_newline", True)
        wait_for_prompt = arguments.get("wait_for_prompt", True)
        prompt_override = arguments.get("prompt_override")
        timeout_s = arguments.get("timeout_s", 10)
        result = send_console_command(
            session_id=session_id,
            command=command,
            append_newline=append_newline,
            wait_for_prompt=wait_for_prompt,
            prompt_override=prompt_override,
            timeout_s=timeout_s,
            autopilot_dir=autopilot_dir
        )
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": "error" in result
        }

    elif name == "read_console_output":
        session_id = arguments["session_id"]
        offset = arguments["offset"]
        max_bytes = arguments.get("max_bytes", 4096)
        result = read_console_output(
            session_id=session_id,
            offset=offset,
            max_bytes=max_bytes,
            autopilot_dir=autopilot_dir
        )
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }

    elif name == "close_console_session":
        session_id = arguments["session_id"]
        close_console_session(session_id, autopilot_dir=autopilot_dir)
        return {
            "content": [{"type": "text", "text": f"Closed session {session_id}"}],
            "isError": False
        }
    elif name == "autopilot_start":
        command = arguments.get("command")
        use_tmux = arguments.get("use_tmux", True)
        tmux_session = arguments.get("tmux_session")
        tty0 = arguments.get("tty0")
        tty1 = arguments.get("tty1")
        if not (tty0 and str(tty0).strip()):
            return {
                "content": [{"type": "text", "text": "tty0 is required (example: /dev/ttyACM0)"}],
                "isError": True
            }
        if not (tty1 and str(tty1).strip()):
            return {
                "content": [{"type": "text", "text": "tty1 is required (example: /dev/ttyACM1)"}],
                "isError": True
            }
        result = apm.start_autopilot(
            autopilot_dir=str(paths["autopilot"]),
            command=command,
            use_tmux=use_tmux,
            tmux_session=tmux_session,
            tty0=str(tty0),
            tty1=str(tty1),
        )
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": result.get("status") == "error"
        }
    elif name == "autopilot_stop":
        force = arguments.get("force", False)
        result = apm.stop_autopilot(
            autopilot_dir=str(paths["autopilot"]),
            force=force,
        )
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }
    elif name == "autopilot_restart":
        command = arguments.get("command")
        use_tmux = arguments.get("use_tmux", True)
        tmux_session = arguments.get("tmux_session")
        force = arguments.get("force", False)
        tty0 = arguments.get("tty0")
        tty1 = arguments.get("tty1")
        if not (tty0 and str(tty0).strip()):
            return {
                "content": [{"type": "text", "text": "tty0 is required (example: /dev/ttyACM0)"}],
                "isError": True
            }
        if not (tty1 and str(tty1).strip()):
            return {
                "content": [{"type": "text", "text": "tty1 is required (example: /dev/ttyACM1)"}],
                "isError": True
            }
        result = apm.restart_autopilot(
            autopilot_dir=str(paths["autopilot"]),
            command=command,
            use_tmux=use_tmux,
            tmux_session=tmux_session,
            force=force,
            tty0=str(tty0),
            tty1=str(tty1),
        )
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": result.get("status") == "error"
        }
    elif name == "autopilot_status":
        result = apm.status_autopilot(
            autopilot_dir=str(paths["autopilot"]),
        )
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": False
        }

    else:
        return {
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
            "isError": True
        }


def handle_request(request: dict) -> None:
    """Handle an incoming JSON-RPC request."""
    method = request.get("method")
    id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        send_response(id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {}
            },
            "serverInfo": {
                "name": "sel4-autopilot",
                "version": "1.0.0"
            }
        })

    elif method == "notifications/initialized":
        # Client acknowledged initialization, nothing to do
        pass

    elif method == "tools/list":
        send_response(id, {"tools": TOOLS})

    elif method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        result = handle_tool_call(tool_name, tool_args)
        send_response(id, result)

    elif method == "ping":
        send_response(id, {})

    else:
        # Unknown method
        if id is not None:
            send_response(id, error={
                "code": -32601,
                "message": f"Method not found: {method}"
            })


def main():
    """Main loop - read JSON-RPC requests from stdin, write responses to stdout."""
    # Log to stderr so it doesn't interfere with protocol
    sys.stderr.write("seL4 Autopilot MCP Server starting...\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
            handle_request(request)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"JSON parse error: {e}\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"Error handling request: {e}\n")
            sys.stderr.flush()
            # Try to send error response if we have an ID
            try:
                if 'id' in request:
                    send_response(request['id'], error={
                        "code": -32603,
                        "message": str(e)
                    })
            except:
                pass


if __name__ == "__main__":
    main()
