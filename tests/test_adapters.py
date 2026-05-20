"""
Adapter unit tests.

Tests that require no hardware:
  - engine/primitives.py: PatternOracle, CommandOracle (via MockBiStream)
  - adapters/process.py: SpawnProcessOracle, RunProcessOracle (via real subprocesses)
  - adapters/vcmux.py: VCMuxParser, NvidiaTCUFilter, VCMuxBiStream
  - adapters/robot.py: parse_rf_output, RobotFrameworkOracle (via real subprocess)
  - adapters/interactive.py: ConsoleBridge, InteractiveOracle
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
import json
import re
import textwrap
from pathlib import Path

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


# ---------------------------------------------------------------------------
# VCMuxParser
# ---------------------------------------------------------------------------

async def test_vcmux_parser_routes_to_channel():
    from adapters.vcmux import VCMuxParser

    parser = VCMuxParser()
    q = parser.add_channel(2)

    # 0xfe 0x02 switches to channel 2; "hello" is payload; 0xfe 0x00 clears
    raw = bytes([0xFE, 0x02]) + b"hello" + bytes([0xFE, 0x00])
    for b in raw:
        parser.feed(b)

    # All five bytes should be in the queue
    chunks = []
    while not q.empty():
        chunks.append(await q.get())
    assert b"".join(chunks) == b"hello"


async def test_vcmux_parser_escapes_literal_0xfe():
    from adapters.vcmux import VCMuxParser

    parser = VCMuxParser()
    q = parser.add_channel(1)

    # 0xfe 0x01 → channel 1; 0xfe 0xfe → literal 0xfe; 0xfe 0x00 → clear
    raw = bytes([0xFE, 0x01, 0xFE, 0xFE, 0xFE, 0x00])
    for b in raw:
        parser.feed(b)

    chunks = []
    while not q.empty():
        chunks.append(await q.get())
    assert b"".join(chunks) == b"\xfe"


async def test_vcmux_parser_routes_to_default():
    from adapters.vcmux import VCMuxParser

    parser = VCMuxParser()
    q = parser.add_default_channel()

    # Bytes with no prior stream switch go to default
    for b in b"raw":
        parser.feed(b)

    chunks = []
    while not q.empty():
        chunks.append(await q.get())
    assert b"".join(chunks) == b"raw"


async def test_vcmux_parser_stream_registry_callback():
    from adapters.vcmux import VCMuxParser

    received = []
    parser = VCMuxParser(on_registry=received.append)

    registry_json = json.dumps({
        "streams": [
            {"component": "vm0_console", "stream_id": 2, "direction": "output"},
        ]
    }).encode()
    length = len(registry_json)
    ctrl_frame = bytes([
        0xFE, 0xFD,          # escape + control
        0x02,                # STREAM_REGISTRY
        (length >> 8) & 0xFF,
        length & 0xFF,
    ]) + registry_json

    for b in ctrl_frame:
        parser.feed(b)

    assert len(received) == 1
    assert received[0]["streams"][0]["component"] == "vm0_console"


async def test_vcmux_parser_eof_signals_all_channels():
    from adapters.vcmux import VCMuxParser

    parser = VCMuxParser()
    q1 = parser.add_channel(1)
    q2 = parser.add_channel(2)
    parser.eof()

    assert await q1.get() is None
    assert await q2.get() is None


async def test_vcmux_parser_unregistered_channel_discarded():
    """Bytes for an unregistered channel ID are silently dropped."""
    from adapters.vcmux import VCMuxParser

    parser = VCMuxParser()
    # Only channel 1 registered; bytes for channel 2 should be dropped
    q = parser.add_channel(1)

    raw = bytes([0xFE, 0x02]) + b"ignored" + bytes([0xFE, 0x01]) + b"kept" + bytes([0xFE, 0x00])
    for b in raw:
        parser.feed(b)

    chunks = []
    while not q.empty():
        chunks.append(await q.get())
    assert b"".join(chunks) == b"kept"


# ---------------------------------------------------------------------------
# NvidiaTCUFilter
# ---------------------------------------------------------------------------

async def test_nvidia_tcu_filter_passes_ccplex_bytes():
    from adapters.vcmux import NvidiaTCUFilter, VCMuxParser

    parser = VCMuxParser()
    q = parser.add_channel(3)
    filt = NvidiaTCUFilter(parser, ccplex_tag=0xE1)

    # 0xff 0xe1 → CCPLEX tag; then inner VCMux: 0xfe 0x03 + "hi" + 0xfe 0x00
    outer = bytes([0xFF, 0xE1, 0xFE, 0x03]) + b"hi" + bytes([0xFE, 0x00])
    for b in outer:
        filt.feed(b)

    chunks = []
    while not q.empty():
        chunks.append(await q.get())
    assert b"".join(chunks) == b"hi"


async def test_nvidia_tcu_filter_discards_other_tags():
    from adapters.vcmux import NvidiaTCUFilter, VCMuxParser

    parser = VCMuxParser()
    default_q = parser.add_default_channel()
    filt = NvidiaTCUFilter(parser, ccplex_tag=0xE1)

    # 0xff 0xE2 → non-CCPLEX tag; bytes should be discarded
    outer = bytes([0xFF, 0xE2]) + b"garbage" + bytes([0xFF, 0xE1]) + b"ok"
    for b in outer:
        filt.feed(b)

    # Default queue should only have "ok"
    chunks = []
    while not default_q.empty():
        chunks.append(await default_q.get())
    assert b"".join(chunks) == b"ok"


# ---------------------------------------------------------------------------
# VCMuxBiStream write encoding
# ---------------------------------------------------------------------------

async def test_vcmux_bistream_write_encodes_frame():
    from adapters.vcmux import VCMuxBiStream, _encode_frame

    frame = _encode_frame(stream_id=2, data=b"hello")
    # [0xfe 0x02] [h e l l o] [0xfe 0x00]
    assert frame == bytes([0xFE, 0x02]) + b"hello" + bytes([0xFE, 0x00])


async def test_vcmux_bistream_write_escapes_0xfe_in_payload():
    from adapters.vcmux import _encode_frame

    frame = _encode_frame(stream_id=1, data=b"\xfe")
    # [0xfe 0x01] [0xfe 0xfe] [0xfe 0x00]
    assert frame == bytes([0xFE, 0x01, 0xFE, 0xFE, 0xFE, 0x00])


async def test_vcmux_bistream_write_sends_to_raw():
    from adapters.vcmux import VCMuxBiStream

    sent = []

    class FakeRaw:
        async def read(self, n=4096): return b""
        async def write(self, data): sent.append(data)

    lock = asyncio.Lock()
    q: asyncio.Queue[bytes | None] = asyncio.Queue()
    bio = VCMuxBiStream(stream_id=3, queue=q, raw=FakeRaw(), write_lock=lock)
    await bio.write(b"test")

    assert len(sent) == 1
    # Frame: 0xfe 0x03 "test" 0xfe 0x00
    assert sent[0] == bytes([0xFE, 0x03]) + b"test" + bytes([0xFE, 0x00])


# ---------------------------------------------------------------------------
# VCMuxSourceOracle (integration)
# ---------------------------------------------------------------------------

async def test_vcmux_source_oracle_registers_streams():
    """VCMuxSourceOracle: parse registry frame from raw stream, register channels in ctx."""
    from adapters.vcmux import VCMuxSourceOracle

    registry = {
        "streams": [
            {"component": "vm0_console", "stream_id": 2, "direction": "output"},
            {"component": "vm1_console", "stream_id": 3, "direction": "output"},
        ]
    }
    registry_json = json.dumps(registry).encode()
    length = len(registry_json)
    ctrl_frame = bytes([0xFE, 0xFD, 0x02, (length >> 8) & 0xFF, length & 0xFF]) + registry_json

    class OneShotRaw:
        def __init__(self, data: bytes):
            self._data = data
            self._sent = False
        async def read(self, n=4096):
            if not self._sent:
                self._sent = True
                return self._data
            await asyncio.sleep(100)  # stall after sending all data
            return b""
        async def write(self, data): pass

    ctx = make_ctx(uart=OneShotRaw(ctrl_frame))
    oracle = VCMuxSourceOracle("uart", registry_timeout=5.0)
    verdict, out = await oracle(ctx, 5.0)

    assert verdict == Matched("vcmux_ready")
    assert "vm0_console" in out.streams
    assert "vm1_console" in out.streams
    # pump task cleanup hook registered
    assert any(name == "uart" for name, _ in out.cleanup_hooks)
    out.cleanup()


async def test_vcmux_source_oracle_timeout_on_no_registry():
    """VCMuxSourceOracle returns Error if registry never arrives."""
    from adapters.vcmux import VCMuxSourceOracle

    class SilentRaw:
        async def read(self, n=4096):
            await asyncio.sleep(10)
            return b""
        async def write(self, data): pass

    ctx = make_ctx(uart=SilentRaw())
    oracle = VCMuxSourceOracle("uart", registry_timeout=0.1)
    verdict, out = await oracle(ctx, 5.0)

    assert isinstance(verdict, Error)
    assert "registry" in verdict.reason
    out.cleanup()


# ---------------------------------------------------------------------------
# parse_rf_output
# ---------------------------------------------------------------------------

async def test_parse_rf_output_pass(tmp_path):
    from adapters.robot import parse_rf_output

    xml = tmp_path / "output.xml"
    xml.write_text(textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <robot>
          <suite name="MySuite">
            <status status="PASS"/>
          </suite>
          <statistics>
            <total>
              <stat pass="3" fail="0">All Tests</stat>
            </total>
          </statistics>
        </robot>
    """))

    label, summary = parse_rf_output(xml)
    assert label == "rf_pass"
    assert summary["tests_passed"] == 3
    assert summary["tests_failed"] == 0


