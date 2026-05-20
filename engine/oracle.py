"""Core oracle types: Verdict, BiStream, StreamContext, Oracle Protocol."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Matched:
    label: str

    def is_matched(self, label: str | None = None) -> bool:
        return label is None or self.label == label


@dataclass(frozen=True)
class TimeoutVerdict:
    def is_matched(self, label: str | None = None) -> bool:
        return False


@dataclass(frozen=True)
class Error:
    reason: str

    def is_matched(self, label: str | None = None) -> bool:
        return False


Verdict = Matched | TimeoutVerdict | Error


# ---------------------------------------------------------------------------
# BiStream Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class BiStream(Protocol):
    """Bidirectional byte channel: read and write go to the same resource."""

    async def read(self, n: int = 4096) -> bytes: ...
    async def write(self, data: bytes) -> None: ...


# ---------------------------------------------------------------------------
# StreamContext
# ---------------------------------------------------------------------------

# cleanup_hooks entry: (stream_name | None, callable)
# - stream_name: the stream this hook is associated with (for selective cleanup at merge)
# - None: global hook not tied to a specific stream (runs at full ctx.cleanup())
CleanupHook = tuple[str | None, Callable[[], None]]


@dataclass
class StreamContext:
    """
    Named registry of BiStream instances plus runtime metadata.

    Oracles receive ctx, may mutate it in-place, and return the same object.
    At Parallel/Race fork points, ctx.fork() produces an isolated child copy.
    """

    streams: dict[str, BiStream] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
    cleanup_hooks: list[CleanupHook] = field(default_factory=list)

    def fork(self) -> StreamContext:
        """
        Produce a child context for Parallel/Race branches.

        Shallow-copies the streams dict (same BiStream objects, new dict).
        Child starts with an EMPTY cleanup_hooks list — resources acquired
        within the branch register into the branch's hooks only.
        Pre-fork hooks remain on the parent; they are not the branch's concern.
        """
        return StreamContext(
            streams=dict(self.streams),
            metadata=dict(self.metadata),
            cleanup_hooks=[],
        )

    def merge(self, child: StreamContext, include_streams: set[str]) -> None:
        """
        Incorporate a branch's results after Parallel join.

        Streams named in include_streams are copied from child into self.
        Their associated cleanup_hooks migrate to self.cleanup_hooks.
        Resources in child that are NOT in include_streams are cleaned up now.
        """
        # Migrate included streams and their hooks
        for name in include_streams:
            if name in child.streams:
                self.streams[name] = child.streams[name]
        hooks_to_migrate = [h for h in child.cleanup_hooks if h[0] in include_streams]
        hooks_to_discard = [h for h in child.cleanup_hooks if h[0] not in include_streams]
        self.cleanup_hooks.extend(hooks_to_migrate)

        # Clean up resources that are not being merged
        for _, fn in hooks_to_discard:
            try:
                fn()
            except Exception:
                pass

    def cleanup(self, stream_name: str | None = None) -> None:
        """
        Run cleanup hooks.

        If stream_name is given, run only hooks associated with that stream.
        If stream_name is None, run all hooks (used at chain end or on error).
        Hooks run in reverse registration order (LIFO — last acquired, first released).
        Exceptions in hooks are suppressed individually so all hooks run.
        """
        if stream_name is None:
            hooks = list(reversed(self.cleanup_hooks))
            self.cleanup_hooks.clear()
        else:
            remaining = []
            hooks = []
            for h in self.cleanup_hooks:
                if h[0] == stream_name:
                    hooks.append(h)
                else:
                    remaining.append(h)
            self.cleanup_hooks = remaining
            hooks = list(reversed(hooks))

        for _, fn in hooks:
            try:
                fn()
            except Exception:
                pass

    def register_cleanup(self, stream_name: str | None, fn: Callable[[], None]) -> None:
        """Register a cleanup hook. Call immediately after resource acquisition."""
        self.cleanup_hooks.append((stream_name, fn))


# ---------------------------------------------------------------------------
# Oracle Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Oracle(Protocol):
    """
    An oracle is a callable that takes a StreamContext and timeout,
    may mutate the context in-place, and returns (Verdict, same_ctx).

    The returned StreamContext MUST be the same object as the input ctx.
    The engine asserts this at each call site (disabled with python -O).
    """

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]: ...


def assert_oracle_result(result_ctx: StreamContext, input_ctx: StreamContext) -> None:
    """Assert oracle returned the same StreamContext object it received."""
    assert result_ctx is input_ctx, (
        f"oracle returned a different StreamContext object "
        f"({id(result_ctx)} != {id(input_ctx)}). "
        "Oracles must mutate ctx in-place and return the same object."
    )
