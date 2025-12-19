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
                    "enum": ["el1", "el2", "el2-ftrace"],
                    "description": "Kernel mode: 'el2' for hypervisor mode (default), 'el1' for no hypervisor, 'el2-ftrace' for hypervisor with function tracing",
                    "default": "el2"
                }
            }
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
                build_config={'arm_hyp': arm_hyp, 'platform': 'orinagx'}
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

        # Return paths instead of full log content (logs can be huge with ftrace)
        result_dir = RESULTS_DIR / request_id
        sel4_log_path = result_dir / 'sel4.log'
        uart_raw_path = result_dir / 'uart-raw.log'

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
seL4 log: {sel4_log_path}
Raw UART log: {uart_raw_path}

Use get_sel4_log tool or read the files directly to view output."""

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

        result_dir = RESULTS_DIR / request_id
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
                build_config=build_config
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

        # Return paths instead of full log content (logs can be huge with ftrace)
        result_dir = RESULTS_DIR / request_id

        # Get summary info
        logs = get_multi_run_logs(request_id)
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
        result_dir = RESULTS_DIR / request_id

        if not result_dir.exists():
            return {
                "content": [{"type": "text", "text": f"No results found for request {request_id}"}],
                "isError": True
            }

        # Get summary info
        logs = get_multi_run_logs(request_id)
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

    elif name == "build_sel4test":
        mode = arguments.get("mode", "el2")

        # Configuration
        workspace_root = Path("/home/hlyytine/tii-sel4")
        build_dir = workspace_root / "orinagx_sel4test"
        binary_path = build_dir / "images" / "sel4test-driver-image-arm-orinagx"

        # Determine defconfig based on mode
        if mode == "el2":
            defconfig = "orinagx_defconfig"
        elif mode == "el2-ftrace":
            defconfig = "orinagx_ftrace_defconfig"
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
