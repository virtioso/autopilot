"""
Autopilot client library and CLI.

Connects to the daemon via Unix socket using newline-delimited JSON.

Async API:
    client = AutopilotClient()
    run_id = await client.submit("chains/sel4test.json", platform="orin-agx")
    record = await client.status(run_id)

Sync API (for scripts):
    client = SyncAutopilotClient()
    run_id = client.submit("chains/sel4test.json")

CLI:
    python client.py submit --chain chains/sel4test.json [--wait]
    python client.py status <run_id>
    python client.py cancel <run_id>
    python client.py list [--limit 20]
    python client.py logs <run_id> [--tail 100]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import structlog

log = structlog.get_logger()

from daemon import _DEFAULT_SOCKET


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------

class AutopilotClient:
    """
    Async client that talks to the daemon via Unix socket.

    Each method opens a fresh connection, sends one request, reads one response,
    and closes the connection. The daemon handles concurrent clients.
    """

    def __init__(self, socket_path: Path | None = None) -> None:
        self._socket_path = socket_path or _DEFAULT_SOCKET

    async def _rpc(self, request: dict) -> dict:
        """Send one JSON request and return the response."""
        try:
            reader, writer = await asyncio.open_unix_connection(
                path=str(self._socket_path)
            )
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise ConnectionError(
                f"Cannot connect to daemon at {self._socket_path}: {exc}. "
                "Is the daemon running? (`python daemon.py`)"
            ) from exc

        try:
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
            return json.loads(line.decode())
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def ping(self) -> bool:
        resp = await self._rpc({"method": "ping"})
        return resp.get("ok", False)

    async def submit(
        self,
        chain: str | Path,
        *,
        timeout: float = 3600.0,
        platform: str | None = None,
        run_id: str | None = None,
        config_overrides: dict | None = None,
    ) -> str:
        """Submit a chain run and return the run_id."""
        resp = await self._rpc({
            "method": "submit",
            "chain": str(chain),
            "timeout": timeout,
            "platform": platform,
            "run_id": run_id,
            "config_overrides": config_overrides or {},
        })
        if not resp.get("ok"):
            raise RuntimeError(f"submit failed: {resp.get('error')}")
        return resp["run_id"]

    async def status(self, run_id: str) -> dict | None:
        """Return the run record dict, or None if not found."""
        resp = await self._rpc({"method": "status", "run_id": run_id})
        if not resp.get("ok"):
            return None
        return resp.get("record")

    async def wait(
        self,
        run_id: str,
        poll_interval: float = 1.0,
        timeout: float = 3600.0,
    ) -> dict:
        """Poll until the run is no longer queued/running. Returns final record."""
        import time
        deadline = time.monotonic() + timeout
        while True:
            record = await self.status(run_id)
            if record is None:
                raise RuntimeError(f"Run {run_id!r} not found in registry")
            if record["status"] not in ("queued", "running"):
                return record
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for run {run_id!r} "
                    f"(still {record['status']} after {timeout}s)"
                )
            await asyncio.sleep(poll_interval)

    async def cancel(self, run_id: str) -> bool:
        resp = await self._rpc({"method": "cancel", "run_id": run_id})
        return resp.get("ok", False)

    async def list_runs(self, limit: int = 20) -> list[dict]:
        resp = await self._rpc({"method": "list", "limit": limit})
        if not resp.get("ok"):
            return []
        return resp.get("runs", [])

    async def get_logs(
        self,
        run_id: str,
        *,
        file_name: str | None = None,
        tail: int | None = None,
    ) -> dict:
        """
        Return log information for a run.

        result_dir: path to the run's result directory
        files: list of {name, path, size} dicts for files in result_dir
        contents: file contents if file_name specified (tail lines if tail set)
        """
        record = await self.status(run_id)
        if record is None:
            return {"error": "not_found"}

        result_dir = record.get("result_dir")
        if not result_dir:
            return {"error": "no_result_dir", "record": record}

        result_path = Path(result_dir)
        files = []
        if result_path.exists():
            for f in sorted(result_path.rglob("*")):
                if f.is_file():
                    files.append({
                        "name": f.name,
                        "path": str(f),
                        "size": f.stat().st_size,
                    })

        result: dict = {"result_dir": result_dir, "files": files}

        if file_name:
            for f_info in files:
                if f_info["name"] == file_name:
                    content_path = Path(f_info["path"])
                    try:
                        text = content_path.read_text(errors="replace")
                        if tail:
                            lines = text.splitlines()
                            text = "\n".join(lines[-tail:])
                        result["contents"] = text
                    except OSError as exc:
                        result["read_error"] = str(exc)
                    break

        return result


# ---------------------------------------------------------------------------
# Sync wrapper
# ---------------------------------------------------------------------------

class SyncAutopilotClient:
    """Sync wrapper for use in scripts and tests that don't run an event loop."""

    def __init__(self, socket_path: Path | None = None) -> None:
        self._async = AutopilotClient(socket_path)

    def _run(self, coro):
        return asyncio.run(coro)

    def ping(self) -> bool:
        return self._run(self._async.ping())

    def submit(self, chain: str | Path, **kwargs) -> str:
        return self._run(self._async.submit(chain, **kwargs))

    def status(self, run_id: str) -> dict | None:
        return self._run(self._async.status(run_id))

    def wait(self, run_id: str, **kwargs) -> dict:
        return self._run(self._async.wait(run_id, **kwargs))

    def cancel(self, run_id: str) -> bool:
        return self._run(self._async.cancel(run_id))

    def list_runs(self, limit: int = 20) -> list[dict]:
        return self._run(self._async.list_runs(limit))

    def get_logs(self, run_id: str, **kwargs) -> dict:
        return self._run(self._async.get_logs(run_id, **kwargs))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_record(record: dict) -> None:
    status = record.get("status", "?")
    run_id = record.get("run_id", "?")
    verdict = record.get("verdict")
    label = record.get("label")
    result_dir = record.get("result_dir", "")
    verdict_str = f"{verdict}({label})" if label else verdict or ""
    print(f"{run_id}  {status:<12}  {verdict_str or ''}")
    if result_dir:
        print(f"  results: {result_dir}")


