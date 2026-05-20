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
