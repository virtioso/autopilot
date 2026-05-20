"""
SSH adapter: SSHCommandBiStream, SSHCommandOracle, SSHUploadOracle.

Uses asyncssh instead of subprocess ssh CLI. The old system spawned `ssh`
as a subprocess for every command — synchronous, no connection reuse.
asyncssh is fully async-native and integrates cleanly with the event loop.

Connection options:
  - known_hosts=None: equivalent to StrictHostKeyChecking=no. Appropriate
    for a closed test lab where board IPs are known and host key rotation
    is not a security concern.
  - gss_auth=False: equivalent to GSSAPIAuthentication=no. Required in
    environments without Kerberos — avoids 5-second delay on auth failure.
  - preferred_auth: try public key first (no password prompt), then keyboard
    interactive as fallback. Matches BatchMode=yes intent from old code.

SSHCommandOracle — single command execution:
    Opens a connection, runs the command, wraps stdout as a BiStream,
    closes the connection when done. Verdict on exit code (zero = Matched).

    For polling (ssh_wait_ready pattern):
        Repeat.poll(SSHCommandOracle(..., success_label="connected"), "connected",
                    max_iter=30, backoff=2.0)

SSHUploadOracle — SFTP file upload:
    Opens a connection, uploads src → dst via SFTP, returns Matched on success.
    Replaces the old scp-via-subprocess pattern.

SSHBiStream — persistent session channel:
    Not used for simple command execution. Used by InteractiveOracle to
    maintain a long-lived PTY session where both read and write are needed.
    See adapters/interactive.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncssh
import structlog

from engine.oracle import Error, Matched, StreamContext, Verdict

log = structlog.get_logger()

_DEFAULT_SSH_OPTIONS = dict(
    known_hosts=None,
    gss_auth=False,
    preferred_auth="publickey,keyboard-interactive",
)


# ---------------------------------------------------------------------------
# SSHCommandBiStream
# ---------------------------------------------------------------------------

class SSHCommandBiStream:
    """
    BiStream backed by a single asyncssh command channel (stdin + stdout).

    Lifetime: tied to the command. When the command exits, reads return b"".
    Write to stdin before/while reading stdout. The connection is closed
    by SSHCommandOracle after the oracle completes.

    Not for long-lived interactive sessions — use SSHBiStream for that.
    """

    def __init__(self, process: asyncssh.SSHClientProcess) -> None:
        self._proc = process

    async def read(self, n: int = 4096) -> bytes:
        data = await self._proc.stdout.read(n)
        # asyncssh returns str in text mode; always use encoding=None for bytes
        if isinstance(data, str):
            return data.encode("utf-8", errors="replace")
        return data or b""

    async def write(self, data: bytes) -> None:
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()


# ---------------------------------------------------------------------------
# SSHCommandOracle
# ---------------------------------------------------------------------------

class SSHCommandOracle:
    """
    Run a command over SSH and return a verdict based on exit code.

    Opens a fresh connection per invocation — matches the old `ssh_cmd`
    step behaviour (subprocess per call, no connection reuse). Suitable
    for infrequent commands; for high-frequency access, pass a persistent
    asyncssh.SSHClientConnection instead.

    success_exit_codes: set of exit codes considered success (default {0}).
    success_label: verdict label on success.
    failure_label: verdict label on non-success exit code. If None, returns
        Error(f"exit={code}") instead.

    To expose stdout as a BiStream for pattern matching by subsequent oracles,
    set stream_name and the oracle registers the command's stdout in ctx.streams.
    """

    def __init__(
        self,
        host: str,
        cmd: str,
        *,
        username: str = "root",
        port: int = 22,
        success_exit_codes: frozenset[int] = frozenset({0}),
        success_label: str = "ok",
        failure_label: str | None = None,
        stream_name: str | None = None,
        ssh_options: dict | None = None,
    ) -> None:
        self._host = host
        self._cmd = cmd
        self._username = username
        self._port = port
        self._success_codes = success_exit_codes
        self._success_label = success_label
        self._failure_label = failure_label
        self._stream_name = stream_name
        self._ssh_options = {**_DEFAULT_SSH_OPTIONS, **(ssh_options or {})}

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        log.debug("ssh_cmd.connecting", host=self._host, cmd=self._cmd[:80])
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    self._host,
                    port=self._port,
                    username=self._username,
                    **self._ssh_options,
                ),
                timeout=timeout,
            )
        except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
            log.warning("ssh_cmd.connect_failed", host=self._host, error=repr(exc))
            return Error(f"connect_failed: {exc}"), ctx

        async with conn:
            try:
                result = await asyncio.wait_for(
                    conn.run(self._cmd, check=False, encoding=None),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                log.warning("ssh_cmd.timeout", host=self._host, cmd=self._cmd[:80])
                return Error("cmd_timeout"), ctx
            except asyncssh.Error as exc:
                log.warning("ssh_cmd.error", host=self._host, error=repr(exc))
                return Error(f"ssh_error: {exc}"), ctx

        exit_code = result.exit_status
        log.debug("ssh_cmd.done", host=self._host, exit_code=exit_code)

        if exit_code in self._success_codes:
            return Matched(self._success_label), ctx
        if self._failure_label is not None:
            return Matched(self._failure_label), ctx
        return Error(f"exit={exit_code}"), ctx


# ---------------------------------------------------------------------------
# SSHUploadOracle
# ---------------------------------------------------------------------------

class SSHUploadOracle:
    """
    Upload a local file to a remote path via SFTP.

    Replaces the old scp-via-subprocess pattern. asyncssh SFTP is native
    async; no subprocess overhead.

    The old system used subprocess scp with StrictHostKeyChecking=no.
    This oracle uses the same connection options.
    """

    def __init__(
        self,
        host: str,
        src: Path | str,
        dst: str,
        *,
        username: str = "root",
        port: int = 22,
        success_label: str = "uploaded",
        ssh_options: dict | None = None,
    ) -> None:
        self._host = host
        self._src = Path(src)
        self._dst = dst
        self._username = username
        self._port = port
        self._success_label = success_label
        self._ssh_options = {**_DEFAULT_SSH_OPTIONS, **(ssh_options or {})}

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        log.info(
            "sftp.uploading",
            host=self._host,
            src=str(self._src),
            dst=self._dst,
        )
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    self._host,
                    port=self._port,
                    username=self._username,
                    **self._ssh_options,
                ),
                timeout=timeout,
            )
        except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
            return Error(f"connect_failed: {exc}"), ctx

        async with conn:
            try:
                async with conn.start_sftp_client() as sftp:
                    await asyncio.wait_for(
                        sftp.put(str(self._src), self._dst),
                        timeout=timeout,
                    )
            except asyncio.TimeoutError:
                return Error("upload_timeout"), ctx
            except (asyncssh.Error, OSError) as exc:
                log.warning("sftp.upload_failed", error=repr(exc))
                return Error(f"upload_failed: {exc}"), ctx

        log.info("sftp.uploaded", src=str(self._src), dst=self._dst)
        return Matched(self._success_label), ctx


# ---------------------------------------------------------------------------
# SSHBiStream (persistent interactive session)
# ---------------------------------------------------------------------------

class SSHBiStream:
    """
    Long-lived SSH channel BiStream for interactive PTY sessions.

    Created by InteractiveOracle and used by the MCP server to expose a
    console session. Unlike SSHCommandBiStream, this stream is kept open
    for the duration of the InteractiveOracle — the connection lifetime
    is managed by the oracle, not by a single command.

    call open_ssh_session() to create; register .close() as a cleanup_hook.
    """

    def __init__(
        self,
        conn: asyncssh.SSHClientConnection,
        process: asyncssh.SSHClientProcess,
    ) -> None:
        self._conn = conn
        self._process = process
        self._closed = False

    async def read(self, n: int = 4096) -> bytes:
        data = await self._process.stdout.read(n)
        if isinstance(data, str):
            return data.encode("utf-8", errors="replace")
        return data or b""

    async def write(self, data: bytes) -> None:
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._process.close()
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass


async def open_ssh_session(
    host: str,
    *,
    username: str = "root",
    port: int = 22,
    request_pty: bool = True,
    ssh_options: dict | None = None,
) -> SSHBiStream:
    """
    Open a persistent interactive SSH session.

    request_pty=True: allocates a pseudo-terminal — required for interactive
    applications (shells, UART session proxies). Disable for command sessions
    where PTY echo would interfere with pattern matching.

    Always register the returned stream's close() as a cleanup_hook immediately:
        stream = await open_ssh_session(host)
        ctx.streams["ssh0"] = stream
        ctx.register_cleanup("ssh0", stream.close)
    """
    opts = {**_DEFAULT_SSH_OPTIONS, **(ssh_options or {})}
    conn = await asyncssh.connect(host, port=port, username=username, **opts)
    process = await conn.create_process(
        request_pty=request_pty,
        encoding=None,
    )
    return SSHBiStream(conn, process)
