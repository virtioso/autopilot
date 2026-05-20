"""
MCP (Model Context Protocol) server for Autopilot.

Runs as a co-task inside the daemon process (same event loop). Reads JSON-RPC
2.0 from stdin, writes responses to stdout, using Content-Length framing per
the MCP specification.

Tools exposed (domain-neutral names, replacing sel4-specific names):
  submit_chain          — Enqueue a chain run; returns run_id immediately
  check_test            — Get status of a run
  wait_for_test         — Poll until run completes (max 30s per call)
  list_runs             — List recent runs (queued/running/done/cancelled/error)
  cancel_test           — Cancel a queued or running chain run
  get_logs              — List result files; optionally read a file
  list_sessions         — List live interactive console sessions
  read_session          — Read output from a console session
  send_to_session       — Send bytes/command to a console session
  close_session         — Signal a console session to end

The daemon object is injected at construction (not imported globally) so
MCPServer is testable with a mock daemon.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import structlog

log = structlog.get_logger()

# MCP protocol version
_MCP_VERSION = "2024-11-05"
_SERVER_NAME = "autopilot"
_SERVER_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "submit_chain",
        "description": (
            "Enqueue a chain run. Returns immediately with a run_id. "
            "Use check_test or wait_for_test to track completion."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "chain": {"type": "string", "description": "Path to chain JSON file"},
                "timeout": {"type": "number", "description": "Execution timeout in seconds (default 3600)"},
                "platform": {"type": "string", "description": "Platform profile (e.g. 'orin-agx')"},
                "run_id": {"type": "string", "description": "Optional run ID override"},
            },
            "required": ["chain"],
        },
    },
    {
        "name": "check_test",
        "description": "Get current status and verdict for a chain run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "wait_for_test",
        "description": (
            "Poll until a run finishes or max_wait_s elapses. "
            "If still running, returns current status so you can call again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "max_wait_s": {"type": "number", "description": "Max seconds to block (default 30)"},
                "poll_interval": {"type": "number", "description": "Poll interval in seconds (default 1)"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "list_runs",
        "description": "List recent chain runs with their status and verdict.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max runs to return (default 10)"},
                "status_filter": {
                    "type": "string",
                    "description": "Filter by status: queued|running|done|cancelled|error (default: all)",
                },
            },
        },
    },
    {
        "name": "cancel_test",
        "description": "Cancel a queued or running chain run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "get_logs",
        "description": (
            "List result files for a run. Optionally read a specific file. "
            "Use file_name to read verdict.json, events.jsonl, or console logs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "file_name": {"type": "string", "description": "File to read (e.g. 'verdict.json')"},
                "tail": {"type": "integer", "description": "Read only last N lines of the file"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "list_sessions",
        "description": "List active interactive console sessions (streams exposed by InteractiveOracle).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_session",
        "description": "Read buffered output from an interactive console session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "timeout": {"type": "number", "description": "Max seconds to wait for output (default 1)"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "send_to_session",
        "description": "Send bytes to an interactive console session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "data": {"type": "string", "description": "Text to send (newline appended by default)"},
                "append_newline": {"type": "boolean", "description": "Append newline (default true)"},
            },
            "required": ["session_id", "data"],
        },
    },
    {
        "name": "close_session",
        "description": "Signal an interactive console session to end (sets done event).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    },
]


# ---------------------------------------------------------------------------
# MCPServer
# ---------------------------------------------------------------------------

class MCPServer:
    """
    MCP JSON-RPC 2.0 server running as a co-task in the daemon event loop.

    Reads Content-Length framed messages from stdin (asyncio), writes
    framed responses to stdout. Falls back to raw JSONL if no framing detected.
    """

    def __init__(self, daemon: Any) -> None:
        self._daemon = daemon

    async def serve(self) -> None:
        """Read and process MCP messages until stdin is closed or cancelled."""
        stdin_reader = await _open_stdin()
        stdout_writer = await _open_stdout()

        log.info("mcp.serving")
        try:
            while True:
                msg = await _read_message(stdin_reader)
                if msg is None:
                    log.info("mcp.stdin_closed")
                    break
                response = await self._handle(msg)
                if response is not None:
                    await _write_message(stdout_writer, response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("mcp.serve_error", error=repr(exc))

    # ------------------------------------------------------------------
    # JSON-RPC dispatch
    # ------------------------------------------------------------------

    async def _handle(self, msg: dict) -> dict | None:
        msg_id = msg.get("id")
        method = msg.get("method", "")

        # Notifications have no id — no response needed
        if msg_id is None and not method.startswith("initialize"):
            return None

        try:
            result = await self._dispatch(method, msg.get("params") or {})
            return _ok(msg_id, result)
        except Exception as exc:
            log.warning("mcp.tool_error", method=method, error=repr(exc))
            return _err(msg_id, -32603, str(exc))

    async def _dispatch(self, method: str, params: dict) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": _MCP_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
            }

        if method == "tools/list":
            return {"tools": _TOOLS}

        if method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments") or {}
            text = await self._call_tool(tool_name, args)
            return {"content": [{"type": "text", "text": text}], "isError": False}

        if method == "ping":
            return {}

        raise ValueError(f"unknown method: {method!r}")

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _call_tool(self, name: str, args: dict) -> str:
        d = self._daemon

        if name == "submit_chain":
            run_id = d.submit(
                chain=args["chain"],
                timeout=float(args.get("timeout", 3600.0)),
                platform=args.get("platform"),
                run_id=args.get("run_id"),
            )
            return json.dumps({"run_id": run_id, "status": "queued"}, indent=2)

        if name == "check_test":
            record = d.status(args["run_id"])
            if record is None:
                return json.dumps({"error": "not_found"})
            return json.dumps(record, indent=2)

        if name == "wait_for_test":
            run_id = args["run_id"]
            max_wait = float(args.get("max_wait_s", 30.0))
            poll_interval = float(args.get("poll_interval", 1.0))
            deadline = time.monotonic() + max_wait
            record = None
            while True:
                record = d.status(run_id)
                if record is None:
                    return json.dumps({"error": "not_found"})
                if record["status"] not in ("queued", "running"):
                    break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(poll_interval)
            assert record is not None
            record["elapsed_waited_s"] = round(max_wait - max(0, deadline - time.monotonic()), 1)
            return json.dumps(record, indent=2)

        if name == "list_runs":
            limit = int(args.get("limit", 10))
            status_filter = args.get("status_filter")
            runs = d.list_runs(limit * 3 if status_filter else limit)
            if status_filter:
                runs = [r for r in runs if r.get("status") == status_filter][:limit]
            return json.dumps(runs, indent=2)

        if name == "cancel_test":
            ok = d.cancel(args["run_id"])
            return json.dumps({"ok": ok, "run_id": args["run_id"]})

        if name == "get_logs":
            from client import AutopilotClient
            record = d.status(args["run_id"])
            if record is None:
                return json.dumps({"error": "not_found"})
            result_dir = record.get("result_dir")
            if not result_dir:
                return json.dumps({"error": "no_result_dir", "record": record})
            from pathlib import Path
            result_path = Path(result_dir)
            files = []
            if result_path.exists():
                for f in sorted(result_path.rglob("*")):
                    if f.is_file():
                        files.append({"name": f.name, "path": str(f), "size": f.stat().st_size})
            out: dict = {"result_dir": result_dir, "files": files}
            file_name = args.get("file_name")
            if file_name:
                for f_info in files:
                    if f_info["name"] == file_name:
                        try:
                            text = Path(f_info["path"]).read_text(errors="replace")
                            tail = args.get("tail")
                            if tail:
                                text = "\n".join(text.splitlines()[-int(tail):])
                            out["contents"] = text
                        except OSError as exc:
                            out["read_error"] = str(exc)
                        break
            return json.dumps(out, indent=2)

        if name == "list_sessions":
            from adapters.interactive import list_sessions
            sessions = list_sessions()
            return json.dumps({"sessions": sessions}, indent=2)

        if name == "read_session":
            from adapters.interactive import get_session
            session_id = args["session_id"]
            timeout = float(args.get("timeout", 1.0))
            bridge = get_session(session_id)
            if bridge is None:
                return json.dumps({"error": "session_not_found"})
            chunks = []
            try:
                item = await asyncio.wait_for(bridge.read_q.get(), timeout=timeout)
                if item is not None:
                    chunks.append(item.decode("utf-8", errors="replace"))
                # Drain remaining bytes available immediately
                while not bridge.read_q.empty():
                    item = bridge.read_q.get_nowait()
                    if item is not None:
                        chunks.append(item.decode("utf-8", errors="replace"))
            except asyncio.TimeoutError:
                pass
            return json.dumps({"session_id": session_id, "output": "".join(chunks)}, indent=2)

        if name == "send_to_session":
            from adapters.interactive import get_session
            session_id = args["session_id"]
            data = args["data"]
            append_newline = args.get("append_newline", True)
            bridge = get_session(session_id)
            if bridge is None:
                return json.dumps({"error": "session_not_found"})
            if append_newline and not data.endswith("\n"):
                data += "\n"
            await bridge.send(data.encode())
            return json.dumps({"ok": True, "session_id": session_id})

        if name == "close_session":
            from adapters.interactive import get_session
            session_id = args["session_id"]
            bridge = get_session(session_id)
            if bridge is None:
                return json.dumps({"error": "session_not_found"})
            bridge.signal_done()
            return json.dumps({"ok": True, "session_id": session_id})

        raise ValueError(f"unknown tool: {name!r}")


# ---------------------------------------------------------------------------
# Async stdin/stdout helpers
# ---------------------------------------------------------------------------

async def _open_stdin() -> asyncio.StreamReader:
    """Wrap sys.stdin as an asyncio StreamReader."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    return reader


async def _open_stdout() -> asyncio.StreamWriter:
    """Wrap sys.stdout as an asyncio StreamWriter."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.connect_write_pipe(
        lambda: asyncio.BaseProtocol(), sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    return writer


async def _read_message(reader: asyncio.StreamReader) -> dict | None:
    """
    Read one JSON-RPC message. Supports Content-Length framing and raw JSONL.

    Returns None on EOF.
    """
    # Peek at first byte to detect framing
    try:
        first = await reader.read(1)
    except asyncio.IncompleteReadError:
        return None
    if not first:
        return None

    if first == b"C":
        # Content-Length framing: "Content-Length: N\r\n\r\n{...}"
        header_bytes = first + await reader.readuntil(b"\r\n\r\n")
        header_text = header_bytes.decode("utf-8", errors="replace")
        content_length = 0
        for line in header_text.splitlines():
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
        if content_length <= 0:
            return None
        body = await reader.readexactly(content_length)
    else:
        # Raw JSONL: read to newline
        rest = await reader.readline()
        body = first + rest

    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return None


async def _write_message(writer: asyncio.StreamWriter, msg: dict) -> None:
    """Write one JSON-RPC message with Content-Length framing."""
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    writer.write(header + body)
    await writer.drain()


def _ok(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
