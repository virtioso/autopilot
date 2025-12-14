#!/usr/bin/env python3
"""
seL4 Autopilot MCP Server

An MCP (Model Context Protocol) server that provides tools for testing
seL4 EFI binaries on NVIDIA Orin AGX hardware.

Tools:
- test_sel4_binary: Submit a binary, wait for completion, return results
- check_sel4_test: Check status of a submitted test
- get_sel4_log: Get the console output of a completed test

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
import sys
from datetime import datetime
from typing import Any

# Import the sel4_client library
sys.path.insert(0, '/home/hlyytine/pkvm/autopilot')
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
    RESULTS_DIR,
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
                }
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
                }
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
                }
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
                }
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
                }
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
                }
            },
            "required": ["request_id"]
        }
    }
]


def handle_tool_call(name: str, arguments: dict) -> dict:
    """Handle a tool call and return the result."""

    if name == "test_sel4_binary":
        binary_path = arguments["binary_path"]
        description = arguments.get("description", "")
        timeout = arguments.get("timeout", 300)

        # Generate timestamped binary name to detect upload failures
        binary_name = f"sel4test-{datetime.now().strftime('%Y%m%d-%H%M%S')}.efi"

        # Submit the test
        try:
            request_id = submit_sel4_test(
                binary_path=binary_path,
                binary_name=binary_name,
                description=description
            )
        except FileNotFoundError as e:
            return {
                "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                "isError": True
            }

        # Wait for completion
        result = wait_for_result(request_id, timeout=timeout)

        if result["status"] == "timeout":
            return {
                "content": [{"type": "text", "text": f"Test timed out after {timeout}s. Request ID: {request_id}\nYou can check status later with check_sel4_test."}],
                "isError": False
            }

        # Get the log
        log = get_sel4_log(request_id)

        # Check for error file if test failed
        error_msg = ""
        if result["status"] == "failed":
            error_file = RESULTS_DIR / request_id / 'error.txt'
            if error_file.exists():
                error_msg = f"\nError: {error_file.read_text()}"

        response_text = f"""Test {result['status']}
Request ID: {request_id}
Binary: {binary_path}{error_msg}

Console Output:
{log}"""

        return {
            "content": [{"type": "text", "text": response_text}],
            "isError": result["status"] == "failed"
        }

    elif name == "check_sel4_test":
        request_id = arguments["request_id"]
        status = get_status(request_id)

        response_text = f"Status: {status['status']}"
        if "result_dir" in status:
            response_text += f"\nResults directory: {status['result_dir']}"

        # Include request info if available
        info = get_request_info(request_id)
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

        if raw:
            log = get_raw_log(request_id)
        else:
            log = get_sel4_log(request_id)

        if not log:
            return {
                "content": [{"type": "text", "text": f"No log found for request {request_id}"}],
                "isError": True
            }

        return {
            "content": [{"type": "text", "text": log}],
            "isError": False
        }

    elif name == "list_sel4_tests":
        show_pending = arguments.get("pending", True)
        show_completed = arguments.get("completed", False)
        show_failed = arguments.get("failed", False)

        result_lines = []

        if show_pending:
            pending = list_pending()
            if pending:
                result_lines.append("Pending:")
                result_lines.extend(f"  {ts}" for ts in pending[-10:])  # Last 10

        if show_completed:
            completed = list_completed()
            if completed:
                result_lines.append("Completed:")
                result_lines.extend(f"  {ts}" for ts in completed[-10:])  # Last 10

        if show_failed:
            failed = list_failed()
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

        # Submit the multi-run test
        try:
            request_id = submit_multi_run_test(
                binary_path=binary_path,
                run_count=run_count,
                binary_name=binary_name,
                test_type=test_type,
                description=description
            )
        except FileNotFoundError as e:
            return {
                "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                "isError": True
            }

        # Wait for completion with scaled timeout
        total_timeout = timeout_per_run * run_count
        result = wait_for_result(request_id, timeout=total_timeout)

        if result["status"] == "timeout":
            return {
                "content": [{"type": "text", "text": f"Multi-run test timed out after {total_timeout}s. Request ID: {request_id}\nYou can check status later with check_sel4_test."}],
                "isError": False
            }

        # Get logs and build response
        logs = get_multi_run_logs(request_id)
        summary_text = ""
        if logs['summary']:
            s = logs['summary']
            summary_text = f"Summary: {s.get('completed_runs', '?')}/{s.get('total_runs', '?')} runs completed"

        # Check for top-level error file if test failed
        error_msg = ""
        if result["status"] == "failed":
            error_file = RESULTS_DIR / request_id / 'error.txt'
            if error_file.exists():
                error_msg = f"\nError: {error_file.read_text()}"

        response_text = f"""Multi-run test {result['status']}
Request ID: {request_id}
Binary: {binary_path}
Run count: {run_count}
{summary_text}{error_msg}

"""
        for run in logs['runs']:
            response_text += f"--- Run {run['run_number']} ---\n"
            if 'error' in run:
                response_text += f"Error: {run['error']}\n"
            elif 'sel4_log' in run:
                # Truncate long logs
                log_text = run['sel4_log']
                if len(log_text) > 3000:
                    log_text = log_text[:3000] + "\n... (truncated)"
                response_text += log_text + "\n"
            elif 'kernel_log' in run:
                log_text = run['kernel_log']
                if len(log_text) > 3000:
                    log_text = log_text[:3000] + "\n... (truncated)"
                response_text += log_text + "\n"
            response_text += "\n"

        return {
            "content": [{"type": "text", "text": response_text}],
            "isError": result["status"] == "failed"
        }

    elif name == "get_multi_run_logs":
        request_id = arguments["request_id"]
        logs = get_multi_run_logs(request_id)

        if not logs['runs']:
            return {
                "content": [{"type": "text", "text": f"No multi-run logs found for request {request_id}"}],
                "isError": True
            }

        response_text = ""
        if logs['summary']:
            s = logs['summary']
            response_text += f"Summary: {s.get('completed_runs', '?')}/{s.get('total_runs', '?')} runs completed, {s.get('failed_runs', '?')} failed\n\n"

        for run in logs['runs']:
            response_text += f"=== Run {run['run_number']} ===\n"
            if 'error' in run:
                response_text += f"Error: {run['error']}\n"
            elif 'sel4_log' in run:
                response_text += run['sel4_log'] + "\n"
            elif 'kernel_log' in run:
                response_text += run['kernel_log'] + "\n"
            response_text += "\n"

        return {
            "content": [{"type": "text", "text": response_text}],
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
