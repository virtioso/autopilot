# Adapter Library: asyncssh

**Verdict: Use. asyncssh is asyncio-native throughout, maps directly to the BiStream model, and makes Paramiko a non-starter by comparison.**

---

## Why Not Paramiko

Paramiko is synchronous and thread-based. For an asyncio-committed system, every Paramiko operation would need `run_in_executor` wrapping, `asyncio.Queue` bridges for streaming, manual thread safety on channel access, and synchronous SFTP calls pretending to be async. asyncssh's `SSHClientProcess` is already the shape of a BiStream — the mapping is direct.

| | asyncssh | Paramiko |
|---|---|---|
| asyncio native | Yes — all operations are coroutines | No — executor wrapper at every call site |
| BiStream shape | `SSHClientProcess.stdin/stdout` directly | Manual thread + queue bridge |
| Real-time streaming | `async for line in proc.stdout` | `channel.recv()` in thread, queue to asyncio |
| Multiple channels | Native SSHv2 mux in event loop | One thread per channel |
| SFTP | Async client built-in | Synchronous; must run in executor |
| `readuntil(regex)` | Native on `SSHReader` | Does not exist |
| Keepalive | `keepalive_interval`, `keepalive_count_max` | Must implement manually |

---

## Architecture

asyncssh is a pure-Python SSHv2 implementation written entirely against the asyncio event loop. No threads anywhere.

- **Transport layer**: `SSHClientConnection` owns the TCP socket (as an `asyncio.Protocol`), drives the SSH handshake and key exchange, and multiplexes all channels over one socket via SSHv2 channel IDs.
- **Channel layer**: Each `SSHClientChannel` is an isolated byte stream. Flow control, window sizes, and EOF/close signaling handled here.
- **Stream layer**: `SSHReader` and `SSHWriter` wrap a channel, exposing an interface close to `asyncio.StreamReader`/`asyncio.StreamWriter`.
- **Process layer**: `SSHClientProcess` bundles `.stdin` (SSHWriter), `.stdout` (SSHReader), `.stderr` (SSHReader) — same shape as `asyncio.subprocess.Process`.

---

## Command Execution

**High-level — `conn.run()`**: runs a command, buffers all output, returns `SSHCompletedProcess`. Good for fire-and-forget oracle commands.

```python
async with asyncssh.connect(host, username=user, known_hosts=None) as conn:
    result = await conn.run("uname -r", check=True)
    print(result.stdout)       # full captured output
    print(result.exit_status)  # integer exit code
```

**Low-level — `conn.create_process()`**: returns `SSHClientProcess` immediately with live stdin/stdout streams. Required for streaming, interactive sessions, and the write-then-read oracle pattern.

```python
async with conn.create_process("journalctl -f") as proc:
    async for line in proc.stdout:   # SSHReader is async-iterable
        if re.search(pattern, line):
            break
```

`SSHReader` supports: `.read(n)`, `.readline()`, `.readuntil(separator)` (accepts compiled regex natively), `.readexactly(n)`, async iteration. The regex `readuntil()` is directly the ssh_cmd oracle: write command, `await proc.stdout.readuntil(prompt_regex)`.

---

## Channel Multiplexing — The In-Process ControlMaster

A single `SSHClientConnection` can have many concurrent channels. Call `conn.run()` or `conn.create_process()` multiple times on the same connection — each opens a new SSHv2 channel, all multiplexed over one TCP socket transparently.

```python
async with asyncssh.connect(host, known_hosts=None) as conn:
    # Three concurrent channels, one connection, one auth
    results = await asyncio.gather(
        conn.run("df -h"),
        conn.run("free -m"),
        conn.run("uptime"),
    )
```

Hold `conn` alive across the test session; open channels on demand. This is strictly better than SSH ControlMaster: in-process, no subprocess, fully awaitable.

Note: some embedded SSH servers cap channels per connection. In that case serialize — one channel at a time — but that is a server constraint, not an asyncssh limitation.

---

## SFTP File Transfer (upload_efi, upload_file, upload_kernel)

```python
async with conn.start_sftp_client() as sftp:
    await sftp.put("/local/kernel.efi", "/remote/EFI/boot/bootx64.efi")

    # With progress callback
    async def progress(srcpath, dstpath, bytes_copied, total):
        logging.debug("upload %d/%d", bytes_copied, total)
    await sftp.put("/local/bigfile.bin", "/remote/bigfile.bin",
                   progress_handler=progress)

    await sftp.mkdir("/remote/efi_staging")
    stat = await sftp.stat("/remote/efi_staging")
```