async def test_parse_rf_output_fail(tmp_path):
    from adapters.robot import parse_rf_output

    xml = tmp_path / "output.xml"
    xml.write_text(textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <robot>
          <suite name="MySuite">
            <status status="FAIL"/>
          </suite>
          <statistics>
            <total>
              <stat pass="1" fail="2">All Tests</stat>
            </total>
          </statistics>
        </robot>
    """))

    label, summary = parse_rf_output(xml)
    assert label == "rf_fail"
    assert summary["tests_failed"] == 2


async def test_parse_rf_output_bad_xml(tmp_path):
    from adapters.robot import parse_rf_output

    xml = tmp_path / "output.xml"
    xml.write_text("not xml at all")

    with pytest.raises(ValueError, match="xml_parse_error"):
        parse_rf_output(xml)


# ---------------------------------------------------------------------------
# RobotFrameworkOracle
# ---------------------------------------------------------------------------

async def test_rf_oracle_pass(tmp_path):
    """RobotFrameworkOracle: run a trivial .robot file that passes."""
    pytest.importorskip("robot")

    suite = tmp_path / "pass.robot"
    suite.write_text(textwrap.dedent("""\
        *** Test Cases ***
        Always Pass
            Log    hello
    """))

    from adapters.robot import RobotFrameworkOracle

    oracle = RobotFrameworkOracle(suite, outputdir=tmp_path / "results")
    ctx = make_ctx()
    verdict, out = await oracle(ctx, 30.0)

    assert verdict == Matched("rf_pass")
    assert out.metadata["rf_verdict"]["tests_passed"] >= 1


async def test_rf_oracle_fail(tmp_path):
    """RobotFrameworkOracle: run a .robot file that fails."""
    pytest.importorskip("robot")

    suite = tmp_path / "fail.robot"
    suite.write_text(textwrap.dedent("""\
        *** Test Cases ***
        Always Fail
            Fail    intentional failure
    """))

    from adapters.robot import RobotFrameworkOracle

    oracle = RobotFrameworkOracle(suite, outputdir=tmp_path / "results")
    ctx = make_ctx()
    verdict, out = await oracle(ctx, 30.0)

    assert verdict == Matched("rf_fail")
    assert out.metadata["rf_verdict"]["tests_failed"] >= 1


async def test_rf_oracle_no_robot_binary():
    """RobotFrameworkOracle with a nonexistent binary returns Error."""
    from adapters.robot import RobotFrameworkOracle

    oracle = RobotFrameworkOracle(
        "/nonexistent/suite.robot",
        robot_cmd="/no/such/robot",
    )
    ctx = make_ctx()
    verdict, _ = await oracle(ctx, 5.0)

    assert isinstance(verdict, Error)
    assert "spawn_failed" in verdict.reason


# ---------------------------------------------------------------------------
# ConsoleBridge
# ---------------------------------------------------------------------------

async def test_console_bridge_send_recv():
    from adapters.interactive import ConsoleBridge

    bridge = ConsoleBridge(session_id="test")
    await bridge.write_q.put(b"hello")
    item = await bridge.write_q.get()
    assert item == b"hello"


async def test_console_bridge_signal_done():
    from adapters.interactive import ConsoleBridge

    bridge = ConsoleBridge(session_id="test")
    assert not bridge.done.is_set()
    bridge.signal_done()
    assert bridge.done.is_set()


# ---------------------------------------------------------------------------
# InteractiveOracle
# ---------------------------------------------------------------------------

async def test_interactive_oracle_completes_on_done_signal():
    """InteractiveOracle returns Matched when done.set() is called externally."""
    from adapters.interactive import InteractiveOracle, get_session

    class BlockingStream:
        async def read(self, n=4096):
            await asyncio.sleep(100)
            return b""
        async def write(self, data): pass

    ctx = make_ctx(tty0=BlockingStream())
    oracle = InteractiveOracle("tty0", session_id="test_session")

    async def signal_after_short_delay():
        await asyncio.sleep(0.05)
        session = get_session("test_session")
        assert session is not None
        session.signal_done()

    trigger = asyncio.create_task(signal_after_short_delay())
    verdict, out = await oracle(ctx, 10.0)
    await trigger

    assert verdict == Matched("interactive_done")
    # Session cleaned up from registry
    assert get_session("test_session") is None


async def test_interactive_oracle_relays_output_to_read_q():
    """InteractiveOracle pump_out relays target→read_q before session ends."""
    from adapters.interactive import InteractiveOracle, get_session

    output = [b"line1\n", b"line2\n"]

    class SequencedStream:
        def __init__(self):
            self._idx = 0
        async def read(self, n=4096):
            if self._idx < len(output):
                chunk = output[self._idx]
                self._idx += 1
                return chunk
            await asyncio.sleep(100)
            return b""
        async def write(self, data): pass

    ctx = make_ctx(tty0=SequencedStream())
    oracle = InteractiveOracle("tty0", session_id="test_relay")

    received = []

    async def consumer():
        await asyncio.sleep(0.02)
        session = get_session("test_relay")
        assert session is not None
        # Drain up to 2 items then signal done
        for _ in range(2):
            item = await asyncio.wait_for(session.read_q.get(), timeout=1.0)
            if item is not None:
                received.append(item)
        session.signal_done()

    consumer_task = asyncio.create_task(consumer())
    verdict, _ = await oracle(ctx, 5.0)
    await consumer_task

    assert b"".join(received) == b"line1\nline2\n"


async def test_interactive_oracle_missing_stream_returns_error():
    from adapters.interactive import InteractiveOracle

    ctx = make_ctx()  # no streams
    oracle = InteractiveOracle("tty0")
    verdict, _ = await oracle(ctx, 5.0)

    assert isinstance(verdict, Error)
    assert "tty0" in verdict.reason


async def test_session_registry_populated_and_cleared():
    from adapters.interactive import InteractiveOracle, list_sessions, get_session

    class BlockingStream:
        async def read(self, n=4096):
            await asyncio.sleep(100)
            return b""
        async def write(self, data): pass

    ctx = make_ctx(tty0=BlockingStream())
    oracle = InteractiveOracle("tty0", session_id="reg_test")

    async def check_and_close():
        await asyncio.sleep(0.02)
        assert "reg_test" in list_sessions()
        get_session("reg_test").signal_done()

    task = asyncio.create_task(check_and_close())
    await oracle(ctx, 5.0)
    await task

    assert "reg_test" not in list_sessions()
