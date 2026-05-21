"""
VCMux adapter: in-process 0xfe frame parser for seL4/CAmkES virtual channel mux.

VCMux is a byte-stream multiplexer. A single physical UART carries multiple
named virtual channels (vm0_guest_console_sink, vm1_guest_console_sink, etc.)
separated by 0xfe escape sequences.

The old system runs an external C binary (vcmuxer) which handles parsing and
creates PTY file descriptors. This adapter implements the same parser in Python,
eliminating the subprocess dependency and enabling in-process demultiplexing.

Protocol — inner layer (0xfe escape):
  0xfe <stream_id>                             switch active stream
  0xfe 0x00                                    clear active stream (→ default)
  0xfe 0xfe                                    literal 0xfe in payload
  0xfe 0xfd <type> ...                         control frame
  0xfe 0xfd 0x02 <len_hi> <len_lo> <json>     stream registry

Protocol — outer layer (NVIDIA TCU, optional, 0xff escape):
  0xff <tag>    set active tag (0xe1 = CCPLEX → inner VCMux)
  0xff 0xff     literal 0xff
  0xff 0xfd     reset outer state

Backpressure: per-channel queues use put_nowait with overflow logging.
The pump loop must not stall on one slow channel while other channels
continue producing (head-of-line blocking). Bytes are dropped rather
than blocking the UART read path if a channel's consumer falls behind.

Write path: VCMuxBiStream.write() encodes 0xfe-framed data and writes
to the shared raw BiStream. Concurrent writes are serialised by an
asyncio.Lock (multiple VCMuxBiStream instances share one raw stream).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Callable

import structlog

from engine.oracle import Error, Matched, StreamContext, Verdict

log = structlog.get_logger()

# Escape bytes (inner VCMux layer)
_ESC = 0xFE
_ESC_CLEAR = 0x00    # 0xfe 0x00 → clear active stream
_ESC_CTRL = 0xFD     # 0xfe 0xfd → control frame
_ESC_ESC = 0xFE      # 0xfe 0xfe → literal 0xfe

# Control frame types (after 0xfe 0xfd)
_CTRL_REGISTRY = 0x02
_CTRL_ANNOUNCE_CONNECTED = 0x01
_CTRL_ANNOUNCE_DISCONNECTED = 0x04

# Outer layer (NVIDIA TCU)
_OUTER_ESC = 0xFF
_OUTER_CCPLEX_TAG = 0xE1
_OUTER_RESET = 0xFD


# ---------------------------------------------------------------------------
# VCMuxParser: byte-level state machine
# ---------------------------------------------------------------------------

class VCMuxParser:
    """
    Byte-level VCMux 0xfe state machine.

    Call feed(byte) for each byte from the raw stream. Parsed bytes are
    dispatched to per-channel queues registered via add_channel().

    The default channel (stream_id=None) receives bytes when no virtual channel
    is active — corresponds to raw_ccplex / physical_uart_default in the C code.

    on_registry(registry_dict) is called when a stream_registry control frame
    is fully parsed. The caller uses this to create VCMuxBiStream instances
    for the announced channels.
    """

    # Parser states
    _S_IDLE = 0           # no active stream, routing to default
    _S_STREAM = 1         # routing bytes to active stream
    _S_ESC = 2            # saw 0xfe, waiting for command byte
    _S_CTRL_CMD = 3       # saw 0xfe 0xfd, waiting for control type
    _S_CTRL_LEN_HI = 4    # registry/announce: reading length high byte
    _S_CTRL_LEN_LO = 5    # registry/announce: reading length low byte
    _S_CTRL_JSON = 6      # registry: reading JSON body

    def __init__(
        self,
        on_registry: Callable[[dict], None] | None = None,
        queue_maxsize: int = 8192,
    ) -> None:
        self._on_registry = on_registry
        self._queue_maxsize = queue_maxsize

        # stream_id (int) → asyncio.Queue
        self._channels: dict[int | None, asyncio.Queue[bytes | None]] = {}
        self._default_queue: asyncio.Queue[bytes | None] | None = None

        # Parser state
        self._state = self._S_IDLE
        self._active_stream: int | None = None  # current stream_id

        # Control frame accumulation
        self._ctrl_type: int = 0
        self._ctrl_len: int = 0
        self._ctrl_buf = bytearray()

    def add_channel(self, stream_id: int) -> asyncio.Queue[bytes | None]:
        """Register a channel and return its queue."""
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._channels[stream_id] = q
        return q

    def add_default_channel(self) -> asyncio.Queue[bytes | None]:
        """Register the default channel (bytes before any stream switch)."""
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._default_queue = q
        return q

    def eof(self) -> None:
        """Signal EOF to all registered channels."""
        for q in self._channels.values():
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if self._default_queue is not None:
            try:
                self._default_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def feed(self, byte: int) -> None:
        """Feed one raw byte through the state machine."""
        s = self._state

        if s == self._S_ESC:
            self._state = self._S_STREAM if self._active_stream is not None else self._S_IDLE
            if byte == _ESC_ESC:          # literal 0xfe
                self._dispatch(bytes([_ESC]))
            elif byte == _ESC_CLEAR:      # 0xfe 0x00 → clear active stream
                self._active_stream = None
                self._state = self._S_IDLE
            elif byte == _ESC_CTRL:       # 0xfe 0xfd → control frame
                self._state = self._S_CTRL_CMD
            else:                         # 0xfe <id> → switch stream
                self._active_stream = byte
                self._state = self._S_STREAM

        elif s in (self._S_IDLE, self._S_STREAM):
            if byte == _ESC:
                self._state = self._S_ESC
            else:
                self._dispatch(bytes([byte]))

        elif s == self._S_CTRL_CMD:
            self._ctrl_type = byte
            self._ctrl_len = 0
            self._ctrl_buf = bytearray()
            if byte in (_CTRL_REGISTRY, _CTRL_ANNOUNCE_CONNECTED, _CTRL_ANNOUNCE_DISCONNECTED):
                self._state = self._S_CTRL_LEN_HI
            else:
                # Unknown control type — skip back to idle
                self._state = self._S_IDLE

        elif s == self._S_CTRL_LEN_HI:
            self._ctrl_len = byte << 8
            self._state = self._S_CTRL_LEN_LO

        elif s == self._S_CTRL_LEN_LO:
            self._ctrl_len |= byte
            if self._ctrl_len == 0:
                self._finish_control_frame()
            else:
                self._state = self._S_CTRL_JSON

        elif s == self._S_CTRL_JSON:
            self._ctrl_buf.append(byte)
            if len(self._ctrl_buf) >= self._ctrl_len:
                self._finish_control_frame()

    def _dispatch(self, data: bytes) -> None:
        """Route bytes to the active channel's queue (or default queue)."""
        if self._active_stream is not None and self._active_stream in self._channels:
            q = self._channels[self._active_stream]
        elif self._active_stream is None and self._default_queue is not None:
            q = self._default_queue
        else:
            return  # channel not registered — silently discard
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            log.warning(
                "vcmux.channel_overflow",
                stream_id=self._active_stream,
                dropped=len(data),
            )

    def _finish_control_frame(self) -> None:
        self._state = self._S_IDLE
        if self._ctrl_type != _CTRL_REGISTRY:
            # ANNOUNCE frames are informational — we rely on the registry
            return
        try:
            registry = json.loads(self._ctrl_buf.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            log.warning("vcmux.registry_parse_error", error=repr(exc))
            return
        log.info("vcmux.stream_registry_received", streams=len(registry.get("streams", [])))
        if self._on_registry is not None:
            self._on_registry(registry)


# ---------------------------------------------------------------------------
# NVIDIA TCU outer filter
# ---------------------------------------------------------------------------

class NvidiaTCUFilter:
    """
    Strip the optional NVIDIA TCU 0xff outer framing.

    Only bytes tagged as CCPLEX (0xe1) are forwarded to the inner VCMuxParser.
    Other tag groups (BPMP, SatMC, etc.) are discarded.

    Operates as a thin shim: call feed(byte) to process outer bytes;
    inner bytes are forwarded to the wrapped VCMuxParser.
    """

    _S_DATA = 0   # normal data (pass-through or discard based on tag)
    _S_ESC = 1    # saw 0xff, waiting for tag/command

    def __init__(
        self,
        inner: VCMuxParser,
        ccplex_tag: int = _OUTER_CCPLEX_TAG,
    ) -> None:
        self._inner = inner
        self._ccplex_tag = ccplex_tag
        self._state = self._S_DATA
        self._active_tag: int | None = None

    def feed(self, byte: int) -> None:
        if self._state == self._S_ESC:
            self._state = self._S_DATA
            if byte == _OUTER_ESC:        # literal 0xff
                if self._active_tag == self._ccplex_tag:
                    self._inner.feed(_OUTER_ESC)
            elif byte == _OUTER_RESET:    # 0xfd → reset
                self._active_tag = None
            else:                         # tag byte
                self._active_tag = byte
        else:
            if byte == _OUTER_ESC:
                self._state = self._S_ESC
            elif self._active_tag == self._ccplex_tag:
                self._inner.feed(byte)

    def eof(self) -> None:
        self._inner.eof()


# ---------------------------------------------------------------------------
# VCMuxBiStream: per-channel read/write
# ---------------------------------------------------------------------------

class VCMuxBiStream:
    """
    BiStream for one virtual channel inside a VCMux multiplexed connection.

    Reads demultiplexed bytes from the channel's asyncio.Queue.
    Writes are 0xfe-encoded and sent to the shared raw stream.

    EOF is signalled by None in the queue (parser called eof() or was cancelled).
    After EOF, read() returns b"" on subsequent calls.

    The write_lock is shared across all VCMuxBiStream instances that reference
    the same raw stream — it serialises concurrent writes from multiple channels.
    """

    def __init__(
        self,
        stream_id: int,
        queue: asyncio.Queue[bytes | None],
        raw: object,           # BiStream (raw UART/SSH)
        write_lock: asyncio.Lock,
        *,
        nvidia_tcu: bool = False,
        nvidia_tag: int = _OUTER_CCPLEX_TAG,
    ) -> None:
        self._stream_id = stream_id
        self._q = queue
        self._raw = raw
        self._lock = write_lock
        self._nvidia_tcu = nvidia_tcu
        self._nvidia_tag = nvidia_tag
        self._eof = False

    async def read(self, n: int = 4096) -> bytes:
        if self._eof:
            return b""
        chunk = await self._q.get()
        if chunk is None:
            self._eof = True
            return b""
        return chunk

    async def write(self, data: bytes) -> None:
        frame = _encode_frame(self._stream_id, data)
        if self._nvidia_tcu:
            frame = _encode_nvidia_tcu(self._nvidia_tag, frame)
        async with self._lock:
            await self._raw.write(frame)


def _encode_frame(stream_id: int, data: bytes) -> bytes:
    """Encode data as 0xfe-framed VCMux write."""
    buf = bytearray()
    buf.append(_ESC)
    buf.append(stream_id)
    for byte in data:
        if byte == _ESC:
            buf.append(_ESC)
            buf.append(_ESC_ESC)
        else:
            buf.append(byte)
    buf.append(_ESC)
    buf.append(_ESC_CLEAR)
    return bytes(buf)


def _encode_nvidia_tcu(tag: int, data: bytes) -> bytes:
    """Wrap data in NVIDIA TCU 0xff outer framing."""
    buf = bytearray()
    buf.append(_OUTER_ESC)
    buf.append(tag)
    for byte in data:
        if byte == _OUTER_ESC:
            buf.append(_OUTER_ESC)
            buf.append(_OUTER_ESC)
        else:
            buf.append(byte)
    return bytes(buf)


# ---------------------------------------------------------------------------
# VCMuxSourceOracle
# ---------------------------------------------------------------------------

class VCMuxSourceOracle:
    """
    Connect a raw BiStream to the in-process VCMux parser.

    The oracle:
    1. Reads raw_stream_name from ctx.streams (typically a UARTBiStream)
    2. Creates a VCMuxParser (with optional NvidiaTCU outer filter)
    3. Starts a pump task bridging the raw stream into the parser
    4. Waits for the stream_registry control frame (announces channel names)
    5. Registers a VCMuxBiStream for each channel listed in registry_streams
    6. Returns Matched("vcmux_ready")

    registry_streams: list of component names to register as streams in ctx.
    If None, registers all output/bidirectional streams from the registry.

    stream_prefix: optional prefix prepended to ctx stream names.
    For example, prefix="orin/" and component="vm0_guest_console_sink" →
    ctx.streams["orin/vm0_guest_console_sink"].

    The pump task is registered as a cleanup hook so it is cancelled on teardown.
    """

    def __init__(
        self,
        raw_stream_name: str,
        *,
        nvidia_tcu: bool = False,
        nvidia_tag: int = _OUTER_CCPLEX_TAG,
        registry_streams: list[str] | None = None,
        stream_prefix: str = "",
        registry_timeout: float = 30.0,
        include_default: bool = False,
        queue_maxsize: int = 8192,
        preprocess: bool = True,
    ) -> None:
        self._raw_name = raw_stream_name
        self._nvidia_tcu = nvidia_tcu
        self._nvidia_tag = nvidia_tag
        self._registry_streams = registry_streams
        self._prefix = stream_prefix
        self._registry_timeout = registry_timeout
        self._include_default = include_default
        self._queue_maxsize = queue_maxsize
        self._preprocess = preprocess

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        raw = ctx.streams.get(self._raw_name)
        if raw is None:
            return Error(f"vcmux: stream '{self._raw_name}' not in ctx"), ctx

        registry_event: asyncio.Event = asyncio.Event()
        registry_result: list[dict] = []   # mutated by callback

        def on_registry(reg: dict) -> None:
            registry_result.append(reg)
            registry_event.set()

        parser = VCMuxParser(on_registry=on_registry, queue_maxsize=self._queue_maxsize)

        if self._include_default:
            default_q = parser.add_default_channel()

        # Outer filter or direct parser
        feeder: VCMuxParser | NvidiaTCUFilter
        if self._nvidia_tcu:
            feeder = NvidiaTCUFilter(parser, ccplex_tag=self._nvidia_tag)
        else:
            feeder = parser

        log.info("vcmux.starting", raw=self._raw_name, nvidia_tcu=self._nvidia_tcu)

        pump_task = asyncio.create_task(
            _pump_raw_to_parser(raw, feeder),
            name=f"vcmux_pump_{self._raw_name}",
        )
        ctx.register_cleanup(self._raw_name, pump_task.cancel)

        # Wait for stream registry
        deadline = min(timeout, self._registry_timeout)
        try:
            await asyncio.wait_for(registry_event.wait(), timeout=deadline)
        except asyncio.TimeoutError:
            return Error("vcmux: timed out waiting for stream registry"), ctx

        registry = registry_result[0]
        streams = registry.get("streams", [])

        # Filter to requested channels (output or bidirectional)
        want = set(self._registry_streams) if self._registry_streams is not None else None
        write_lock = asyncio.Lock()

        registered: list[str] = []
        for s in streams:
            name: str = s.get("component", "")
            stream_id: int | None = s.get("stream_id")
            direction: str = s.get("direction", "")

            if not name or stream_id is None:
                continue
            if want is not None and name not in want:
                continue
            if want is None and direction not in ("output", "bidirectional", "input"):
                continue

            q = parser.add_channel(stream_id)
            bio: object = VCMuxBiStream(
                stream_id, q, raw, write_lock,
                nvidia_tcu=self._nvidia_tcu,
                nvidia_tag=self._nvidia_tag,
            )
            if self._preprocess:
                from engine.primitives import FilterBiStream
                bio = FilterBiStream(bio)
            ctx_name = f"{self._prefix}{name}"
            ctx.streams[ctx_name] = bio
            registered.append(ctx_name)
            log.info("vcmux.channel_registered", name=ctx_name, stream_id=stream_id)

        if self._include_default:
            default_bio: object = VCMuxBiStream(
                0, default_q, raw, write_lock,
                nvidia_tcu=self._nvidia_tcu,
                nvidia_tag=self._nvidia_tag,
            )
            if self._preprocess:
                from engine.primitives import FilterBiStream
                default_bio = FilterBiStream(default_bio)
            ctx_name = f"{self._prefix}default"
            ctx.streams[ctx_name] = default_bio
            registered.append(ctx_name)

        log.info("vcmux.ready", streams=registered)
        return Matched("vcmux_ready"), ctx


async def _pump_raw_to_parser(
    raw: object,
    feeder: VCMuxParser | NvidiaTCUFilter,
) -> None:
    """
    Bridge raw BiStream into the VCMux parser byte by byte.

    Runs as a background task. Reads chunks from the raw stream and feeds
    individual bytes to the parser state machine. On EOF or cancellation,
    signals EOF to all channel queues.
    """
    try:
        while True:
            chunk: bytes = await raw.read(4096)
            if not chunk:
                break
            for byte in chunk:
                feeder.feed(byte)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("vcmux.pump_error", error=repr(exc))
    finally:
        feeder.eof()