async def _cli_async(args: argparse.Namespace) -> int:
    client = AutopilotClient()

    if args.command == "submit":
        run_id = await client.submit(
            args.chain,
            timeout=args.timeout,
            platform=args.platform,
        )
        print(f"submitted: {run_id}")

        if args.wait:
            print(f"waiting for {run_id}...", file=sys.stderr)
            record = await client.wait(run_id, timeout=args.timeout)
            _print_record(record)
            exit_codes = {"done": 0}
            return 0 if record.get("exit_code") == 0 else 1
        return 0

    if args.command == "status":
        record = await client.status(args.run_id)
        if record is None:
            print(f"not found: {args.run_id}", file=sys.stderr)
            return 1
        _print_record(record)
        return 0

    if args.command == "cancel":
        ok = await client.cancel(args.run_id)
        print("cancelled" if ok else "not found or already done")
        return 0 if ok else 1

    if args.command == "list":
        runs = await client.list_runs(limit=args.limit)
        if not runs:
            print("(no runs)")
        for r in runs:
            _print_record(r)
        return 0

    if args.command == "logs":
        info = await client.get_logs(
            args.run_id,
            file_name=args.file,
            tail=args.tail,
        )
        if "error" in info:
            print(f"error: {info['error']}", file=sys.stderr)
            return 1
        print(f"result_dir: {info.get('result_dir')}")
        for f in info.get("files", []):
            print(f"  {f['name']}  ({f['size']} bytes)")
        if "contents" in info:
            print("\n--- contents ---")
            print(info["contents"])
        return 0

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autopilot-client")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    s = sub.add_parser("submit", help="Submit a chain run")
    s.add_argument("--chain", required=True, type=Path)
    s.add_argument("--timeout", type=float, default=3600.0)
    s.add_argument("--platform", default=None)
    s.add_argument("--wait", action="store_true", help="Wait for completion")

    st = sub.add_parser("status", help="Check run status")
    st.add_argument("run_id")

    ca = sub.add_parser("cancel", help="Cancel a run")
    ca.add_argument("run_id")

    li = sub.add_parser("list", help="List recent runs")
    li.add_argument("--limit", type=int, default=20)

    lo = sub.add_parser("logs", help="Show run logs")
    lo.add_argument("run_id")
    lo.add_argument("--file", default=None, metavar="NAME")
    lo.add_argument("--tail", type=int, default=None, metavar="N")

    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        exit_code = asyncio.run(_cli_async(args))
    except ConnectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
