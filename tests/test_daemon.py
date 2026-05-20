"""
Tests for daemon.py, client.py, and mcp_server.py.

Tests run without real hardware. The daemon is exercised via in-process calls
and via a real Unix socket (using a tmp_path socket). MCP tests use the
MCPServer directly with a mock daemon.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from daemon import Daemon, RunRecord, RunRegistry, _DEFAULT_SOCKET
from mcp_server import MCPServer, _read_message, _write_message


# ---------------------------------------------------------------------------
# RunRegistry
# ---------------------------------------------------------------------------

class TestRunRegistry:
    def test_add_and_get(self):
        reg = RunRegistry()
        rec = RunRecord(run_id="r1", chain="chains/test.json", status="queued")
        reg.add(rec)
        assert reg.get("r1") is rec

    def test_get_missing(self):
        reg = RunRegistry()
        assert reg.get("nonexistent") is None

    def test_list_most_recent_first(self):
        reg = RunRegistry()
        for i in range(3):
            reg.add(RunRecord(run_id=f"r{i}", chain="c.json", status="done"))
        ids = [r.run_id for r in reg.list(10)]
        assert ids == ["r2", "r1", "r0"]

    def test_list_limit(self):
        reg = RunRegistry()
        for i in range(5):
            reg.add(RunRecord(run_id=f"r{i}", chain="c.json", status="done"))
        assert len(reg.list(3)) == 3

    def test_eviction_at_max(self):
        reg = RunRegistry()
        for i in range(100):
            reg.add(RunRecord(run_id=f"r{i:04d}", chain="c.json", status="done"))
        # All 100 fit
        assert reg.get("r0000") is not None
        # Adding one more evicts oldest
        reg.add(RunRecord(run_id="r_new", chain="c.json", status="done"))
        assert reg.get("r0000") is None
        assert reg.get("r_new") is not None

    def test_to_dict(self):
        rec = RunRecord(run_id="r1", chain="chains/test.json", status="queued")
        d = rec.to_dict()
        assert d["run_id"] == "r1"
        assert d["status"] == "queued"
        assert "verdict" in d


# ---------------------------------------------------------------------------
# Daemon — in-process API
# ---------------------------------------------------------------------------

class TestDaemonAPI:
    def _make_daemon(self, tmp_path):
        return Daemon(
            socket_path=tmp_path / "test.sock",
            result_base=tmp_path / "results",
        )

    def test_submit_returns_run_id(self, tmp_path):
        d = self._make_daemon(tmp_path)
        run_id = d.submit("chains/test.json")
        assert run_id
        assert "test" in run_id

    def test_submit_custom_run_id(self, tmp_path):
        d = self._make_daemon(tmp_path)
        run_id = d.submit("chains/test.json", run_id="my-run-1")
        assert run_id == "my-run-1"

    def test_status_queued(self, tmp_path):
        d = self._make_daemon(tmp_path)
        run_id = d.submit("chains/test.json", run_id="r1")
        record = d.status("r1")
        assert record is not None
        assert record["status"] == "queued"
        assert record["chain"] == "chains/test.json"

    def test_status_not_found(self, tmp_path):
        d = self._make_daemon(tmp_path)
        assert d.status("nonexistent") is None

    def test_list_runs(self, tmp_path):
        d = self._make_daemon(tmp_path)
        d.submit("chains/a.json", run_id="r1")
        d.submit("chains/b.json", run_id="r2")
        runs = d.list_runs(10)
        assert len(runs) == 2
        ids = [r["run_id"] for r in runs]
        assert "r1" in ids
        assert "r2" in ids

    def test_cancel_queued(self, tmp_path):
        d = self._make_daemon(tmp_path)
        d.submit("chains/test.json", run_id="r1")
        ok = d.cancel("r1")
        assert ok
        assert d.status("r1")["status"] == "cancelled"

    def test_cancel_not_found(self, tmp_path):
        d = self._make_daemon(tmp_path)
        assert not d.cancel("nonexistent")

    def test_cancel_running_cancels_task(self, tmp_path):
        d = self._make_daemon(tmp_path)
        d.submit("chains/test.json", run_id="r1")
        rec = d._registry.get("r1")
        rec.status = "running"
        mock_task = MagicMock()
        d._current_run_task = mock_task
        ok = d.cancel("r1")
        assert ok
        mock_task.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# Daemon — worker processes queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_processes_chain(tmp_path):
    """Worker picks up queued request, runs ChainRunner, updates record."""
    d = Daemon(
        socket_path=tmp_path / "test.sock",
        result_base=tmp_path / "results",
    )
    chain_path = tmp_path / "noop.json"
    chain_path.write_text('{"oracle": "verdict", "label": "pass"}')

    run_id = d.submit(str(chain_path))

    # Run worker for one iteration by wrapping it with a timeout
    async def run_worker_once():
        worker_task = asyncio.create_task(d._worker())
        # Give it time to process the one queued item
        await asyncio.sleep(0.5)
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    await asyncio.wait_for(run_worker_once(), timeout=5.0)

    record = d.status(run_id)
    assert record is not None
    assert record["status"] in ("done", "error")  # may fail to load chain path


@pytest.mark.asyncio
async def test_worker_skips_cancelled(tmp_path):
    """Worker skips requests whose record is already cancelled."""
    d = Daemon(
        socket_path=tmp_path / "test.sock",
        result_base=tmp_path / "results",
    )
    chain_path = tmp_path / "noop.json"
    chain_path.write_text('{"oracle": "verdict", "label": "pass"}')

    run_id = d.submit(str(chain_path))
    d.cancel(run_id)  # cancel before worker runs

    processed = []

    async def run_worker_briefly():
        worker_task = asyncio.create_task(d._worker())
        await asyncio.sleep(0.3)
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    await asyncio.wait_for(run_worker_briefly(), timeout=3.0)

    # Status should still be cancelled (worker skipped it)
    record = d.status(run_id)
    assert record["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Daemon — Unix socket IPC
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_socket_ping(tmp_path):
    """Ping via Unix socket returns ok=True."""
    socket_path = tmp_path / "test.sock"
    d = Daemon(socket_path=socket_path, result_base=tmp_path / "results")

    async with asyncio.timeout(5.0):
        server = await asyncio.start_unix_server(
            d._handle_socket_client, path=str(socket_path)
        )
        async with server:
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write((json.dumps({"method": "ping"}) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
            resp = json.loads(line.decode())
            writer.close()
            await writer.wait_closed()

    assert resp.get("ok") is True


@pytest.mark.asyncio
async def test_socket_submit_and_status(tmp_path):
    """Submit via socket creates a record; status returns it."""
    socket_path = tmp_path / "test.sock"
    d = Daemon(socket_path=socket_path, result_base=tmp_path / "results")

    async def send_recv(req):
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(line.decode())

    async with asyncio.timeout(5.0):
        server = await asyncio.start_unix_server(
            d._handle_socket_client, path=str(socket_path)
        )
        async with server:
            submit_resp = await send_recv({"method": "submit", "chain": "chains/test.json", "run_id": "r99"})
            assert submit_resp["ok"]
            assert submit_resp["run_id"] == "r99"

            status_resp = await send_recv({"method": "status", "run_id": "r99"})
            assert status_resp["ok"]
            assert status_resp["record"]["status"] == "queued"


@pytest.mark.asyncio
async def test_socket_cancel(tmp_path):
    """Cancel via socket marks record as cancelled."""
    socket_path = tmp_path / "test.sock"
    d = Daemon(socket_path=socket_path, result_base=tmp_path / "results")
    d.submit("chains/test.json", run_id="r100")

    async def send_recv(req):
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(line.decode())

    async with asyncio.timeout(5.0):
        server = await asyncio.start_unix_server(
            d._handle_socket_client, path=str(socket_path)
        )
        async with server:
            resp = await send_recv({"method": "cancel", "run_id": "r100"})
            assert resp["ok"]

    assert d.status("r100")["status"] == "cancelled"


@pytest.mark.asyncio
async def test_socket_list(tmp_path):
    """List via socket returns all runs."""
    socket_path = tmp_path / "test.sock"
    d = Daemon(socket_path=socket_path, result_base=tmp_path / "results")
    d.submit("chains/a.json", run_id="r1")
    d.submit("chains/b.json", run_id="r2")

    async with asyncio.timeout(5.0):
        server = await asyncio.start_unix_server(
            d._handle_socket_client, path=str(socket_path)
        )
        async with server:
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write((json.dumps({"method": "list", "limit": 10}) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
            writer.close()
            await writer.wait_closed()
            resp = json.loads(line.decode())

    assert resp["ok"]
    ids = [r["run_id"] for r in resp["runs"]]
    assert "r1" in ids
    assert "r2" in ids


@pytest.mark.asyncio
async def test_socket_unknown_method(tmp_path):
    """Unknown method returns ok=False."""
    socket_path = tmp_path / "test.sock"
    d = Daemon(socket_path=socket_path, result_base=tmp_path / "results")

    async with asyncio.timeout(5.0):
        server = await asyncio.start_unix_server(
            d._handle_socket_client, path=str(socket_path)
        )
        async with server:
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write((json.dumps({"method": "bogus"}) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
            writer.close()
            await writer.wait_closed()
            resp = json.loads(line.decode())

    assert not resp["ok"]


# ---------------------------------------------------------------------------
# MCPServer — mock daemon
# ---------------------------------------------------------------------------

class MockDaemon:
    def __init__(self):
        self._runs = {}

    def submit(self, chain, timeout=3600.0, platform=None, run_id=None, config_overrides=None):
        rid = run_id or f"run-{len(self._runs)}"
        self._runs[rid] = {"run_id": rid, "chain": chain, "status": "queued"}
        return rid

    def status(self, run_id):
        return self._runs.get(run_id)

    def list_runs(self, limit=10):
        return list(self._runs.values())[:limit]

    def cancel(self, run_id):
        if run_id in self._runs:
            self._runs[run_id]["status"] = "cancelled"
            return True
        return False


def _make_mcp_rpc(method, params=None, msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


@pytest.mark.asyncio
async def test_mcp_initialize():
    server = MCPServer(MockDaemon())
    resp = await server._handle(_make_mcp_rpc("initialize"))
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in resp["result"]["capabilities"]


@pytest.mark.asyncio
async def test_mcp_tools_list():
    server = MCPServer(MockDaemon())
    resp = await server._handle(_make_mcp_rpc("tools/list"))
    tools = resp["result"]["tools"]
    names = [t["name"] for t in tools]
    assert "submit_chain" in names
    assert "check_test" in names
    assert "wait_for_test" in names
    assert "list_runs" in names
    assert "cancel_test" in names
    assert "get_logs" in names


@pytest.mark.asyncio
async def test_mcp_submit_chain():
    server = MCPServer(MockDaemon())
    resp = await server._handle(_make_mcp_rpc("tools/call", {
        "name": "submit_chain",
        "arguments": {"chain": "chains/test.json"},
    }))
    assert not resp.get("error")
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["status"] == "queued"
    assert "run_id" in content


@pytest.mark.asyncio
async def test_mcp_check_test():
    d = MockDaemon()
    d.submit("chains/test.json", run_id="r1")
    server = MCPServer(d)
    resp = await server._handle(_make_mcp_rpc("tools/call", {
        "name": "check_test",
        "arguments": {"run_id": "r1"},
    }))
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["run_id"] == "r1"


@pytest.mark.asyncio
async def test_mcp_check_test_not_found():
    server = MCPServer(MockDaemon())
    resp = await server._handle(_make_mcp_rpc("tools/call", {
        "name": "check_test",
        "arguments": {"run_id": "nonexistent"},
    }))
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["error"] == "not_found"


@pytest.mark.asyncio
async def test_mcp_wait_for_test_already_done():
    d = MockDaemon()
    d.submit("chains/test.json", run_id="r1")
    d._runs["r1"]["status"] = "done"
    d._runs["r1"]["verdict"] = "Matched"
    server = MCPServer(d)
    resp = await server._handle(_make_mcp_rpc("tools/call", {
        "name": "wait_for_test",
        "arguments": {"run_id": "r1", "max_wait_s": 2},
    }))
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["status"] == "done"
    assert "elapsed_waited_s" in content


@pytest.mark.asyncio
async def test_mcp_list_runs():
    d = MockDaemon()
    d.submit("chains/a.json", run_id="r1")
    d.submit("chains/b.json", run_id="r2")
    server = MCPServer(d)
    resp = await server._handle(_make_mcp_rpc("tools/call", {
        "name": "list_runs",
        "arguments": {"limit": 10},
    }))
    runs = json.loads(resp["result"]["content"][0]["text"])
    assert isinstance(runs, list)
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_mcp_cancel_test():
    d = MockDaemon()
    d.submit("chains/test.json", run_id="r1")
    server = MCPServer(d)
    resp = await server._handle(_make_mcp_rpc("tools/call", {
        "name": "cancel_test",
        "arguments": {"run_id": "r1"},
    }))
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["ok"]


@pytest.mark.asyncio
async def test_mcp_get_logs_not_found():
    server = MCPServer(MockDaemon())
    resp = await server._handle(_make_mcp_rpc("tools/call", {
        "name": "get_logs",
        "arguments": {"run_id": "nonexistent"},
    }))
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["error"] == "not_found"


@pytest.mark.asyncio
async def test_mcp_get_logs_with_files(tmp_path):
    d = MockDaemon()
    d.submit("chains/test.json", run_id="r1")
    result_dir = tmp_path / "results" / "r1"
    result_dir.mkdir(parents=True)
    (result_dir / "verdict.json").write_text('{"verdict": "Matched"}')
    d._runs["r1"]["result_dir"] = str(result_dir)

    server = MCPServer(d)
    resp = await server._handle(_make_mcp_rpc("tools/call", {
        "name": "get_logs",
        "arguments": {"run_id": "r1"},
    }))
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["result_dir"] == str(result_dir)
    assert any(f["name"] == "verdict.json" for f in content["files"])


@pytest.mark.asyncio
async def test_mcp_get_logs_read_file(tmp_path):
    d = MockDaemon()
    d.submit("chains/test.json", run_id="r1")
    result_dir = tmp_path / "results" / "r1"
    result_dir.mkdir(parents=True)
    (result_dir / "verdict.json").write_text('{"verdict": "Matched"}')
    d._runs["r1"]["result_dir"] = str(result_dir)

    server = MCPServer(d)
    resp = await server._handle(_make_mcp_rpc("tools/call", {
        "name": "get_logs",
        "arguments": {"run_id": "r1", "file_name": "verdict.json"},
    }))
    content = json.loads(resp["result"]["content"][0]["text"])
    assert "contents" in content
    assert "Matched" in content["contents"]


@pytest.mark.asyncio
async def test_mcp_ping():
    server = MCPServer(MockDaemon())
    resp = await server._handle(_make_mcp_rpc("ping"))
    assert resp["result"] == {}


@pytest.mark.asyncio
async def test_mcp_unknown_method():
    server = MCPServer(MockDaemon())
    resp = await server._handle(_make_mcp_rpc("unknown/method"))
    assert "error" in resp


@pytest.mark.asyncio
async def test_mcp_notification_no_response():
    """Notifications (no id) should return None (no response)."""
    server = MCPServer(MockDaemon())
    msg = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
    resp = await server._handle(msg)
    assert resp is None


@pytest.mark.asyncio
async def test_mcp_list_sessions_empty():
    server = MCPServer(MockDaemon())
    resp = await server._handle(_make_mcp_rpc("tools/call", {
        "name": "list_sessions",
        "arguments": {},
    }))
    content = json.loads(resp["result"]["content"][0]["text"])
    assert "sessions" in content


@pytest.mark.asyncio
async def test_mcp_session_not_found():
    server = MCPServer(MockDaemon())
    for tool in ("read_session", "send_to_session", "close_session"):
        args = {"session_id": "nonexistent"}
        if tool == "send_to_session":
            args["data"] = "hello"
        resp = await server._handle(_make_mcp_rpc("tools/call", {
            "name": tool,
            "arguments": args,
        }))
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["error"] == "session_not_found", f"{tool} should report session_not_found"


# ---------------------------------------------------------------------------
# MCP framing helpers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_message_content_length():
    body = b'{"jsonrpc":"2.0","method":"ping"}'
    framed = f"Content-Length: {len(body)}\r\n\r\n".encode() + body

    reader = asyncio.StreamReader()
    reader.feed_data(framed)
    reader.feed_eof()

    msg = await _read_message(reader)
    assert msg["method"] == "ping"


@pytest.mark.asyncio
async def test_read_message_jsonl():
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"method":"ping"}\n')
    reader.feed_eof()

    msg = await _read_message(reader)
    assert msg["method"] == "ping"


@pytest.mark.asyncio
async def test_read_message_eof():
    reader = asyncio.StreamReader()
    reader.feed_eof()

    msg = await _read_message(reader)
    assert msg is None
