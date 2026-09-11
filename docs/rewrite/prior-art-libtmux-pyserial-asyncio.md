# Recommended Libraries: libtmux and pyserial-asyncio-fast

Both libraries are **recommended for use** in the rewrite — libtmux as the display scaffolding layer, pyserial-asyncio-fast as the UART BiStream transport.

---

## libtmux

**Verdict: Use — display scaffolding only. All libtmux calls are synchronous subprocess invocations; wrap them in `run_in_executor` in an asyncio context.**

### Object Model

Four-level hierarchy: **Server → Session → Window → Pane**. Each level is a typed Python object with `QueryList` collections supporting `.filter()` and `.get()`.

```python
import libtmux

server  = libtmux.Server(socket_name='autopilot')  # named socket
session = server.sessions.get(session_name="autopilot")
window  = session.active_window
pane    = window.split(attach=False)               # returns Pane
```

Sessions are identified as `$N`, windows as `@N`, panes as `%N` (stable identifiers across renames).

### IPC Mechanism

libtmux does **not** maintain a persistent control-mode connection. It shells out individual `tmux` subprocesses for every command via `subprocess.run(["tmux", "-L", socket_name, <subcommand>, ...])` and parses stdout. Each API call has subprocess launch overhead (~5–20 ms). There is no event-driven notification path — it polls.

tmux's control mode (`tmux -C`) does exist at the tmux level and emits async events (`%output`, `%window-close`, `%pane-mode-changed`), but libtmux does not expose this interface. For lifecycle event detection in Autopilot, either poll explicitly or run a dedicated `tmux -C` thread.

### Relevant Pane APIs

| Operation | API |
|---|---|
| Create pane | `window.split(attach=False)` |
| Send keystrokes | `pane.send_keys("text", enter=False)` |
| Read scrollback (lossy) | `pane.capture_pane()` — rendered text, not byte-faithful |
| Pipe bytes into pane | `pane.cmd("pipe-pane", "-I", "cat <path>")` |
| Detect pane alive | poll `server.panes.filter(pane_id=pane.id)` |

`capture_pane()` delivers the visual scrollback buffer as decoded text — ANSI sequences partially decoded, binary mangled. **Do not use this in the I/O path.** It is only usable for debugging or display status checks.

### Implementing the PTY Tee: Displaying Python-Owned Bytes

The correct mechanism is **`pipe-pane -I`**:

```
tmux pipe-pane -I 'shell-command'
```

With `-I`, anything the shell-command writes to stdout is **fed into the pane as if typed** — rendered by the pane's terminal emulator. One pipe per pane at a time.

**Concrete pattern for Autopilot:**

```python
import os, pty, asyncio, libtmux

# 1. Create PTY master/slave pair
master_fd, slave_fd = pty.openpty()
slave_path = os.ttyname(slave_fd)

# 2. Create display pane
server  = libtmux.Server(socket_name="autopilot")
session = server.sessions.get(session_name="autopilot")
window  = session.active_window
pane    = window.split(attach=False)

# 3. Wire pipe-pane -I to cat from the PTY slave
pane.cmd("pipe-pane", "-I", f"cat {slave_path}")

# 4. In the async I/O path, tee bytes to master_fd
async def on_bytes_received(data: bytes) -> None:
    os.write(master_fd, data)   # tee to tmux display
    # oracle also receives data directly from BiStream
```

Why PTY over named FIFO: a PTY slave path gives proper terminal rendering (ANSI handling, carriage-return translation). A named FIFO works but blocks if the reader stalls — the PTY master/slave avoids blocking semantics.

An alternative simpler approach (no PTY):
```python
# Write bytes to named FIFO; pipe-pane -I reads from it
fifo_path = '/run/autopilot/uart0.fifo'
os.mkfifo(fifo_path)
pane.cmd("pipe-pane", "-I", f"cat {fifo_path}")
```
FIFO is simpler but requires careful open-ordering (reader must open before writer, or use `O_NONBLOCK`).

### Event/Callback Model

**libtmux has no event/callback model.** Detecting pane death:

