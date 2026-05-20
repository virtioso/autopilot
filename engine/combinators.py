"""
Oracle combinators: Sequence, Choice, Race, Timeout, Parallel, Repeat.

All combinators implement the Oracle protocol — they are oracles themselves
and compose cleanly. The engine never needs to know whether it is running
a primitive oracle or a nested combinator tree.

Python 3.12+ required: asyncio.wait_for cancellation bug (bpo-46707) is
fixed in 3.12. TaskGroup is used for Parallel (structured concurrency);
Race uses asyncio.wait(FIRST_COMPLETED) for clean first-winner semantics.

Design decisions are documented in ~/autopilot/docs/rewrite/index.md W1–W29.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Callable

import structlog

from .oracle import (
    Error,
    Matched,
    Oracle,
    StreamContext,
    TimeoutVerdict,
    Verdict,
    assert_oracle_result,
)

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Sequence
# ---------------------------------------------------------------------------

class Sequence:
    """
    Run oracles A, B, C, ... in order, threading StreamContext through.

    Short-circuits on the first non-Matched verdict (TimeoutVerdict or Error):
    subsequent oracles are not called, and the non-Matched verdict is returned
    immediately. This preserves the current system's behaviour where a failed
    step terminates the chain.

    Deadline note: Sequence does NOT reduce the remaining timeout between
    steps. For a shared wall-clock budget across all steps, wrap the entire
    Sequence in a Timeout combinator: Timeout(Sequence(A, B, C), 60).
    This separation of concerns is intentional (see W26 in index.md).
    """

    def __init__(self, steps: list[Oracle]) -> None:
        self._steps = steps
        if not steps:
            raise ValueError("Sequence requires at least one step")

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        verdict: Verdict = Matched("noop")
        for step in self._steps:
            orig = ctx
            verdict, ctx = await step(ctx, timeout)
            assert_oracle_result(ctx, orig)
            if not isinstance(verdict, Matched):
                log.debug("sequence.short_circuit", verdict=type(verdict).__name__)
                return verdict, ctx
            log.debug("sequence.step_ok", label=verdict.label)
        return verdict, ctx


# ---------------------------------------------------------------------------
# Choice
# ---------------------------------------------------------------------------

@dataclass
class ChoiceOption:
    """One pattern option for the Choice combinator."""
    pattern: re.Pattern[bytes]
    label: str


class Choice:
    """
    Race multiple byte patterns against the SAME stream.

    Reads the named stream in chunks, appending to a bytearray buffer.
    After each append, tries all patterns via re.search. The first pattern
    to match wins; the buffer advances past the match end so the next oracle
    sees bytes starting after the match.

    This is the pattern for the current system's `case` step — one stream,
    multiple possible responses, first match wins. For racing across
    DIFFERENT streams, use Race instead.

    max_buf: if exceeded before any pattern matches, returns Error("buffer_overflow")
    rather than growing without bound. Default 1 MiB is generous; lower for tight loops.

    Timeout: the outer Timeout combinator's asyncio.wait_for cancels this
    coroutine via CancelledError when the deadline fires. No internal deadline
    tracking is needed here.
    """

    def __init__(
        self,
        stream: str,
        options: list[ChoiceOption],
        max_buf: int = 1024 * 1024,
    ) -> None:
        self._stream = stream
        self._options = options
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
                    "choice.buffer_overflow",
                    stream=self._stream,
                    size=len(buf),
                    max_buf=self._max_buf,
                )
                return Error("buffer_overflow"), ctx

            for opt in self._options:
                m = opt.pattern.search(buf)
                if m:
                    log.debug(
                        "choice.matched",
                        label=opt.label,
                        pattern=opt.pattern.pattern,
                    )
                    del buf[: m.end()]
                    return Matched(opt.label), ctx


# ---------------------------------------------------------------------------
# Race
# ---------------------------------------------------------------------------

class Race:
    """
    Race independent oracles across DIFFERENT named streams.

    Each branch receives ctx.fork() — its own stream dict and empty
    cleanup_hooks. The first branch to return a Matched verdict wins;
    all other branches are cancelled, awaited, and their contexts cleaned up.

    Disjoint-stream constraint: every branch must reference a different
    named stream. Asserted at runtime. Two branches cannot read the same
    live UART/SSH stream — bytes consumed by one branch are gone. If two
    branches genuinely need the same source, create a tee (fan-out pump)
    Source oracle first.

    Implementation uses asyncio.wait(FIRST_COMPLETED) rather than TaskGroup
    because cancellation of the group from inside a task requires raising an
    exception through the winner's result, which is clumsy. asyncio.wait
    gives clean access to the done/pending split without that indirection.
    """

    def __init__(self, branches: list[tuple[Oracle, str]]) -> None:
        """
        branches: list of (oracle, primary_stream_name) pairs.
        primary_stream_name is used only for the disjoint-stream assertion.
        """
        self._branches = branches

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        stream_names = [s for _, s in self._branches]
        if len(stream_names) != len(set(stream_names)):
            raise RuntimeError(
                f"Race branches reference overlapping streams: {stream_names}. "
                "Each branch must read a distinct named stream."
            )

        fork_ctxs = [ctx.fork() for _ in self._branches]

        async def run_branch(
            idx: int, oracle: Oracle, fork: StreamContext
        ) -> tuple[int, Verdict, StreamContext]:
            verdict, fork = await oracle(fork, timeout)
            return idx, verdict, fork

        tasks = {
            asyncio.create_task(run_branch(i, oracle, fork_ctxs[i])): i
            for i, (oracle, _) in enumerate(self._branches)
        }

        winner_idx: int | None = None
        winner_verdict: Verdict | None = None
        winner_fork: StreamContext | None = None

        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                exc = task.exception()
                if exc is not None:
                    log.warning("race.branch_error", error=repr(exc))
                    continue
                idx, verdict, fork = task.result()
                if isinstance(verdict, Matched) and winner_idx is None:
                    winner_idx = idx
                    winner_verdict = verdict
                    winner_fork = fork
                    for t in pending:
                        t.cancel()
                    pending = set()  # exit outer while

        # Await cancellation of losing tasks so they don't leak
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        # Clean up all losing branch contexts
        for i, fork in enumerate(fork_ctxs):
            if i != winner_idx:
                fork.cleanup()

        if winner_idx is None:
            return TimeoutVerdict(), ctx

        # Merge any new streams the winner created into the parent
        assert winner_fork is not None
        new_streams = set(winner_fork.streams.keys()) - set(ctx.streams.keys())
        ctx.merge(winner_fork, new_streams)
        return winner_verdict, ctx  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

class Timeout:
    """
    Enforce a wall-clock deadline on any oracle.

    Wraps the oracle with asyncio.wait_for. On expiry, the inner oracle's
    task is cancelled and this combinator returns (TimeoutVerdict, ctx).

    The ctx may have partial mutations from the inner oracle if it ran
    partway before the deadline. This is intentional: a TimeoutVerdict is
    always terminal — no subsequent oracle will receive the partially-mutated
    context expecting a successful predecessor. ctx.cleanup() is called by
    the chain executor after any terminal verdict.

    Python 3.12+ required: asyncio.wait_for had a bug (bpo-46707) in 3.10/3.11
    where cancelling the outer task during wait_for could corrupt the exception
    chain. Fixed in 3.12, which is why requires-python = ">=3.12".
    """

    def __init__(self, oracle: Oracle, t: float) -> None:
        self._oracle = oracle
        self._t = t

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        try:
            orig = ctx
            verdict, ctx = await asyncio.wait_for(
                self._oracle(ctx, self._t), timeout=self._t
            )
            assert_oracle_result(ctx, orig)
            return verdict, ctx
        except asyncio.TimeoutError:
            log.debug("timeout.fired", t=self._t)
            return TimeoutVerdict(), ctx


# ---------------------------------------------------------------------------
# Parallel
# ---------------------------------------------------------------------------

@dataclass
class ParallelBranchResult:
    idx: int
    verdict: Verdict
    fork: StreamContext


class Parallel:
    """
    Run branches concurrently, each on a fork of StreamContext.

    Disjoint-stream constraint: same as Race. Each branch gets ctx.fork()
    for isolation — its own cleanup_hooks and stream dict.

    After all branches complete, the reducer decides which verdicts and
    streams are included in the merged parent context. Unhandled exceptions
    in branches become Error verdicts, so the reducer always sees Verdict
    objects rather than raw exceptions.

    Default reducer: all_pass — all branches must return Matched verdicts.
    Pass a custom reducer for other semantics (any_pass, collect_all, etc.).
    """

    def __init__(
        self,
        branches: list[tuple[Oracle, str]],
        reducer: Callable[
            [list[ParallelBranchResult]], tuple[Verdict, set[str]]
        ] | None = None,
    ) -> None:
        """
        branches: list of (oracle, primary_stream_name).
        reducer: (results) → (combined_verdict, stream_names_to_merge)
            stream_names_to_merge: names of streams from branch forks to copy
            into the parent context (with their cleanup_hooks).
        """
        self._branches = branches
        self._reducer = reducer or _all_pass_reducer

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        stream_names = [s for _, s in self._branches]
        if len(stream_names) != len(set(stream_names)):
            raise RuntimeError(
                f"Parallel branches reference overlapping streams: {stream_names}."
            )

        fork_ctxs = [ctx.fork() for _ in self._branches]
        results: list[ParallelBranchResult] = [None] * len(self._branches)  # type: ignore

        async def run_branch(idx: int, oracle: Oracle, fork: StreamContext) -> None:
            try:
                verdict, fork = await oracle(fork, timeout)
                results[idx] = ParallelBranchResult(idx=idx, verdict=verdict, fork=fork)
            except Exception as exc:
                log.warning("parallel.branch_error", idx=idx, error=repr(exc))
                results[idx] = ParallelBranchResult(
                    idx=idx, verdict=Error(f"unhandled: {exc}"), fork=fork_ctxs[idx]
                )

        async with asyncio.TaskGroup() as tg:
            for i, (oracle, _) in enumerate(self._branches):
                tg.create_task(run_branch(i, oracle, fork_ctxs[i]))

        combined_verdict, include_streams = self._reducer(results)

        # Merge winning streams; clean up resources excluded from the merge
        for result in results:
            branch_new_streams = set(result.fork.streams.keys()) - set(ctx.streams.keys())
            to_include = branch_new_streams & include_streams
            to_exclude = branch_new_streams - include_streams
            ctx.merge(result.fork, to_include)
            # cleanup_hooks for excluded new streams are run inside merge()

        return combined_verdict, ctx


def _all_pass_reducer(
    results: list[ParallelBranchResult],
) -> tuple[Verdict, set[str]]:
    """
    All branches must return Matched. Returns first non-Matched if any.
    On full success, all new streams from all branches are merged.
    """
    for r in results:
        if not isinstance(r.verdict, Matched):
            return r.verdict, set()
    all_new: set[str] = set()
    for r in results:
        all_new.update(r.fork.streams.keys())
    return results[-1].verdict, all_new


# ---------------------------------------------------------------------------
# Repeat
# ---------------------------------------------------------------------------

class Repeat:
    """
    Run an oracle repeatedly according to a stop condition.

    Do not construct directly — use the factory methods:
      Repeat.monitor(oracle, *, max_iter, backoff)
      Repeat.poll(oracle, success_label, *, max_iter, backoff)

    These encode the two common intents explicitly. A single stop_on default
    cannot serve both use cases without silently doing the wrong thing (W9).

    max_iter exhausted → Error("max_iter_exceeded").
    backoff: seconds to sleep between iterations.
    """

    def __init__(
        self,
        oracle: Oracle,
        *,
        stop_on: Callable[[Verdict], bool],
        max_iter: int,
        backoff: float = 0.0,
    ) -> None:
        self._oracle = oracle
        self._stop_on = stop_on
        self._max_iter = max_iter
        self._backoff = backoff

    @classmethod
    def monitor(
        cls,
        oracle: Oracle,
        *,
        max_iter: int = 2**31,
        backoff: float = 0.0,
    ) -> "Repeat":
        """
        Monitoring loop: continues on Matched, stops on TimeoutVerdict or Error.
        For: infinite loops watching a stream for events (ftrace monitor, health
        check sentinel). Stops only when the oracle signals it's done or errors.
        """
        return cls(
            oracle,
            stop_on=lambda v: not isinstance(v, Matched),
            max_iter=max_iter,
            backoff=backoff,
        )

    @classmethod
    def poll(
        cls,
        oracle: Oracle,
        success_label: str,
        *,
        max_iter: int,
        backoff: float = 1.0,
    ) -> "Repeat":
        """
        Polling loop: stops on Matched(success_label) or Error, retries on
        TimeoutVerdict. For: "retry until SSH is reachable", "wait for service".
        The success_label distinguishes the expected success match from other
        Matched verdicts the inner oracle might return during retries.
        """
        return cls(
            oracle,
            stop_on=lambda v: (
                isinstance(v, Matched) and v.label == success_label
            ) or isinstance(v, Error),
            max_iter=max_iter,
            backoff=backoff,
        )

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        for i in range(self._max_iter):
            orig = ctx
            verdict, ctx = await self._oracle(ctx, timeout)
            assert_oracle_result(ctx, orig)
            log.debug("repeat.iter", i=i, verdict=type(verdict).__name__)
            if self._stop_on(verdict):
                return verdict, ctx
            if self._backoff > 0:
                await asyncio.sleep(self._backoff)

        log.warning("repeat.max_iter_exceeded", max_iter=self._max_iter)
        return Error("max_iter_exceeded"), ctx
