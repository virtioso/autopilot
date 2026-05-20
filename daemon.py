"""
Autopilot daemon.

Single asyncio event loop hosting three concurrent tasks:

  _worker()              — consumes RunRequests from asyncio.Queue one at a time;
                           runs ChainRunner; updates RunRegistry.
  _serve_unix_socket()   — serves client.py connections on a Unix socket;
                           newline-delimited JSON protocol.
  _serve_mcp()           — serves the MCP stdio interface for AI integration;
                           JSON-RPC 2.0 with Content-Length framing.

RunRegistry holds the last 100 run records in memory. Clients poll via the
Unix socket for status; they do not need to poll the filesystem.

Signal handling: SIGINT/SIGTERM cancel the asyncio.TaskGroup, which propagates
CancelledError to all three tasks. The worker cancels any in-progress chain run
(ChainRunner.cancel()) before the event loop exits.

Usage:
  python daemon.py [--socket PATH] [--result-dir DIR] [--platform NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from engine.oracle import Matched, TimeoutVerdict
from engine.runtime import ChainRunner, _verdict_exit_code

log = structlog.get_logger()

# Default socket path
_DEFAULT_SOCKET = Path.home() / ".autopilot" / "daemon.sock"

# Maximum runs kept in memory
_REGISTRY_MAX = 100


# ---------------------------------------------------------------------------
# RunRecord and RunRegistry
# ---------------------------------------------------------------------------

@dataclass
class RunRecord:
    run_id: str
    chain: str
    status: str          # queued | running | done | cancelled | error
    platform: str | None = None
    verdict: str | None = None    # "Matched" | "TimeoutVerdict" | "Error"
    label: str | None = None      # Matched.label or Error.reason
    exit_code: int | None = None
    result_dir: str | None = None
    submitted_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunRequest:
    run_id: str
    chain_path: Path
    timeout: float = 3600.0
    platform: str | None = None
    config_overrides: dict = field(default_factory=dict)


class RunRegistry:
    """In-memory store for the last _REGISTRY_MAX run records."""

    def __init__(self) -> None:
        self._runs: deque[RunRecord] = deque(maxlen=_REGISTRY_MAX)
        self._by_id: dict[str, RunRecord] = {}

    def add(self, record: RunRecord) -> None:
        if len(self._runs) == _REGISTRY_MAX:
            oldest = self._runs[0]
            self._by_id.pop(oldest.run_id, None)
        self._runs.append(record)
        self._by_id[record.run_id] = record

    def get(self, run_id: str) -> RunRecord | None:
        return self._by_id.get(run_id)

    def list(self, limit: int = 20) -> list[RunRecord]:
        runs = list(self._runs)
        runs.reverse()
        return runs[:limit]


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class Daemon:
    """
    Main daemon: asyncio event loop hosting worker + socket server + MCP server.
    """

    def __init__(
        self,
        socket_path: Path = _DEFAULT_SOCKET,
        result_base: Path | None = None,
        platform: str | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._result_base = result_base or Path("results")
        self._platform = platform
        self._queue: asyncio.Queue[RunRequest] = asyncio.Queue()
        self._registry = RunRegistry()
        self._current_run_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public API (called by socket handler and MCP handler)
    # ------------------------------------------------------------------

    def submit(
        self,
        chain: str,
        timeout: float = 3600.0,
        platform: str | None = None,
        run_id: str | None = None,
        config_overrides: dict | None = None,
    ) -> str:
        """Enqueue a chain run and return its run_id."""
        if run_id is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            stem = Path(chain).stem
            run_id = f"{stem}-{ts}"

        req = RunRequest(
            run_id=run_id,
            chain_path=Path(chain),
            timeout=timeout,
            platform=platform or self._platform,
            config_overrides=config_overrides or {},
        )
        record = RunRecord(
            run_id=run_id,
            chain=chain,
            status="queued",
            platform=req.platform,
            submitted_at=datetime.now(timezone.utc).isoformat(),
        )
        self._registry.add(record)
        self._queue.put_nowait(req)

        log.info("daemon.submitted", run_id=run_id, chain=chain)
        return run_id

    def status(self, run_id: str) -> dict | None:
        record = self._registry.get(run_id)
        if record is None:
            return None
        return record.to_dict()

    def list_runs(self, limit: int = 20) -> list[dict]:
        return [r.to_dict() for r in self._registry.list(limit)]

    def cancel(self, run_id: str) -> bool:
        """Cancel a queued or running run. Returns True if action was taken."""
        record = self._registry.get(run_id)
        if record is None:
            return False

        if record.status == "queued":
            record.status = "cancelled"
            record.finished_at = datetime.now(timezone.utc).isoformat()
            # The queued request is left in the queue; _worker will see
            # status=="cancelled" and skip it.
            log.info("daemon.cancelled_queued", run_id=run_id)
            return True

        if record.status == "running" and self._current_run_task:
            self._current_run_task.cancel()
            log.info("daemon.cancelled_running", run_id=run_id)
            return True

        return False

    # ------------------------------------------------------------------
    # Daemon event loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start all daemon tasks. Returns when all tasks complete or are cancelled."""
        socket_path = self._socket_path
        socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale socket from previous run
        if socket_path.exists():
            socket_path.unlink()

        log.info(
            "daemon.starting",
            socket=str(socket_path),
            result_base=str(self._result_base),
            platform=self._platform,
        )

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._worker(), name="daemon_worker")
                tg.create_task(
                    self._serve_unix_socket(socket_path),
                    name="daemon_unix_socket",
                )
                tg.create_task(self._serve_mcp(), name="daemon_mcp")
        finally:
            if socket_path.exists():
                socket_path.unlink(missing_ok=True)
            log.info("daemon.stopped")

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        """Consume chain run requests one at a time."""
        log.info("daemon.worker.ready")
        while True:
            req = await self._queue.get()
            record = self._registry.get(req.run_id)

            # Skip if cancelled while queued
            if record is None or record.status == "cancelled":
                log.info("daemon.worker.skipped_cancelled", run_id=req.run_id)
                continue

            record.status = "running"
            record.started_at = datetime.now(timezone.utc).isoformat()
            log.info("daemon.worker.starting", run_id=req.run_id, chain=str(req.chain_path))

            runner = ChainRunner(
                req.chain_path,
                result_base=self._result_base,
                run_id=req.run_id,
                timeout=req.timeout,
                platform=req.platform,
                config_overrides=req.config_overrides,
            )
            record.result_dir = str(runner.result_dir)

            self._current_run_task = asyncio.create_task(
                runner.run(),
                name=f"chain_{req.run_id}",
            )
            try:
                verdict = await self._current_run_task
                record.status = "done"
                record.verdict = type(verdict).__name__
                record.label = getattr(verdict, "label", None)
                record.exit_code = _verdict_exit_code(verdict)
                log.info(
                    "daemon.worker.done",
                    run_id=req.run_id,
                    verdict=record.verdict,
                    label=record.label,
                )
            except asyncio.CancelledError:
                record.status = "cancelled"
                log.info("daemon.worker.cancelled", run_id=req.run_id)
            except Exception as exc:
                record.status = "error"
                record.verdict = "Error"
                record.label = str(exc)
                log.error("daemon.worker.error", run_id=req.run_id, error=repr(exc))
            finally:
                self._current_run_task = None
                record.finished_at = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Unix socket server
    # ------------------------------------------------------------------

    async def _serve_unix_socket(self, socket_path: Path) -> None:
        server = await asyncio.start_unix_server(
            self._handle_socket_client,
            path=str(socket_path),
        )
        log.info("daemon.socket.listening", path=str(socket_path))
        async with server:
            await server.serve_forever()

    async def _handle_socket_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = json.loads(line.decode())
            except json.JSONDecodeError as exc:
                _write_json(writer, {"ok": False, "error": f"json_parse: {exc}"})
                return

            resp = self._dispatch_socket(req)
            _write_json(writer, resp)
        except Exception as exc:
            log.warning("daemon.socket.handler_error", error=repr(exc))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _dispatch_socket(self, req: dict) -> dict:
        method = req.get("method", "")

        if method == "submit":
            run_id = self.submit(
                chain=req.get("chain", ""),
                timeout=float(req.get("timeout", 3600.0)),
                platform=req.get("platform"),
                run_id=req.get("run_id"),
                config_overrides=req.get("config_overrides") or {},
            )
            return {"ok": True, "run_id": run_id}

        if method == "status":
            record = self.status(req.get("run_id", ""))
            if record is None:
                return {"ok": False, "error": "not_found"}
            return {"ok": True, "record": record}

        if method == "list":
            limit = int(req.get("limit", 20))
            return {"ok": True, "runs": self.list_runs(limit)}

        if method == "cancel":
            ok = self.cancel(req.get("run_id", ""))
            return {"ok": ok, "error": None if ok else "not_found_or_already_done"}

        if method == "ping":
            return {"ok": True, "pong": True}

        return {"ok": False, "error": f"unknown method: {method!r}"}

    # ------------------------------------------------------------------
    # MCP server (co-task)
    # ------------------------------------------------------------------

    async def _serve_mcp(self) -> None:
        """Serve MCP JSON-RPC 2.0 on stdin/stdout."""
        from mcp_server import MCPServer
        mcp = MCPServer(self)
        await mcp.serve()


def _write_json(writer: asyncio.StreamWriter, data: dict) -> None:
    writer.write((json.dumps(data) + "\n").encode())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autopilot-daemon", description="Autopilot daemon")
    p.add_argument("--socket", type=Path, default=_DEFAULT_SOCKET, metavar="PATH")
    p.add_argument("--result-dir", type=Path, default=Path("results"), metavar="DIR")
    p.add_argument("--platform", default=None, metavar="NAME")
    return p


async def _main_async(args: argparse.Namespace) -> None:
    daemon = Daemon(
        socket_path=args.socket,
        result_base=args.result_dir,
        platform=args.platform,
    )

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def _handle_signal(sig: signal.Signals) -> None:
        log.warning("daemon.signal", signal=sig.name, action="shutting_down")
        if main_task and not main_task.done():
            main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, signal.Signals(sig))

    try:
        await daemon.run()
    except asyncio.CancelledError:
        pass


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
