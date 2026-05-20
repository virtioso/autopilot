# VCMux Transport: Options for the Rewrite

## What Actually Exists

There are two separate mux paths with different characteristics. They should be treated as independent design questions.

### CAmkES/seL4 Path (UART-based)

```
ConsoleMux (CAmkES) → VCMux frames over UART → vcmuxer (C binary, host) → PTYs → Autopilot
```

- Transport: physical UART (`/dev/ttyACM1`) or virtual serial
- Wire protocol: VCMux — 0xfe escape framing, stream registry via control frames
- Stream discovery: vcmuxer emits JSON on stdout (`session_open` events); registry announced by CAmkES at startup
- CAmkES side: `ConsoleMux` component multiplexes all component output; `GuestConsoleSink` bridges VM output to ConsoleMux
- Bidirectional: partially — downlink ACK mechanism exists but write path is limited

### Isengard/Docker Path (Linux-based)

```
virtioso-muxd (Rust daemon on target) → Unix socket clients → [planned: Zenoh bridge] → Autopilot
```

- Transport: `virtioso-muxd` on target Linux allocates Unix socket per client, multiplexes onto UART
- Stream discovery: CTRL_CONNECTED / CTRL_DISCONNECTED frames from virtioso-muxd; Zenoh bridge planned but not implemented
- This is a Linux userspace daemon — cannot run on seL4 bare-metal

**Zenoh is not applicable to the CAmkES/seL4 path.** seL4 bare-metal has no networking stack; there is nowhere to run a Zenoh router on the CAmkES side. Adding Zenoh here would require a host-side bridge after vcmuxer, adding a layer without removing the existing one.

---

## CAmkES Path: Three Options for the Rewrite

### Option A: Keep vcmuxer subprocess + wrap PTYs as BiStreams

Continue spawning vcmuxer as a subprocess. The Source oracle:
1. Spawns `vcmuxer -A -O raw -d /dev/ttyACM1 ...`
2. Reads JSON `session_open` events from vcmuxer's stdout
3. Opens each PTY path as a file descriptor
4. Wraps each fd as a `BiStream` using `os.read`/`os.write` via `asyncio.add_reader`
5. Registers in StreamContext under the stream name

```python
class VcmuxerSourceOracle:
    async def __call__(self, ctx: StreamContext, timeout: float):
        proc = await asyncio.create_subprocess_exec(
            "vcmuxer", "-A", "-O", "raw", "-d", ctx.uart_device, ...,
            stdout=asyncio.subprocess.PIPE,
        )
        # Read JSON events until all expected streams appear
        async for line in proc.stdout:
            event = json.loads(line)
            if event["event"] == "session_open":
                name = event["name"]
                pty_fd = open(event["pty_path"], "rb+", buffering=0)
                ctx.streams[name] = PtyBiStream(fd=pty_fd.fileno())
        return Verdict.matched("streams_ready"), ctx
```

**Pros:**
- Minimal change — vcmuxer is battle-tested
- Raw binary logs and PTYs remain accessible for other tools (minicom, screen)
- CAmkES side completely unchanged
- vcmuxer handles UART locking, log files, NVIDIA TCU outer layer

**Cons:**
- Subprocess management (lifecycle, crash recovery) adds complexity
- PTY creation is unnecessary indirection — Autopilot ends up reading PTY fds backed by the same data vcmuxer already has
- Bidirectional write (send to VM stdin) is awkward — write to the PTY fd, which vcmuxer must relay back
- The downlink write path (host → CAmkES → VM) is underspecified in vcmuxer
- JSON event-stream parsing is a fragile interface

---

### Option B: Python VCMux frame parser (no subprocess)

Port the `feed_virtioso_byte()` state machine from C to Python. The UART BiStream (pyserial-asyncio-fast) is the raw input; the parser routes bytes to per-stream `asyncio.Queue`s in-process.

The VCMux parser state machine (translating `tcu_com.c:feed_virtioso_byte()`):

