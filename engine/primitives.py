"""
Primitive oracles: PatternOracle and CommandOracle.

These work on any BiStream and have no transport-specific dependencies.
They are the building blocks that UART, SSH, and process adapters use
to implement their higher-level oracles.

Layering:
  engine/oracle.py       — types (Verdict, StreamContext, BiStream Protocol)
  engine/combinators.py  — combinators (Sequence, Choice, Race, ...)
  engine/primitives.py   — primitive oracles (this file)
  adapters/uart.py       — UART BiStream factory
  adapters/ssh.py        — SSH BiStream + command oracles
  adapters/process.py    — subprocess BiStream + spawn oracle
"""

from __future__ import annotations

import re

import structlog

from .oracle import Error, Matched, StreamContext, Verdict

log = structlog.get_logger()


class PatternOracle:
    """
    Read from a named stream until a regex matches, then return Matched(label).

    Buffer semantics: identical to Choice with one option — appends chunks to
    a bytearray and tries the pattern after each append. The buffer advances
    past the match end so the next oracle sees bytes starting after the match.

    max_buf: same overflow protection as Choice. Default 1 MiB.

    For the common case of a single expected pattern on a single stream.
    For racing multiple patterns on the same stream, use Choice instead.
    """

    def __init__(
        self,
        stream: str,
        pattern: bytes | str,
        label: str = "match",
        max_buf: int = 1024 * 1024,
    ) -> None:
        if isinstance(pattern, str):
            pattern = pattern.encode()
        self._stream = stream
        self._pattern = re.compile(pattern)
        self._label = label
        self._max_buf = max_buf

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        bio = ctx.streams[self._stream]
        buf = bytearray()

        while True:
            chunk = await bio.read(4096)
            if not chunk:
                return Error("stream_eof"), ctx
            buf.extend(chunk)

            if len(buf) > self._max_buf:
                log.warning(
                    "pattern.buffer_overflow",
                    stream=self._stream,
                    pattern=self._pattern.pattern,
                    size=len(buf),
                )
                return Error("buffer_overflow"), ctx

            m = self._pattern.search(buf)
            if m:
                log.debug(
                    "pattern.matched",
                    stream=self._stream,
                    label=self._label,
                    pattern=self._pattern.pattern,
                )
                del buf[: m.end()]
                return Matched(self._label), ctx


class VerdictOracle:
    """
    Return an immediate Matched(label) verdict without reading any stream.

    Maps to the old chain 'pass'/'fail' terminal step types, and to
    'set_test_verdict' when the verdict is known without pattern matching.
    """

    def __init__(self, label: str = "ok") -> None:
        self._label = label

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        return Matched(self._label), ctx


class FilterBiStream:
    """
    Wraps any BiStream and preprocesses read() output.

    Applies two transformations in a single stateful scan:

    1. ANSI CSI stripping: ESC [ ... <final byte 0x40–0x7E> sequences are
       removed. Non-CSI escapes (ESC M, ESC =, etc.) pass through unchanged.
       Incomplete sequences split across read() calls are buffered and resolved
       on the next call.

    2. CRLF normalisation: DOS-style CR LF (0x0D 0x0A) is replaced by LF
       (0x0A). A bare CR not followed by LF is passed through unchanged.
       A CR at the very end of a chunk is buffered until the next chunk
       determines whether it is part of a CRLF pair.

    write() is passed through to the inner stream unchanged — only inbound
    bytes (read direction) are filtered.

    Both transformations are on by default. Pass strip_ansi=False or
    normalize_crlf=False to disable either one.
    """

    def __init__(
        self,
        inner: object,
        *,
        strip_ansi: bool = True,
        normalize_crlf: bool = True,
    ) -> None:
        self._inner = inner
        self._strip_ansi = strip_ansi
        self._normalize_crlf = normalize_crlf
        self._pending = bytearray()

    async def read(self, n: int = 4096) -> bytes:
        data = await self._inner.read(n)
        if not data:
            # EOF: flush whatever is buffered (incomplete escape or lone CR)
            tail = bytes(self._pending)
            self._pending.clear()
            return tail
        return self._filter(data)

    async def write(self, data: bytes) -> None:
        await self._inner.write(data)

    def __getattr__(self, name: str):
        # Forward any attribute not defined on FilterBiStream to the inner stream.
        # This lets callers use transport-specific methods (e.g. ProcessBiStream.wait(),
        # UARTBiStream.close()) on the wrapper without needing to unwrap it.
        return getattr(self._inner, name)

    def _filter(self, data: bytes) -> bytes:
        buf = bytes(self._pending) + data
        self._pending.clear()
        out = bytearray()
        i = 0
        n = len(buf)
        while i < n:
            b = buf[i]

            if self._strip_ansi and b == 0x1B:
                if i + 1 >= n:
                    # ESC at end of chunk — can't determine type yet
                    self._pending.extend(buf[i:])
                    break
                if buf[i + 1] == 0x5B:  # '[' — CSI sequence
                    j = i + 2
                    while j < n and not (0x40 <= buf[j] <= 0x7E):
                        j += 1
                    if j < n:
                        i = j + 1   # complete CSI — skip it entirely
                    else:
                        self._pending.extend(buf[i:])  # incomplete — save for next chunk
                        break
                    continue
                # Non-CSI escape (ESC M, ESC =, etc.) — fall through and emit ESC byte

            if self._normalize_crlf and b == 0x0D:
                if i + 1 >= n:
                    # CR at end of chunk — buffer to check next byte
                    self._pending.append(0x0D)
                    i += 1
                    break
                if buf[i + 1] == 0x0A:
                    # CR LF pair — skip CR, LF will be emitted on the next iteration
                    i += 1
                    continue
                # bare CR (not followed by LF) — fall through and emit it

            out.append(b)
            i += 1
        return bytes(out)


class CommandOracle:
    """
    Write a command to a stream, then wait for a response pattern.

    This is the write-then-read pattern used by ssh_cmd, send_cmd, and
    uefi_shell_run in the current system.

    PTY echo: when writing to a PTY-backed BiStream, the line discipline
    echoes the sent command back into the read stream before the response
    arrives. If the command itself would match the response pattern, pass
    echo_cmd=True — the oracle skips the first occurrence (the echo) and
    waits for the actual response.

    suffix: appended to cmd before writing. Default b"\\n" (send as a line).
    """

    def __init__(
        self,
        stream: str,
        cmd: bytes | str,
        response_pattern: bytes | str,
        label: str = "ok",
        suffix: bytes = b"\n",
        max_buf: int = 1024 * 1024,
    ) -> None:
        if isinstance(cmd, str):
            cmd = cmd.encode()
        if isinstance(response_pattern, str):
            response_pattern = response_pattern.encode()
        self._stream = stream
        self._cmd = cmd + suffix
        self._response = PatternOracle(stream, response_pattern, label=label, max_buf=max_buf)

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        bio = ctx.streams[self._stream]
        await bio.write(self._cmd)
        log.debug(
            "command.sent",
            stream=self._stream,
            cmd=self._cmd[:80],
        )
        return await self._response(ctx, timeout)
