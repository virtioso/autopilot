"""
Interactive adapter: ConsoleBridge and InteractiveOracle.

InteractiveOracle exposes a named BiStream to a human or AI for direct
interaction (read/write), then waits for a completion signal before
returning a verdict.

Typical usage in a chain:
    Sequence(
        BootOracle,         # navigate hardware to known state
        LoginOracle,        # authenticate
        InteractiveOracle,  # hand off to human or AI; resume after done
    )

ConsoleBridge: the live session object. Holds:
  - session_id: unique identifier (used by MCP server to address the session)
  - read_q:  bytes flowing from the target → observer/AI
  - write_q: bytes flowing from observer/AI → target
  - done:    asyncio.Event; set to end the session and resume the oracle

Module-level _session_registry: dict[str, ConsoleBridge]
  Populated on oracle entry, removed on oracle exit. The MCP server queries
  this registry to discover live sessions and forward reads/writes.

Completion signals:
  - done.set() — called by MCP server tool (close_session, send_completion)
  - timeout — Timeout combinator wrapping InteractiveOracle
  - External cancellation (CancelledError) — chain shutdown

Verdict:
  - Matched("interactive_done") when done.set() signals completion
  - TimeoutVerdict propagates from the outer Timeout combinator
  - Error("interactive_cancelled") if cancelled (unlikely — cancellation
    typically propagates as CancelledError, but the inner wait may mask it)

BiStream tee: read_q and write_q are mirrors of the underlying stream.
A background pump relays between the underlying BiStream and the queues:
  - pump_out: reads BiStream → puts bytes in read_q (observable by MCP/human)
  - pump_in:  reads write_q → writes bytes to BiStream (commands from MCP)

Queue sizes are bounded (default 65536 bytes per direction) to prevent
unbounded memory growth during long interactive sessions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import structlog

from engine.oracle import Error, Matched, StreamContext, Verdict

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------

_session_registry: dict[str, "ConsoleBridge"] = {}


def get_session(session_id: str) -> "ConsoleBridge | None":
    """Look up a live interactive session by ID."""
    return _session_registry.get(session_id)


def list_sessions() -> list[str]:
    """Return IDs of all currently active interactive sessions."""
    return list(_session_registry.keys())


# ---------------------------------------------------------------------------
# ConsoleBridge
# ---------------------------------------------------------------------------

@dataclass
class ConsoleBridge:
    """
    Live interactive session: bidirectional byte relay between a BiStream and
    an external actor (human via tmux / AI via MCP).

    read_q:  target → observer (bytes for the human/AI to read)
    write_q: observer → target (commands from human/AI to send)
    done:    set by external actor to end the session

    Both queues use bytes items. None is the EOF sentinel (only sent on
    underlying stream EOF; external actors use done.set() to end normally).
    """
    session_id: str
    read_q: asyncio.Queue[bytes | None] = field(default_factory=lambda: asyncio.Queue(maxsize=65536))
    write_q: asyncio.Queue[bytes | None] = field(default_factory=lambda: asyncio.Queue(maxsize=65536))
    done: asyncio.Event = field(default_factory=asyncio.Event)

    async def send(self, data: bytes) -> None:
        """External actor sends data to the target (puts in write_q)."""
        await self.write_q.put(data)

    async def recv(self, timeout: float | None = None) -> bytes | None:
        """External actor reads data from the target (pops from read_q)."""
        if timeout is not None:
            return await asyncio.wait_for(self.read_q.get(), timeout=timeout)
        return await self.read_q.get()

    def signal_done(self) -> None:
        """External actor signals session completion."""
        self.done.set()


# ---------------------------------------------------------------------------
# InteractiveOracle
# ---------------------------------------------------------------------------

class InteractiveOracle:
    """
    Hand off a named BiStream to an external actor for direct interaction.

    The oracle:
    1. Looks up stream_name in ctx.streams
    2. Creates a ConsoleBridge and registers it in _session_registry
    3. Starts pump tasks: out-pump (stream → read_q), in-pump (write_q → stream)
    4. Waits for bridge.done to be set (by MCP server or completion signal)
    5. Cancels pump tasks
    6. Removes the session from _session_registry
    7. Returns Matched("interactive_done")

    Use a Timeout combinator to enforce a maximum session duration:
        Timeout(InteractiveOracle("tty0"), 3600)  # 1-hour session cap

    session_id: unique ID for MCP/tmux addressing. Defaults to stream_name.
    done_label: verdict label on normal completion.
    """

    def __init__(
        self,
        stream_name: str,
        *,
        session_id: str | None = None,
        done_label: str = "interactive_done",
        queue_maxsize: int = 65536,
    ) -> None:
        self._stream_name = stream_name
        self._session_id = session_id or stream_name
        self._done_label = done_label
        self._queue_maxsize = queue_maxsize

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        stream = ctx.streams.get(self._stream_name)
        if stream is None:
            return Error(f"interactive: stream '{self._stream_name}' not in ctx"), ctx

        if self._session_id in _session_registry:
            log.warning(
                "interactive.session_id_collision",
                session_id=self._session_id,
            )

        bridge = ConsoleBridge(
            session_id=self._session_id,
            read_q=asyncio.Queue(maxsize=self._queue_maxsize),
            write_q=asyncio.Queue(maxsize=self._queue_maxsize),
        )
        _session_registry[self._session_id] = bridge

        log.info(
            "interactive.session_opened",
            session_id=self._session_id,
            stream=self._stream_name,
        )

        out_task = asyncio.create_task(
            _pump_out(stream, bridge.read_q),
            name=f"interactive_out_{self._session_id}",
        )
        in_task = asyncio.create_task(
            _pump_in(bridge.write_q, stream),
            name=f"interactive_in_{self._session_id}",
        )

        try:
            await bridge.done.wait()
            verdict: Verdict = Matched(self._done_label)
        except asyncio.CancelledError:
            verdict = Error("interactive_cancelled")
            raise
        finally:
            out_task.cancel()
            in_task.cancel()
            # Drain tasks so transports are not left open
            await asyncio.gather(out_task, in_task, return_exceptions=True)
            _session_registry.pop(self._session_id, None)
            log.info(
                "interactive.session_closed",
                session_id=self._session_id,
                verdict=type(verdict).__name__,
            )

        return verdict, ctx


# ---------------------------------------------------------------------------
# Pump coroutines
# ---------------------------------------------------------------------------

async def _pump_out(
    stream: object,
    read_q: asyncio.Queue[bytes | None],
) -> None:
    """Relay bytes from the underlying stream into the read queue."""
    try:
        while True:
            chunk: bytes = await stream.read(4096)
            if not chunk:
                await read_q.put(None)  # EOF
                break
            try:
                read_q.put_nowait(chunk)
            except asyncio.QueueFull:
                log.warning("interactive.read_q_full", dropped=len(chunk))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("interactive.pump_out_error", error=repr(exc))


async def _pump_in(
    write_q: asyncio.Queue[bytes | None],
    stream: object,
) -> None:
    """Relay bytes from the write queue into the underlying stream."""
    try:
        while True:
            item = await write_q.get()
            if item is None:
                break
            await stream.write(item)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("interactive.pump_in_error", error=repr(exc))