```python
ESC = 0xfe
CTRL = 0xfd

class VCMuxParser:
    """In-process VCMux frame demultiplexer."""

    def __init__(self):
        self._streams: dict[int, asyncio.Queue[bytes]] = {}
        self._active_id: int | None = None
        self._escape: bool = False
        self._ctrl_buf: bytearray = bytearray()
        self._ctrl_mode: bool = False

    def feed(self, byte: int) -> None:
        if self._escape:
            self._escape = False
            if byte == ESC:
                # Escaped literal 0xfe — route to active stream
                if self._active_id is not None:
                    self._enqueue(self._active_id, bytes([ESC]))
            elif byte == 0x00:
                # End of stream frame
                self._active_id = None
                self._ctrl_mode = False
            elif byte == CTRL:
                # Control frame follows
                self._ctrl_mode = True
                self._ctrl_buf.clear()
            else:
                # Switch active stream
                self._active_id = byte
        elif byte == ESC:
            self._escape = True
        elif self._ctrl_mode:
            self._ctrl_buf.append(byte)
            self._handle_ctrl()
        elif self._active_id is not None:
            self._enqueue(self._active_id, bytes([byte]))

    def _enqueue(self, stream_id: int, data: bytes) -> None:
        if stream_id not in self._streams:
            self._streams[stream_id] = asyncio.Queue(maxsize=65536)
        self._streams[stream_id].put_nowait(data)

    def _handle_ctrl(self) -> None:
        if len(self._ctrl_buf) < 1:
            return
        ctrl_type = self._ctrl_buf[0]
        if ctrl_type == 0x02:  # VCMUX_CTRL_STREAM_REGISTRY
            # Parse registry JSON (variable length)
            # ... parse length prefix, then JSON blob
            pass


class VCMuxBiStream:
    """A single demuxed VM console stream."""

    def __init__(self, stream_id: int, queue: asyncio.Queue[bytes],
                 uart: UartBiStream):
        self._id = stream_id
        self._queue = queue
        self._uart = uart

    async def read(self, n: int = 4096) -> bytes:
        chunk = await self._queue.get()
        return chunk

    async def write(self, data: bytes) -> None:
        # Frame and send via UART uplink
        framed = self._frame(data)
        self._uart.write(framed)
        await self._uart.drain()

    def _frame(self, data: bytes) -> bytes:
        """Encode data as a VCMux frame: [ESC][sid][data...escaped][ESC][0x00]."""
        buf = bytearray([ESC, self._id])
        for byte in data:
            if byte == ESC:
                buf += bytes([ESC, ESC])  # escape literal 0xfe
            else:
                buf.append(byte)
        buf += bytes([ESC, 0x00])
        return bytes(buf)


class VCMuxSourceOracle:
    async def __call__(self, ctx: StreamContext, timeout: float):
        parser = VCMuxParser()
        uart = ctx.streams["uart0"]  # raw UART BiStream

        # Registry will arrive as the first control frame from CAmkES
        registry: dict | None = None
        registry_ready = asyncio.Event()

        # Feed UART bytes into parser in background
        async def pump():
            nonlocal registry
            while True:
                chunk = await uart.read(256)
                for byte in chunk:
                    parser.feed(byte)
                if parser.registry and registry is None:
                    registry = parser.registry
                    registry_ready.set()

        asyncio.create_task(pump())
        await asyncio.wait_for(registry_ready.wait(), timeout=timeout)

        # Register each announced stream in StreamContext
        for entry in registry["streams"]:
            name = entry["component"]
            sid = entry["stream_id"]
            stream = VCMuxBiStream(
                stream_id=sid,
                queue=parser._streams.setdefault(sid, asyncio.Queue(65536)),
                uart=uart,
            )
            ctx.streams[name] = stream

        return Verdict.matched("streams_ready"), ctx
```

**Pros:**
- No subprocess — pure asyncio, no PTY creation, no JSON event parsing
- Bidirectional write is fully specified: frame bytes and write to UART (VCMux downlink)
- Single read loop on UART fd — no per-stream threads or PTYs
- Testable with a mock UART BiStream (feed recorded VCMux bytes, assert streams created)
- Raw logging is a side-effect of the pump loop (tee bytes to a log file)
- CAmkES side completely unchanged — VCMux wire protocol stays the same

