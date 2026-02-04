#!/usr/bin/env python3
"""
seL4 Autopilot MCP Server

An MCP (Model Context Protocol) server that provides tools for building and
testing seL4 EFI binaries on NVIDIA Orin AGX hardware.

Tools:
- build_sel4test: Build sel4test for Orin AGX (clean build in Docker)
- test_sel4_binary: Submit a binary, wait for completion, return results
- test_sel4_multi_run: Run a binary N times for stress testing
- check_sel4_test: Check status of a submitted test
- get_sel4_log: Get the console output of a completed test
- get_multi_run_logs: Get logs from a multi-run test
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
          "args": ["/home/hlyytine/pkvm/autopilot/sel4_mcp_server.py"]
        }
      }
    }
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Import the sel4_client library
# Use script directory to find sel4_client, not hardcoded path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from sel4_client import (
    submit_sel4_test,
    submit_multi_run_test,
    wait_for_result,
    get_status,
    get_sel4_log,
    get_raw_log,
    get_request_info,
    get_multi_run_logs,
    list_pending,
    list_completed,
    list_failed,
    get_paths,
    get_console_manifest,
    open_console_session,
    read_console_output,
    send_console_command,
    close_console_session,
)

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
    "description": "Override autopilot working directory (default: $AUTOPILOT_DIR or /home/hlyytine/pkvm/autopilot)"
}

# Tool definitions
TOOLS = [
    {
        "name": "test_sel4_binary",
        "description": """Test a seL4 EFI binary on NVIDIA Orin AGX hardware.

Submits the binary to the autopilot service, waits for test completion,
and returns the console output. The test typically takes 1-3 minutes.

The binary is uploaded to the target via SSH, then booted via UEFI.
Console output is captured until quiescent (30 seconds no output).

Use this for testing seL4 kernel/elfloader changes on real hardware.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "binary_path": {
                    "type": "string",
                    "description": "Absolute path to the seL4 EFI binary (e.g., /home/hlyytine/tii-sel4/orinagx_sel4test/images/sel4test-driver-image-arm-orinagx)"
                },
                "description": {
                    "type": "string",
                    "description": "Optional description of what's being tested",
                    "default": ""
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum time to wait for test in seconds (default: 300)",
                    "default": 300
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
        "name": "get_sel4_log",
        "description": """Get the console output from a completed seL4 test.

Returns the filtered log (bootloader/UEFI stripped) showing only seL4 output.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "Request ID (timestamp) from a previous test submission"
                },
                "raw": {
                    "type": "boolean",
                    "description": "If true, return raw UART output including bootloader",
                    "default": False
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
        "name": "test_sel4_multi_run",
        "description": """Test a seL4 or Linux binary with multiple boot iterations.

Uploads the binary once, then reboots the board N times to collect N boot logs.
For Linux: uses SSH reboot if board boots successfully, else hardware reboot.
For seL4: always uses hardware reboot after first run.

Use this for stress testing, detecting intermittent failures, or collecting boot timing statistics.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "binary_path": {
                    "type": "string",
                    "description": "Absolute path to the EFI binary (seL4) or kernel image (Linux)"
                },
                "run_count": {
                    "type": "integer",
                    "description": "Number of boot iterations (default: 5)",
                    "default": 5
                },
                "test_type": {
                    "type": "string",
                    "enum": ["sel4", "linux"],
                    "description": "Test type: 'sel4' for seL4 binaries, 'linux' for kernel tests",
                    "default": "sel4"
                },
                "description": {
                    "type": "string",
                    "description": "Optional description of what's being tested",
                    "default": ""
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum time per run in seconds (default: 300)",
                    "default": 300
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["binary_path"]
        }
    },
    {
        "name": "get_multi_run_logs",
        "description": """Get all logs from a multi-run test.

Returns logs for each boot iteration, including any errors.
Also includes the summary with completed/failed run counts.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "Request ID from a multi-run test submission"
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["request_id"]
        }
    },
    {
        "name": "build_sel4test",
        "description": """Build sel4test for the Orin AGX platform.

Performs a clean build of sel4test inside Docker. This ALWAYS removes any
existing build directory, configures for the specified mode, and runs the
full build.

The build typically takes 3-5 minutes.

Returns the path to the built binary on success.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["el1", "el2", "el2-ras", "el2-ftrace", "el2-ftrace-nocache"],
                    "description": "Kernel mode: 'el2' for hypervisor mode (default), 'el1' for no hypervisor, 'el2-ras' for RAS error logging without function tracing (lower overhead), 'el2-ftrace' for full function tracing, 'el2-ftrace-nocache' for ftrace with data cache disabled",
                    "default": "el2"
                }
            }
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
        "name": "build_vm_minimal",
        "description": """Build vm_minimal CAmkES application for the Orin AGX platform.

Performs a clean build of vm_minimal inside Docker. This ALWAYS removes any
existing build directory, configures for the specified mode, and runs the
full build.

The build typically takes 5-10 minutes.

Returns the path to the built capdl-loader binary on success.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["el1", "el2"],
                    "description": "Kernel mode: 'el2' for hypervisor mode (default), 'el1' for no hypervisor",
                    "default": "el2"
                }
            }
        }
    },
    {
        "name": "test_vm_minimal",
        "description": """Test a vm_minimal capdl-loader binary on NVIDIA Orin AGX hardware.

