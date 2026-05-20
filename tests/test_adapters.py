"""
Adapter unit tests.

Tests that require no hardware:
  - engine/primitives.py: PatternOracle, CommandOracle (via MockBiStream)
  - adapters/process.py: SpawnProcessOracle, RunProcessOracle (via real subprocesses)
  - adapters/ssh.py: SSHCommandOracle (marked skip if no SSH to localhost)

Tests that require hardware (marked skip):
  - adapters/uart.py: UARTBiStream (requires /dev/ttyACM0 or similar)
  - adapters/ssh.py: connection to actual target board

Hardware tests are not skipped automatically — they are collected and
explicitly skipped with @pytest.mark.skip(reason="requires hardware: ...").
Remove the skip marker when running on the board or with a UART loopback.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from engine.oracle import Error, Matched, StreamContext, TimeoutVerdict
from engine.combinators import Timeout
from engine.primitives import CommandOracle, PatternOracle


# ---------------------------------------------------------------------------
# Helpers (shared with test_engine.py — kept local to avoid coupling)
# ---------------------------------------------------------------------------

class MockBiStream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._reader: asyncio.StreamReader | None = None

    def _get_reader(self) -> asyncio.StreamReader:
        if self._reader is None:
            self._reader = asyncio.StreamReader()
            self._reader.feed_data(self._data)
            self._reader.feed_eof()
        return self._reader

    async def read(self, n: int = 4096) -> bytes:
        return await self._get_reader().read(n)

    async def write(self, data: bytes) -> None:
        pass  # no-op for read-only streams


class EchoStream:
    """Echoes writes back as reads — used to test CommandOracle."""
    def __init__(self) -> None:
        self._q: asyncio.Queue[bytes] = asyncio.Queue()

    async def read(self, n: int = 4096) -> bytes:
        return await self._q.get()

    async def write(self, data: bytes) -> None:
        await self._q.put(data)


def make_ctx(**streams) -> StreamContext:
    return StreamContext(streams=dict(streams))


# ---------------------------------------------------------------------------
# PatternOracle
# ---------------------------------------------------------------------------

async def test_pattern_oracle_matches():
    stream = MockBiStream(b"BOOT\r\nREADY\r\n")
    ctx = make_ctx(tty0=stream)
    oracle = PatternOracle("tty0", rb"READY", label="boot_done")
    verdict, out = await oracle(ctx, 5.0)
    assert verdict == Matched("boot_done")
    assert out is ctx


async def test_pattern_oracle_eof_returns_error():
    stream = MockBiStream(b"BOOT\r\n")
    ctx = make_ctx(tty0=stream)
    oracle = PatternOracle("tty0", rb"READY")
    verdict, _ = await oracle(ctx, 5.0)
    assert isinstance(verdict, Error)
    assert "eof" in verdict.reason


async def test_pattern_oracle_buffer_overflow():
    stream = MockBiStream(b"0123456789" * 3)
    ctx = make_ctx(tty0=stream)
    oracle = PatternOracle("tty0", rb"NEVER", max_buf=20)
    verdict, _ = await oracle(ctx, 5.0)
    assert isinstance(verdict, Error)
    assert "overflow" in verdict.reason


async def test_pattern_oracle_accepts_str_pattern():
    stream = MockBiStream(b"login: ")
    ctx = make_ctx(tty0=stream)
    oracle = PatternOracle("tty0", "login:", label="login_prompt")
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("login_prompt")


async def test_pattern_oracle_timeout_via_combinator():
    """PatternOracle on an infinite stream times out via the Timeout combinator."""
    q: asyncio.Queue[bytes] = asyncio.Queue()

    class InfiniteStream:
        async def read(self, n=4096):
            await asyncio.sleep(10)
            return b"nothing"
        async def write(self, data): pass

    ctx = make_ctx(tty0=InfiniteStream())
    wrapped = Timeout(PatternOracle("tty0", rb"READY"), 0.05)
    verdict, _ = await wrapped(ctx, 0.05)
    assert isinstance(verdict, TimeoutVerdict)


async def test_pattern_oracle_does_not_mutate_ctx():
    stream = MockBiStream(b"READY\r\n")
    ctx = make_ctx(tty0=stream)
    ctx.metadata["key"] = "value"
    oracle = PatternOracle("tty0", rb"READY")
    _, returned = await oracle(ctx, 5.0)
    assert returned is ctx
    assert ctx.metadata["key"] == "value"


# ---------------------------------------------------------------------------
# CommandOracle
# ---------------------------------------------------------------------------

async def test_command_oracle_writes_then_reads():
    echo = EchoStream()
    ctx = make_ctx(tty0=echo)
    # EchoStream echoes back what we write; the response pattern is the command itself
    oracle = CommandOracle("tty0", cmd=b"uname", response_pattern=rb"uname")
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("ok")


async def test_command_oracle_uses_newline_suffix_by_default():
    received = []

    class RecordStream:
        async def read(self, n=4096):
            await asyncio.sleep(10)  # block until timeout
        async def write(self, data):
            received.append(data)

    ctx = make_ctx(tty0=RecordStream())
    oracle = Timeout(CommandOracle("tty0", cmd=b"ls", response_pattern=rb"DONE"), 0.05)
    await oracle(ctx, 0.05)
    assert received == [b"ls\n"]


# ---------------------------------------------------------------------------
# SpawnProcessOracle
# ---------------------------------------------------------------------------

async def test_spawn_process_oracle_matches_readiness():
    from adapters.process import SpawnProcessOracle

    oracle = SpawnProcessOracle(
        cmd=["bash", "-c", "echo 'ready'; sleep 30"],
        stream_name="proc0",
        ready_pattern=rb"ready",
        ready_label="proc_ready",
    )
    ctx = make_ctx()
    verdict, out = await oracle(ctx, 5.0)

    assert verdict == Matched("proc_ready")
    assert "proc0" in out.streams
    assert len(out.cleanup_hooks) == 1  # kill hook registered

    # Cleanup kills the background sleep; await process exit to avoid
    # transport cleanup warning when the event loop closes after the test.
    stream = out.streams["proc0"]
    out.cleanup()
    await asyncio.wait_for(stream.wait(), timeout=3.0)


async def test_spawn_process_oracle_registers_kill_hook_before_readiness():
    """
    Kill hook must be in ctx.cleanup_hooks even if readiness times out.
    Simulates W29: process with no readiness signal within timeout.
    """
    from adapters.process import SpawnProcessOracle

    oracle = SpawnProcessOracle(
        cmd=["sleep", "30"],          # never prints the ready signal
        stream_name="stuck_proc",
        ready_pattern=rb"NEVER_APPEARS",
        ready_label="ready",
    )
    ctx = make_ctx()
    # Wrap in a short Timeout so the readiness check fails quickly
    wrapped = Timeout(oracle, 0.15)
    verdict, out = await wrapped(ctx, 0.15)

    assert isinstance(verdict, TimeoutVerdict)
    # Even though readiness timed out, the kill hook is registered
    assert any(name == "stuck_proc" for name, _ in out.cleanup_hooks), (
        "kill cleanup_hook must be registered even when readiness times out"
    )
    # Cleanup kills the process; await exit to avoid transport warning.
    stream = out.streams["stuck_proc"]
    out.cleanup()
    await asyncio.wait_for(stream.wait(), timeout=3.0)


async def test_spawn_process_oracle_eof_before_ready():
    """Process exits before printing the ready signal → Error, not hang."""
    from adapters.process import SpawnProcessOracle

    oracle = SpawnProcessOracle(
        cmd=["echo", "not the readiness signal"],
        stream_name="short_proc",
        ready_pattern=rb"READY",
    )
    ctx = make_ctx()
    verdict, out = await oracle(ctx, 5.0)

    assert isinstance(verdict, Error)
    # Kill hook still registered (process may or may not have exited)
    assert any(name == "short_proc" for name, _ in out.cleanup_hooks)
    stream = out.streams["short_proc"]
    out.cleanup()
    # Process may have already exited (EOF triggered Error); wait with short timeout.
    try:
        await asyncio.wait_for(stream.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        pass


# ---------------------------------------------------------------------------
# RunProcessOracle
# ---------------------------------------------------------------------------

async def test_run_process_oracle_success():
    from adapters.process import RunProcessOracle

    oracle = RunProcessOracle(["echo", "hello"], success_label="done")
    ctx = make_ctx()
    verdict, out = await oracle(ctx, 5.0)
    assert verdict == Matched("done")


async def test_run_process_oracle_failure():
    from adapters.process import RunProcessOracle

    oracle = RunProcessOracle(["false"])  # always exits 1
    ctx = make_ctx()
    verdict, out = await oracle(ctx, 5.0)
    assert isinstance(verdict, Error)
    assert "exit=1" in verdict.reason


async def test_run_process_oracle_captures_stdout():
    from adapters.process import RunProcessOracle

    oracle = RunProcessOracle(
        ["echo", "captured output"],
        capture_name="output",
        success_label="done",
    )
    ctx = make_ctx()
    verdict, out = await oracle(ctx, 5.0)
    assert verdict == Matched("done")
    assert b"captured output" in out.metadata["output"]


async def test_run_process_oracle_timeout():
    from adapters.process import RunProcessOracle

    # Call oracle directly with a short timeout — the internal asyncio.wait_for
    # fires and returns Error("process_timeout"). Do NOT wrap in Timeout combinator
    # here: both would have the same deadline, creating a race between them.
    oracle = RunProcessOracle(["sleep", "30"])
    ctx = make_ctx()
    verdict, _ = await oracle(ctx, 0.1)
    assert isinstance(verdict, Error)
    assert "timeout" in verdict.reason


async def test_run_process_oracle_nonzero_with_failure_label():
    from adapters.process import RunProcessOracle

    oracle = RunProcessOracle(["false"], failure_label="failed")
    ctx = make_ctx()
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("failed")


# ---------------------------------------------------------------------------
# UART tests (require hardware)
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="requires hardware: UART device /dev/ttyACM0")
async def test_uart_open_and_read():
    from adapters.uart import open_uart
    stream = await open_uart("/dev/ttyACM0", baudrate=115200)
    ctx = StreamContext(streams={"tty0": stream})
    ctx.register_cleanup("tty0", stream.close)

    chunk = await asyncio.wait_for(stream.read(64), timeout=2.0)
    assert isinstance(chunk, bytes)
    ctx.cleanup()


@pytest.mark.skip(reason="requires hardware: UART device /dev/ttyACM0")
async def test_uart_write_and_read_loopback():
    """Requires a UART loopback cable (TX connected to RX)."""
    from adapters.uart import open_uart
    stream = await open_uart("/dev/ttyACM0", baudrate=115200)
    try:
        await stream.write(b"PING\n")
        data = await asyncio.wait_for(stream.read(64), timeout=1.0)
        assert b"PING" in data
    finally:
        stream.close()


# ---------------------------------------------------------------------------
# SSH tests (skipped without live target)
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="requires SSH target: set TARGET_IP env var and remove skip")
async def test_ssh_command_oracle_success():
    import os
    from adapters.ssh import SSHCommandOracle

    host = os.environ.get("TARGET_IP", "192.168.1.1")
    oracle = SSHCommandOracle(host, "echo ok", success_label="ok")
    ctx = make_ctx()
    verdict, _ = await oracle(ctx, 10.0)
    assert verdict == Matched("ok")


@pytest.mark.skip(reason="requires SSH target: set TARGET_IP env var and remove skip")
async def test_ssh_upload_oracle():
    import os
    from pathlib import Path
    from adapters.ssh import SSHUploadOracle

    host = os.environ.get("TARGET_IP", "192.168.1.1")
    src = Path("/tmp/autopilot_test_upload.txt")
    src.write_text("test")
    oracle = SSHUploadOracle(host, src, "/tmp/autopilot_test_upload.txt")
    ctx = make_ctx()
    verdict, _ = await oracle(ctx, 10.0)
    assert verdict == Matched("uploaded")