**Cons:**
- Need to port and test the C state machine (~200 lines); must handle all edge cases (partial frames, CTRL variants, NVIDIA TCU outer layer if used)
- Loses PTY files — other tools (minicom, screen) cannot attach directly to demuxed streams. Mitigations: create PTYs as a display side-effect (write copies to PTY fds for human access), or simply document that tools must go through Autopilot
- The `vcmuxer -O nvidia-tcu` outer layer (NVIDIA TCU 0xff escaping) must also be ported if it is used on the CCPLEX port
- Need to handle flow control: if one stream's Queue fills, bytes for that stream are dropped (same as current vcmuxer behaviour with backpressure)

---

### Option C: vcmuxer publishes to Zenoh (bridge approach)

Modify vcmuxer to publish each demuxed stream as a Zenoh topic instead of (or in addition to) creating PTYs. Autopilot subscribes via zenoh-python.

**Not recommended.** Adds Zenoh infrastructure to a path that works fine without it. The UART transport and VCMux protocol already solve the multiplexing problem; Zenoh adds a network layer between vcmuxer and Autopilot without eliminating any existing complexity. The only benefit would be unifying the CAmkES and Isengard discovery interfaces under one Zenoh API — but the operational cost (running zenohd, maintaining a Zenoh bridge) is not justified for a two-author system.

---

## Recommendation

**For the CAmkES/seL4 path: Option B** — port the VCMux parser to Python.

The reasons:
1. The VCMux protocol state machine is not complex (~200 lines of C with a clear structure). Porting it is a bounded, testable task — write it against a mock UART BiStream fed with recorded protocol bytes.
2. Bidirectional write (host → VM stdin) is clean in Option B: frame bytes and write to the same UART BiStream. In Option A, the write path through vcmuxer is underspecified and the downlink mechanism is limited.
3. No subprocess management complexity. In the oracle rewrite, Option A requires managing vcmuxer lifecycle (crash recovery, clean shutdown) as a subprocess. Option B has no subprocess.
4. Directly testable with mock data — fits the oracle unit test model perfectly.
5. CAmkES side unchanged. The ConsoleMux component and VCMux wire protocol are not touched.

**The one real loss** is that minicom/screen cannot attach to individual VM consoles directly. Mitigate by having the `VCMuxSourceOracle` optionally create PTYs as display targets (the same `pipe-pane -I` mechanism used for UART display), written to as a side-effect of the pump loop. This preserves human visibility without requiring vcmuxer.

**For the Isengard/Docker path:** Zenoh-python is the right direction, but only once virtioso-muxd has an actual Zenoh publisher bridge. Until then, the Isengard path uses direct SSH + Docker SDK as covered by the other adapter studies. The vcmux adapter is only needed for seL4/CAmkES.

---

## NVIDIA TCU Outer Layer

One vcmuxer feature that Option B must preserve: the NVIDIA TCU outer layer (`-O nvidia-tcu -C CCPLEX`). This strips NVIDIA's 0xff-escape TCU framing from the CCPLEX UART stream before processing the inner VCMux frames. The outer layer parser is ~50 additional lines of C.

Both layers can coexist in the Python parser:

```python
class NvidiaTCUFilter:
    """Strips NVIDIA TCU 0xff framing, passes inner bytes to VCMuxParser."""
    ESC = 0xff

    def __init__(self, target_tag: int, inner: VCMuxParser):
        self._tag = target_tag
        self._inner = inner
        self._escape = False
        self._active = False

    def feed(self, byte: int) -> None:
        if self._escape:
            self._escape = False
            self._active = (byte == self._tag)
        elif byte == self.ESC:
            self._escape = True
        elif self._active:
            self._inner.feed(byte)
```

This composes cleanly: `NvidiaTCUFilter(tag=0xe1, inner=VCMuxParser())` for CCPLEX, bare `VCMuxParser()` for UARTI.

---

## Summary

| | Option A (subprocess) | Option B (in-process parser) |
|---|---|---|
| CAmkES side change | None | None |
| Subprocess | vcmuxer | None |
| PTYs created | Yes | Optional (display side-effect only) |
| Bidirectional write | Underspecified | Clean (frame + write to UART) |
| Asyncio-native | Partial (subprocess pipe) | Fully |
| Testable without hardware | Partial | Fully (mock UART BiStream) |
| Implementation effort | Low (wrap existing) | Medium (port state machine) |
| Recommended | No | **Yes** |

*Zenoh is not applicable to the CAmkES/seL4 UART path. It may be relevant to the Isengard/Docker path if virtioso-muxd gains a Zenoh publisher bridge.*