`start_sftp_client()` opens an SFTP channel over the existing connection. Multiple SFTP clients can be open concurrently. Prefer SFTP over SCP — SFTP supports resumable transfers and stat queries.

---

## Keepalive and Reconnection

```python
conn = await asyncssh.connect(
    host,
    keepalive_interval=30,      # SSH keepalive every 30 s of silence
    keepalive_count_max=3,      # drop after 3 missed responses
    tcp_keepalive=True,         # also TCP-level keepalives (default True)
)
```

When the connection drops, awaiting reads/writes raise `asyncssh.DisconnectError` or `asyncssh.ConnectionLost`. No automatic reconnect — that is intentional. Maps cleanly to the Poll oracle (`ssh_wait_ready` = `Repeat(connect attempt)` with backoff):

```python
async def ssh_wait_ready(host: str, total_timeout: float) -> asyncssh.SSHClientConnection:
    deadline = asyncio.get_event_loop().time() + total_timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            return await asyncio.wait_for(
                asyncssh.connect(host, known_hosts=None), timeout=5.0
            )
        except (asyncssh.DisconnectError, OSError, asyncio.TimeoutError):
            await asyncio.sleep(2.0)
    raise TimeoutError(f"SSH not ready after {total_timeout}s")
```

---

## Interactive PTY Session

```python
proc = await conn.create_process(
    term_type="vt100",
    term_size=(220, 50),
    request_pty=True,     # allocate PTY on server side
)
# proc.stdin  → write keystrokes
# proc.stdout → read terminal output (stdout+stderr merged in PTY mode)
await proc.stdin.write("help\n")
data = await proc.stdout.readuntil(b"$ ")
```

In PTY mode, stdout and stderr are merged (as in a real terminal). For `interactive_console`, hold `proc` open indefinitely and proxy stdin/stdout to the human/AI interface via the BiStream.

---

## Host Key Verification

```python
conn = await asyncssh.connect(host, known_hosts=None)   # disable verification
```

`known_hosts=None` disables host key checking — correct for hardware test rigs where host keys change with every OS install. asyncssh still negotiates and validates algorithms; it only skips fingerprint comparison.

---

## Failure Modes

**EOF and channel close**: When the remote process exits, asyncssh sends SSH channel EOF then close. `await proc.stdout.read()` returns `b""`. Async iteration terminates cleanly. `proc.wait()` returns the exit status. Deterministic and non-lossy.

**Multiple concurrent reads on same SSHReader**: Not safe — data interleaves. The oracle model already guarantees one active oracle per stream at a time, so this is not a problem in practice.

**Server channel limits**: Embedded SSH servers may cap channels per connection. Serialize oracle access to such targets.

---

## BiStream Wrapper

`SSHClientProcess` is already the shape of a BiStream — `.stdin` for writes, `.stdout` for reads, same SSH channel:

```python
from dataclasses import dataclass
import asyncssh

@dataclass
class SSHBiStream:
    _proc: asyncssh.SSHClientProcess

    async def write(self, data: bytes) -> None:
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def read(self, n: int = 4096) -> bytes:
        return await self._proc.stdout.read(n)

    async def readuntil(self, separator) -> bytes:
        return await self._proc.stdout.readuntil(separator)

    async def readline(self) -> str:
        return await self._proc.stdout.readline()

    async def close(self) -> None:
        self._proc.stdin.close()
        await self._proc.wait()

    @classmethod
    async def open(
        cls,
        conn: asyncssh.SSHClientConnection,
        cmd: str | None = None,
        *,
        pty: bool = False,
    ) -> "SSHBiStream":
        kwargs: dict = {}
        if pty:
            kwargs.update(term_type="vt100", term_size=(220, 50), request_pty=True)
        proc = await conn.create_process(cmd, **kwargs)
        return cls(proc)
```

The connection object lives in StreamContext metadata (or a separate connection registry); the `SSHBiStream` registered under a name like `"target_ssh"` is what oracles interact with.

---

## Maintenance Status

- **Version**: 2.23.0 (2025), Python ≥ 3.10
- **PyPI downloads**: ~15 million/month — production-grade adoption
- **Author**: Ron Frederick (ronf), single primary maintainer, active since 2013
- **Release cadence**: Multiple releases per year
- **Used by**: Scrapli, Netmiko async variants, HPC cluster management, CI/CD platforms
- **No active security CVEs of concern**

*Sources: [asyncssh docs](https://asyncssh.readthedocs.io), [GitHub](https://github.com/ronf/asyncssh), [changelog](https://asyncssh.readthedocs.io/en/latest/changes.html)*