Submits the binary to the autopilot service, waits for test completion,
and returns paths to the logs. Captures both seL4/capdl-loader output (ttyACM0)
and VM console output (ttyACM1).

The test waits for 5 seconds of no output on the VM console (ttyACM1)
before considering the test complete.

No success/failure criteria yet - just captures logs for analysis.""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "binary_path": {
                    "type": "string",
                    "description": "Absolute path to the capdl-loader EFI binary (e.g., /home/hlyytine/tii-sel4/orinagx_vm_minimal/images/capdl-loader-image-arm-orinagx)"
                },
                "description": {
                    "type": "string",
                    "description": "Optional description of what's being tested",
                    "default": ""
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum time to wait for test in seconds (default: 300)",
                    "default": 300
                },
                "autopilot_dir": AUTOPILOT_DIR_PROP
            },
            "required": ["binary_path"]
        }
    },
    {
        "name": "get_vm_logs",
        "description": """Get logs from a vm_minimal test.

Returns paths to both the seL4/capdl-loader log (sel4.log) and
the VM console log (vm.log).""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "Request ID from a vm_minimal test submission"
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
    }
]


def handle_tool_call(name: str, arguments: dict) -> dict:
    """Handle a tool call and return the result."""

    # Extract common autopilot_dir parameter
    autopilot_dir = arguments.get("autopilot_dir")
    paths = get_paths(autopilot_dir)

    if name == "test_sel4_binary":
        binary_path = arguments["binary_path"]
        description = arguments.get("description", "")
        timeout = arguments.get("timeout", 300)

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
            request_id = submit_sel4_test(
                binary_path=binary_path,
                binary_name=binary_name,
                description=description,
                build_config={'arm_hyp': arm_hyp, 'platform': 'orinagx'},
                autopilot_dir=autopilot_dir
            )
        except FileNotFoundError as e:
            return {
                "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                "isError": True
            }

        # Wait for completion
        result = wait_for_result(request_id, timeout=timeout, autopilot_dir=autopilot_dir)

        if result["status"] == "timeout":
            return {
                "content": [{"type": "text", "text": f"Test timed out after {timeout}s. Request ID: {request_id}\nYou can check status later with check_sel4_test."}],
                "isError": False
            }

        # Return paths instead of full log content (logs can be huge with ftrace)
        result_dir = paths['results'] / request_id
        sel4_log_path = result_dir / 'sel4.log'
        uart_raw_path = result_dir / 'uart-raw.log'
        ftrace_idx_path = result_dir / 'ftrace.idx'

        # Check for error file if test failed
        error_msg = ""
        if result["status"] == "failed":
            error_file = result_dir / 'error.txt'
            if error_file.exists():
                error_msg = f"\nError: {error_file.read_text()}"

        # Check for ftrace data
        ftrace_msg = ""
        if ftrace_idx_path.exists():
            ftrace_meta_path = result_dir / 'ftrace.idx.meta'
            if ftrace_meta_path.exists():
                try:
                    meta = json.loads(ftrace_meta_path.read_text())
                    record_count = meta.get('record_count', 0)
                    ftrace_msg = f"\nFtrace: {record_count:,} events indexed - use query_ftrace tool to analyze"
                except:
                    ftrace_msg = "\nFtrace: indexed data available - use query_ftrace tool"
        elif (result_dir / 'ftrace.bin').exists():
            ftrace_msg = "\nFtrace: raw data available (not indexed)"

        response_text = f"""Test {result['status']}
Request ID: {request_id}
Binary: {binary_path}{error_msg}{ftrace_msg}

Results directory: {result_dir}
seL4 log: {sel4_log_path}
Raw UART log: {uart_raw_path}

Use get_sel4_log tool or read the files directly to view output."""

        return {
            "content": [{"type": "text", "text": response_text}],
            "isError": result["status"] == "failed"
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

    elif name == "get_sel4_log":
        request_id = arguments["request_id"]
        raw = arguments.get("raw", False)

        result_dir = paths['results'] / request_id
        if raw:
            log_path = result_dir / 'uart-raw.log'
        else:
            log_path = result_dir / 'sel4.log'

        if not log_path.exists():
            return {
                "content": [{"type": "text", "text": f"No log found at {log_path}"}],
                "isError": True
            }

        return {
            "content": [{"type": "text", "text": f"Log file: {log_path}\n\nUse Read tool to view contents."}],
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

    elif name == "test_sel4_multi_run":
        binary_path = arguments["binary_path"]
        run_count = arguments.get("run_count", 5)
        test_type = arguments.get("test_type", "sel4")
        description = arguments.get("description", "")
        timeout_per_run = arguments.get("timeout", 300)

        # Generate timestamped binary name to detect upload failures
        binary_name = f"sel4test-{datetime.now().strftime('%Y%m%d-%H%M%S')}.efi"

        # Determine arm_hyp from build config (for seL4 tests)
        build_config = None
        if test_type == "sel4":
            build_config_path = Path("/home/hlyytine/tii-sel4/orinagx_sel4test/.config")
            arm_hyp = True  # Default to hypervisor mode
            if build_config_path.exists():
                config_text = build_config_path.read_text()
                if "KernelArmHypervisorSupport=OFF" in config_text:
                    arm_hyp = False
            build_config = {'arm_hyp': arm_hyp, 'platform': 'orinagx'}

        # Submit the multi-run test
        try:
            request_id = submit_multi_run_test(
                binary_path=binary_path,
                run_count=run_count,
                binary_name=binary_name,
                test_type=test_type,
                description=description,
                build_config=build_config,
                autopilot_dir=autopilot_dir
            )
        except FileNotFoundError as e:
            return {
                "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                "isError": True
            }

        # Wait for completion with scaled timeout
        total_timeout = timeout_per_run * run_count
        result = wait_for_result(request_id, timeout=total_timeout, autopilot_dir=autopilot_dir)

        if result["status"] == "timeout":
            return {
                "content": [{"type": "text", "text": f"Multi-run test timed out after {total_timeout}s. Request ID: {request_id}\nYou can check status later with check_sel4_test."}],
                "isError": False
            }

        # Return paths instead of full log content (logs can be huge with ftrace)
        result_dir = paths['results'] / request_id

        # Get summary info
        logs = get_multi_run_logs(request_id, autopilot_dir=autopilot_dir)
        summary_text = ""
        if logs['summary']:
            s = logs['summary']
            summary_text = f"Summary: {s.get('completed_runs', '?')}/{s.get('total_runs', '?')} runs completed"

        # Check for top-level error file if test failed
        error_msg = ""
        if result["status"] == "failed":
            error_file = result_dir / 'error.txt'
            if error_file.exists():
                error_msg = f"\nError: {error_file.read_text()}"

        response_text = f"""Multi-run test {result['status']}
Request ID: {request_id}
Binary: {binary_path}
Run count: {run_count}
{summary_text}{error_msg}

Results directory: {result_dir}
Individual run logs: {result_dir}/run_N/sel4.log

Use get_multi_run_logs tool or read the files directly to view output."""

        return {
            "content": [{"type": "text", "text": response_text}],
            "isError": result["status"] == "failed"
        }

    elif name == "get_multi_run_logs":
        request_id = arguments["request_id"]
        result_dir = paths['results'] / request_id

        if not result_dir.exists():
            return {
                "content": [{"type": "text", "text": f"No results found for request {request_id}"}],
                "isError": True
            }

        # Get summary info
        logs = get_multi_run_logs(request_id, autopilot_dir=autopilot_dir)
        response_text = f"Results directory: {result_dir}\n\n"

        if logs['summary']:
            s = logs['summary']
            response_text += f"Summary: {s.get('completed_runs', '?')}/{s.get('total_runs', '?')} runs completed, {s.get('failed_runs', '?')} failed\n\n"

        response_text += "Run logs:\n"
        for run in logs['runs']:
            run_num = run['run_number']
            run_log = result_dir / f'run_{run_num}' / 'sel4.log'
            if 'error' in run:
                response_text += f"  Run {run_num}: Error - {run['error']}\n"
            else:
                response_text += f"  Run {run_num}: {run_log}\n"

        response_text += "\nUse Read tool to view log contents."

        return {
            "content": [{"type": "text", "text": response_text}],
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

    elif name == "build_sel4test":
        mode = arguments.get("mode", "el2")

        # Configuration
        workspace_root = Path("/home/hlyytine/tii-sel4")
        build_dir = workspace_root / "orinagx_sel4test"
        binary_path = build_dir / "images" / "sel4test-driver-image-arm-orinagx"

        # Determine defconfig based on mode
        if mode == "el2":
            defconfig = "orinagx_defconfig"
        elif mode == "el2-ras":
            defconfig = "orinagx_ras_defconfig"
        elif mode == "el2-ftrace":
            defconfig = "orinagx_ftrace_defconfig"
        elif mode == "el2-ftrace-nocache":
            defconfig = "orinagx_ftrace_nocache_defconfig"
        else:
            defconfig = "orinagx_nohyp_defconfig"

        build_log = []
        build_log.append(f"Building sel4test in {mode} mode...")

        try:
            # Step 1: Always remove existing build directory for clean build
            if build_dir.exists():
                build_log.append(f"Removing existing build directory: {build_dir}")
                shutil.rmtree(build_dir)

            # Step 2: Run defconfig
            build_log.append(f"Running: make {defconfig}")
            result = subprocess.run(
                ["make", defconfig],
                cwd=str(workspace_root),
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode != 0:
                return {
                    "content": [{"type": "text", "text": f"Defconfig failed:\n{result.stderr}\n{result.stdout}"}],
                    "isError": True
                }
            build_log.append("Defconfig completed successfully")

            # Step 3: Build sel4test
            build_log.append("Running: make sel4test (this may take several minutes)")
            result = subprocess.run(
                ["make", "sel4test"],
                cwd=str(workspace_root),
                capture_output=True,
                text=True,
                timeout=900  # 15 minute timeout
            )

            # Check for build errors
            if result.returncode != 0:
                # Include last 50 lines of output for debugging
                stderr_lines = result.stderr.strip().split('\n')[-50:]
                stdout_lines = result.stdout.strip().split('\n')[-50:]
                return {
                    "content": [{"type": "text", "text": f"Build failed (exit code {result.returncode}):\n\nstderr (last 50 lines):\n" + "\n".join(stderr_lines) + "\n\nstdout (last 50 lines):\n" + "\n".join(stdout_lines)}],
                    "isError": True
                }

            # Step 4: Verify binary was created
            if not binary_path.exists():
                return {
                    "content": [{"type": "text", "text": f"Build appeared to succeed but binary not found at: {binary_path}"}],
                    "isError": True
                }

            # Get binary timestamp
            mtime = datetime.fromtimestamp(binary_path.stat().st_mtime)
            build_time = mtime.strftime("%Y-%m-%d %H:%M:%S")

            build_log.append(f"Build completed successfully!")
            build_log.append(f"Binary: {binary_path}")
            build_log.append(f"Build time: {build_time}")

            # Return success with structured result
            result_json = {
                "success": True,
                "binary_path": str(binary_path),
                "mode": mode,
                "build_time": build_time
            }

            response_text = "\n".join(build_log) + f"\n\nResult:\n{json.dumps(result_json, indent=2)}"

            return {
                "content": [{"type": "text", "text": response_text}],
                "isError": False
            }

        except subprocess.TimeoutExpired:
            return {
                "content": [{"type": "text", "text": "Build timed out after 15 minutes"}],
                "isError": True
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Build failed with exception: {str(e)}"}],
                "isError": True
            }

    elif name == "build_vm_minimal":
        mode = arguments.get("mode", "el2")

        # Configuration
        workspace_root = Path("/home/hlyytine/tii-sel4")
        build_dir = workspace_root / "orinagx_vm_minimal"
        binary_path = build_dir / "images" / "capdl-loader-image-arm-orinagx"

        # Determine defconfig based on mode
        if mode == "el2":
            defconfig = "orinagx_defconfig"
        else:
            defconfig = "orinagx_nohyp_defconfig"

        build_log = []
        build_log.append(f"Building vm_minimal in {mode} mode...")

        try:
            # Step 1: Always remove existing build directory for clean build
            if build_dir.exists():
                build_log.append(f"Removing existing build directory: {build_dir}")
                shutil.rmtree(build_dir)

            # Step 2: Run defconfig
            build_log.append(f"Running: make {defconfig}")
            result = subprocess.run(
                ["make", defconfig],
                cwd=str(workspace_root),
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode != 0:
                return {
                    "content": [{"type": "text", "text": f"Defconfig failed:\n{result.stderr}\n{result.stdout}"}],
                    "isError": True
                }
            build_log.append("Defconfig completed successfully")

            # Step 3: Build vm_minimal
            build_log.append("Running: make vm_minimal (this may take several minutes)")
            result = subprocess.run(
                ["make", "vm_minimal"],
                cwd=str(workspace_root),
                capture_output=True,
                text=True,
                timeout=1800  # 30 minute timeout (CAmkES builds take longer)
            )

            # Check for build errors
            if result.returncode != 0:
                # Include last 50 lines of output for debugging
                stderr_lines = result.stderr.strip().split('\n')[-50:]
                stdout_lines = result.stdout.strip().split('\n')[-50:]
                return {
                    "content": [{"type": "text", "text": f"Build failed (exit code {result.returncode}):\n\nstderr (last 50 lines):\n" + "\n".join(stderr_lines) + "\n\nstdout (last 50 lines):\n" + "\n".join(stdout_lines)}],
                    "isError": True
                }

            # Step 4: Verify binary was created
            if not binary_path.exists():
                return {
                    "content": [{"type": "text", "text": f"Build appeared to succeed but binary not found at: {binary_path}"}],
                    "isError": True
                }

            # Get binary timestamp
            mtime = datetime.fromtimestamp(binary_path.stat().st_mtime)
            build_time = mtime.strftime("%Y-%m-%d %H:%M:%S")

            build_log.append(f"Build completed successfully!")
            build_log.append(f"Binary: {binary_path}")
            build_log.append(f"Build time: {build_time}")

            # Return success with structured result
            result_json = {
                "success": True,
                "binary_path": str(binary_path),
                "mode": mode,
                "build_time": build_time
            }

            response_text = "\n".join(build_log) + f"\n\nResult:\n{json.dumps(result_json, indent=2)}"

            return {
                "content": [{"type": "text", "text": response_text}],
                "isError": False
            }

        except subprocess.TimeoutExpired:
            return {
                "content": [{"type": "text", "text": "Build timed out after 30 minutes"}],
                "isError": True
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Build failed with exception: {str(e)}"}],
                "isError": True
            }

    elif name == "test_vm_minimal":
        binary_path = arguments["binary_path"]
        description = arguments.get("description", "")
        timeout = arguments.get("timeout", 300)

        # Generate timestamped binary name
        binary_name = f"capdl-vm_minimal-{datetime.now().strftime('%Y%m%d-%H%M%S')}.efi"

        # Determine arm_hyp from build config
        build_config_path = Path("/home/hlyytine/tii-sel4/orinagx_vm_minimal/.config")
        arm_hyp = True  # Default to hypervisor mode
        if build_config_path.exists():
            config_text = build_config_path.read_text()
            if "KernelArmHypervisorSupport=OFF" in config_text:
                arm_hyp = False

        # Submit the test using vm_minimal type
        try:
            from sel4_client import submit_vm_minimal_test
            request_id = submit_vm_minimal_test(
                binary_path=binary_path,
                binary_name=binary_name,
                description=description,
                build_config={'arm_hyp': arm_hyp, 'platform': 'orinagx'},
                autopilot_dir=autopilot_dir
            )
        except FileNotFoundError as e:
            return {
                "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                "isError": True
            }

        # Wait for completion
        result = wait_for_result(request_id, timeout=timeout, autopilot_dir=autopilot_dir)

        if result["status"] == "timeout":
            return {
                "content": [{"type": "text", "text": f"Test timed out after {timeout}s. Request ID: {request_id}\nYou can check status later with check_sel4_test."}],
                "isError": False
            }

        # Return paths to logs
        result_dir = paths['results'] / request_id
        sel4_log_path = result_dir / 'sel4.log'
        vm_log_path = result_dir / 'vm.log'

        # Check for error file if test failed
        error_msg = ""
        if result["status"] == "failed":
            error_file = result_dir / 'error.txt'
            if error_file.exists():
                error_msg = f"\nError: {error_file.read_text()}"

        response_text = f"""Test {result['status']}
Request ID: {request_id}
Binary: {binary_path}{error_msg}

Results directory: {result_dir}
seL4/capdl-loader log: {sel4_log_path}
VM console log: {vm_log_path}

Use get_vm_logs tool or read the files directly to view output."""

        return {
            "content": [{"type": "text", "text": response_text}],
            "isError": result["status"] == "failed"
        }

    elif name == "get_vm_logs":
        request_id = arguments["request_id"]

        result_dir = paths['results'] / request_id
        sel4_log_path = result_dir / 'sel4.log'
        vm_log_path = result_dir / 'vm.log'

        result_lines = []

        if sel4_log_path.exists():
            result_lines.append(f"seL4/capdl-loader log: {sel4_log_path}")
        else:
            result_lines.append(f"seL4/capdl-loader log: NOT FOUND")

        if vm_log_path.exists():
            result_lines.append(f"VM console log: {vm_log_path}")
        else:
            result_lines.append(f"VM console log: NOT FOUND")

        result_lines.append("")
        result_lines.append("Use Read tool to view contents.")

        return {
            "content": [{"type": "text", "text": "\n".join(result_lines)}],
            "isError": False
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