```python
def pane_alive(server: libtmux.Server, pane_id: str) -> bool:
    return bool(server.panes.filter(pane_id=pane_id))
```

For reactive pane-close detection, run a `tmux -C` client in a thread and parse `%pane-exited` lines from control mode output — libtmux does not do this for you.

For keypress detection (e.g., user presses a key to signal completion to InteractiveOracle): create a pane running a sentinel shell command, or use a Unix socket alongside the pane as the completion signal channel.

### Asyncio Compatibility

**Synchronous only.** Wrap all libtmux calls in `run_in_executor`:

```python
pane = await loop.run_in_executor(None, window.split)
```

Isolate all libtmux I/O to a dedicated thread or executor pool. Do not call libtmux from a coroutine without `run_in_executor` — subprocess launch will block the event loop.

### Failure Modes

| Failure | Behavior |
|---|---|
| tmux not running | `LibTmuxException` on first call |
| Session died | `sessions` returns empty `QueryList` (v0.57.1 behavior) |
| Pane resized | tmux handles transparently; libtmux has no resize event |
| Socket missing | `LibTmuxException` with socket path |

v0.57.0 made `.sessions` raise on session death; v0.57.1 reverted to empty `QueryList`. Use lenient accessors (`.sessions`, `.panes`) for health checks.

### Version and Maintenance

- Current: **v0.57.1** (released 2026-05-18) — actively maintained
- Requires: Python ≥ 3.10, tmux ≥ 3.2a
- **Pre-1.0: API instability is explicitly documented** — breaking changes occur across minor versions
- Foundation of tmuxp (session manager)

Pin to a specific version in `requirements.txt` and track the changelog closely.

---

## pyserial-asyncio-fast

**Verdict: Use `pyserial-asyncio-fast`, NOT the original `pyserial-asyncio`.**

The original `pyserial-asyncio` is effectively unmaintained and performs a **blocking sleep inside the async path**, stalling the event loop. Home Assistant has deprecated it and will block its installation starting in 2026-07. Use `pyserial-asyncio-fast` (from home-assistant-libs) — a drop-in replacement.

### What It Provides

Wraps pyserial's `Serial` object in an `asyncio.Transport` subclass (`SerialTransport`), registering the serial file descriptor with the event loop's I/O reactor (`loop.add_reader(fd, _read_ready)` on POSIX). Reads and writes are non-blocking from the event loop's perspective.

Improvement over original: **eager writes** — tries to write immediately before registering a writer fd, avoiding unnecessary fd add/remove cycles (mirrors CPython `asyncio.selector_events`).

### API

```python
import serial_asyncio_fast as serial_asyncio

reader, writer = await serial_asyncio.open_serial_connection(
    url='/dev/ttyUSB0',
    baudrate=115200,
    bytesize=8,
    parity='N',
    stopbits=1,
    xonxoff=False,
    rtscts=False,
)
```

Returns standard `(asyncio.StreamReader, asyncio.StreamWriter)`. All pyserial `Serial()` kwargs pass through. `limit` kwarg sets `StreamReader`'s internal buffer limit (default: 64 KB).

`create_serial_connection()` is the lower-level alternative returning `(transport, protocol)` for lifecycle callbacks.

### Error Handling and Reconnects

`SerialException` during reads/writes triggers transport abort, propagating to `StreamReader` as EOF or exception. **No built-in reconnect** — implement explicitly:

```python
async def open_uart(url: str, baud: int) -> tuple:
    while True:
        try:
            return await serial_asyncio.open_serial_connection(url=url, baudrate=baud)
        except serial.SerialException as e:
            logging.warning(f"Serial open failed: {e}, retrying in 2s")
            await asyncio.sleep(2)
```

On Linux, a USB serial device disappearing (`/dev/ttyUSB0` vanishing) raises `SerialException` on the next read.

### Buffering and Flow Control

`SerialTransport` sets pyserial to non-blocking mode (`timeout=0`, `write_timeout=0`). On POSIX, `loop.add_reader(fd, _read_ready)` drives reads from the OS `select()`/`epoll()` reactor — no polling latency.

Write buffering: `writer.write(data)` enqueues to `_write_buffer`. `loop.add_writer(fd, _write_ready)` drains when fd is writable. Flow control: protocol is paused when buffer exceeds high-water mark (64 KB), resumed at low-water mark (16 KB).

### Non-Blocking Read with Timeout

```python
try:
    data = await asyncio.wait_for(reader.readuntil(b'\n'), timeout=5.0)
except asyncio.TimeoutError:
    pass  # pattern oracle emits timeout verdict
except asyncio.IncompleteReadError:
    pass  # disconnect
```

### Concrete Example: UART Oracle Pattern

```python
import asyncio, serial_asyncio_fast as serial_asyncio

async def uart_boot_sequence():
    reader, writer = await serial_asyncio.open_serial_connection(
        url='/dev/ttyUSB0',
        baudrate=115200,
    )
    # Wait for boot prompt with 10s timeout
    try:
        data = await asyncio.wait_for(reader.readuntil(b'login:'), timeout=10.0)
    except asyncio.TimeoutError:
        raise RuntimeError("Device did not produce login prompt")

    writer.write(b'root\n')
    await writer.drain()

    response = await asyncio.wait_for(reader.readline(), timeout=5.0)

    writer.close()
    await writer.wait_closed()
```

### Thread Safety

All state mutations are serialized through event loop callbacks — not thread-safe for cross-thread `write()`. Use `loop.call_soon_threadsafe(writer.write, data)` if crossing thread boundaries.

On POSIX: `loop.add_reader(fd, ...)` → OS `epoll` reactor, no polling latency.  
On Windows: polling via `call_later()` at 0.5 ms intervals — functional but less efficient.

### Backing a UART BiStream

```python
from dataclasses import dataclass
from asyncio import StreamReader, StreamWriter
import asyncio, os
import serial_asyncio_fast as serial_asyncio

@dataclass
class UartBiStream:
    reader: StreamReader
    writer: StreamWriter
    _display_fd: int | None = None  # master fd for pipe-pane tee

    async def read(self, n: int = 4096) -> bytes:
        data = await self.reader.read(n)
        if data and self._display_fd is not None:
            os.write(self._display_fd, data)  # tee to tmux display
        return data

    async def read_until(self, separator: bytes, timeout: float | None = None) -> bytes:
        coro = self.reader.readuntil(separator)
        return await (asyncio.wait_for(coro, timeout) if timeout else coro)

    def write(self, data: bytes) -> None:
        self.writer.write(data)

    async def drain(self) -> None:
        await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()

    @classmethod
    async def open(cls, url: str, baudrate: int, display_fd: int | None = None, **kwargs) -> "UartBiStream":
        reader, writer = await serial_asyncio.open_serial_connection(
            url=url, baudrate=baudrate, **kwargs
        )
        return cls(reader=reader, writer=writer, _display_fd=display_fd)
```

### Maintenance Status

- `pyserial-asyncio-fast` v0.16 (March 2025) — actively maintained (home-assistant-libs)
- Python 3.9+
- Drop-in API replacement for the original: change `import serial_asyncio` → `import serial_asyncio_fast`
- Original `pyserial-asyncio`: **do not use** — blocks event loop, being deprecated

---

## Summary

| Library | Role in Autopilot | Key constraint |
|---|---|---|
| **libtmux** | Create sessions/windows/panes at startup; wire `pipe-pane -I` for display | Synchronous subprocess — wrap in `run_in_executor`; pre-1.0 API stability |
| **pyserial-asyncio-fast** | UART BiStream transport — `StreamReader`/`StreamWriter` over serial fd | No built-in reconnect — implement explicit retry |

*Sources: [libtmux docs](https://libtmux.git-pull.com), [libtmux changelog](https://libtmux.git-pull.com/history/), [pyserial-asyncio-fast GitHub](https://github.com/home-assistant-libs/pyserial-asyncio-fast), [Home Assistant deprecation notice (2026-01-05)](https://developers.home-assistant.io/blog/2026/01/05/pyserial-asyncio-fast/), [tmux man page — pipe-pane](https://man7.org/linux/man-pages/man1/tmux.1.html)*
