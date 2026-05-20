"""
Core engine unit tests: oracle.py, combinators.py, recorder.py.

No hardware. Every test uses MockBiStream to feed synthetic byte sequences
into oracles, then asserts on (Verdict, StreamContext) pairs.

pytest-asyncio 1.0+ with asyncio_mode="auto" — no @pytest.mark.asyncio
boilerplate needed. Each test gets a fresh event loop (loop_scope="function").
"""

from __future__ import annotations

import asyncio
import re

import pytest

from engine.oracle import (
    Error,
    Matched,
    StreamContext,
    TimeoutVerdict,
    assert_oracle_result,
)
from engine.combinators import (
    Choice,
    ChoiceOption,
    Parallel,
    Race,
    Repeat,
    Sequence,
    Timeout,
    _all_pass_reducer,
)
from engine.recorder import ListRecorder, NullRecorder, OracleVerdict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockBiStream:
    """
    Feed a fixed byte sequence into an oracle as if it were a live stream.

    Once the data is exhausted, reads return b"" (EOF). The stream is
    not reusable — create a new instance per test (StreamReader cannot be reset
    after feed_eof).

    StreamReader is created lazily on the first read call so that __init__
    runs safely outside an event loop (avoids DeprecationWarning in Python 3.12).
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._reader: asyncio.StreamReader | None = None

    def _get_reader(self) -> asyncio.StreamReader:
        if self._reader is None:
            self._reader = asyncio.StreamReader()
            self._reader.feed_data(self._data)
            self._reader.feed_eof()
        return self._reader

    async def read(self, n: int = 4096) -> bytes:
        return await self._get_reader().read(n)

    async def write(self, data: bytes) -> None:
        pass


class InfiniteStream:
    """Streams an infinitely repeating byte pattern. Used to test timeout paths."""

    def __init__(self, chunk: bytes = b"x") -> None:
        self._chunk = chunk

    async def read(self, n: int = 4096) -> bytes:
        await asyncio.sleep(0)  # yield so the event loop can cancel us
        return self._chunk

    async def write(self, data: bytes) -> None:
        pass


def make_ctx(**streams) -> StreamContext:
    return StreamContext(streams=dict(streams))


# ---------------------------------------------------------------------------
# StreamContext
# ---------------------------------------------------------------------------

def test_fork_produces_independent_stream_dict():
    stream = MockBiStream(b"hello")
    ctx = make_ctx(tty0=stream)
    child = ctx.fork()

    child.streams["new_stream"] = MockBiStream(b"world")
    assert "new_stream" not in ctx.streams


def test_fork_cleanup_hooks_are_empty():
    ctx = make_ctx()
    ran = []
    ctx.register_cleanup(None, lambda: ran.append("parent"))
    child = ctx.fork()
    assert child.cleanup_hooks == []


def test_cleanup_runs_in_reverse_order():
    ctx = make_ctx()
    order = []
    ctx.register_cleanup(None, lambda: order.append(1))
    ctx.register_cleanup(None, lambda: order.append(2))
    ctx.register_cleanup(None, lambda: order.append(3))
    ctx.cleanup()
    assert order == [3, 2, 1]


def test_cleanup_by_stream_name():
    ctx = make_ctx()
    ran = []
    ctx.register_cleanup("tty0", lambda: ran.append("tty0"))
    ctx.register_cleanup("tty1", lambda: ran.append("tty1"))
    ctx.cleanup("tty0")
    assert ran == ["tty0"]
    assert len(ctx.cleanup_hooks) == 1
    assert ctx.cleanup_hooks[0][0] == "tty1"


def test_cleanup_suppresses_exceptions():
    ctx = make_ctx()
    ran = []
    ctx.register_cleanup(None, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    ctx.register_cleanup(None, lambda: ran.append("after"))
    ctx.cleanup()  # should not raise
    assert ran == ["after"]


def test_merge_migrates_included_stream_hooks():
    ctx = make_ctx()
    child = ctx.fork()
    cleaned = []
    child.register_cleanup("vm0", lambda: cleaned.append("vm0"))
    child.streams["vm0"] = MockBiStream(b"")

    ctx.merge(child, {"vm0"})

    assert "vm0" in ctx.streams
    assert len(ctx.cleanup_hooks) == 1
    assert ctx.cleanup_hooks[0][0] == "vm0"
    assert cleaned == []  # hook migrated, not run


def test_merge_runs_cleanup_for_excluded_streams():
    ctx = make_ctx()
    child = ctx.fork()
    cleaned = []
    child.register_cleanup("vm0", lambda: cleaned.append("vm0"))
    child.streams["vm0"] = MockBiStream(b"")

    ctx.merge(child, set())  # exclude vm0

    assert "vm0" not in ctx.streams
    assert cleaned == ["vm0"]  # hook was run, not migrated


# ---------------------------------------------------------------------------
# Oracle protocol assertion
# ---------------------------------------------------------------------------

def test_assert_oracle_result_same_object():
    ctx = StreamContext()
    assert_oracle_result(ctx, ctx)  # should not raise


def test_assert_oracle_result_different_object():
    ctx_a = StreamContext()
    ctx_b = StreamContext()
    with pytest.raises(AssertionError, match="different StreamContext"):
        assert_oracle_result(ctx_a, ctx_b)


# ---------------------------------------------------------------------------
# Sequence
# ---------------------------------------------------------------------------

class PassOracle:
    """Always returns Matched(label)."""
    def __init__(self, label: str = "ok") -> None:
        self.label = label
        self.called = False

    async def __call__(self, ctx, timeout):
        self.called = True
        return Matched(self.label), ctx


class FailOracle:
    """Always returns Error(reason)."""
    def __init__(self, reason: str = "fail") -> None:
        self.reason = reason
        self.called = False

    async def __call__(self, ctx, timeout):
        self.called = True
        return Error(self.reason), ctx


async def test_sequence_threads_context():
    added = {}

    async def adder(ctx, timeout):
        ctx.metadata["x"] = 42
        return Matched("ok"), ctx

    async def checker(ctx, timeout):
        assert ctx.metadata["x"] == 42
        return Matched("ok"), ctx

    ctx = make_ctx()
    seq = Sequence([adder, checker])
    verdict, out = await seq(ctx, 5.0)
    assert isinstance(verdict, Matched)


async def test_sequence_short_circuits_on_error():
    b = PassOracle("b")
    seq = Sequence([FailOracle(), b])
    verdict, _ = await seq(make_ctx(), 5.0)
    assert isinstance(verdict, Error)
    assert not b.called


async def test_sequence_short_circuits_on_timeout():
    b = PassOracle("b")

    async def times_out(ctx, timeout):
        return TimeoutVerdict(), ctx

    seq = Sequence([times_out, b])
    verdict, _ = await seq(make_ctx(), 5.0)
    assert isinstance(verdict, TimeoutVerdict)
    assert not b.called


async def test_sequence_returns_last_label():
    seq = Sequence([PassOracle("first"), PassOracle("last")])
    verdict, _ = await seq(make_ctx(), 5.0)
    assert isinstance(verdict, Matched)
    assert verdict.label == "last"


async def test_sequence_rejects_empty():
    with pytest.raises(ValueError):
        Sequence([])


# ---------------------------------------------------------------------------
# Choice
# ---------------------------------------------------------------------------

async def test_choice_matches_first_pattern():
    stream = MockBiStream(b"BOOT\r\nREADY\r\n")
    ctx = make_ctx(tty0=stream)
    choice = Choice(
        "tty0",
        [
            ChoiceOption(re.compile(rb"READY"), "ready"),
            ChoiceOption(re.compile(rb"ERROR"), "error"),
        ],
    )
    verdict, _ = await choice(ctx, 5.0)
    assert verdict == Matched("ready")


async def test_choice_matches_second_pattern():
    stream = MockBiStream(b"ERROR: kernel panic\r\n")
    ctx = make_ctx(tty0=stream)
    choice = Choice(
        "tty0",
        [
            ChoiceOption(re.compile(rb"READY"), "ready"),
            ChoiceOption(re.compile(rb"ERROR"), "error"),
        ],
    )
    verdict, _ = await choice(ctx, 5.0)
    assert verdict == Matched("error")


async def test_choice_buffer_overflow():
    # 10 bytes of data, max_buf=5 — should overflow before any pattern matches
    stream = MockBiStream(b"0123456789")
    ctx = make_ctx(tty0=stream)
    choice = Choice(
        "tty0",
        [ChoiceOption(re.compile(rb"READY"), "ready")],
        max_buf=5,
    )
    verdict, _ = await choice(ctx, 5.0)
    assert verdict == Error("buffer_overflow")


async def test_choice_eof_returns_error():
    stream = MockBiStream(b"")
    ctx = make_ctx(tty0=stream)
    choice = Choice("tty0", [ChoiceOption(re.compile(rb"READY"), "ready")])
    verdict, _ = await choice(ctx, 5.0)
    assert verdict == Error("stream_eof")


async def test_choice_does_not_mutate_input_ctx():
    stream = MockBiStream(b"READY\r\n")
    ctx = make_ctx(tty0=stream)
    orig_streams = dict(ctx.streams)
    choice = Choice("tty0", [ChoiceOption(re.compile(rb"READY"), "ok")])
    verdict, returned_ctx = await choice(ctx, 5.0)
    assert returned_ctx is ctx
    assert ctx.streams.keys() == orig_streams.keys()


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

async def test_timeout_passes_verdict_through():
    t = Timeout(PassOracle("ok"), 5.0)
    verdict, _ = await t(make_ctx(), 10.0)
    assert verdict == Matched("ok")


async def test_timeout_returns_timeout_verdict_on_expiry():
    async def slow(ctx, timeout):
        await asyncio.sleep(10)
        return Matched("ok"), ctx

    t = Timeout(slow, 0.05)
    verdict, _ = await t(make_ctx(), 10.0)
    assert isinstance(verdict, TimeoutVerdict)


# ---------------------------------------------------------------------------
# Repeat
# ---------------------------------------------------------------------------

async def test_repeat_poll_stops_on_success():
    count = {"n": 0}

    async def sometimes_passes(ctx, timeout):
        count["n"] += 1
        if count["n"] >= 3:
            return Matched("connected"), ctx
        return TimeoutVerdict(), ctx

    rep = Repeat.poll(sometimes_passes, "connected", max_iter=10, backoff=0.0)
    verdict, _ = await rep(make_ctx(), 5.0)
    assert verdict == Matched("connected")
    assert count["n"] == 3


async def test_repeat_poll_stops_on_error():
    async def errors(ctx, timeout):
        return Error("refused"), ctx

    rep = Repeat.poll(errors, "connected", max_iter=10, backoff=0.0)
    verdict, _ = await rep(make_ctx(), 5.0)
    assert isinstance(verdict, Error)


async def test_repeat_poll_max_iter_exceeded():
    rep = Repeat.poll(
        FailOracle("timeout_like"),
        "connected",
        max_iter=3,
        backoff=0.0,
    )
    # FailOracle returns Error, which stops poll — use TimeoutOracle instead
    call_count = {"n": 0}

    async def always_timeout(ctx, timeout):
        call_count["n"] += 1
        return TimeoutVerdict(), ctx

    rep = Repeat.poll(always_timeout, "connected", max_iter=3, backoff=0.0)
    verdict, _ = await rep(make_ctx(), 5.0)
    assert isinstance(verdict, Error)
    assert "max_iter_exceeded" in verdict.reason
    assert call_count["n"] == 3


async def test_repeat_monitor_stops_on_timeout():
    count = {"n": 0}

    async def matches_twice_then_times_out(ctx, timeout):
        count["n"] += 1
        if count["n"] <= 2:
            return Matched("event"), ctx
        return TimeoutVerdict(), ctx

    rep = Repeat.monitor(matches_twice_then_times_out, max_iter=10, backoff=0.0)
    verdict, _ = await rep(make_ctx(), 5.0)
    assert isinstance(verdict, TimeoutVerdict)
    assert count["n"] == 3


# ---------------------------------------------------------------------------
# Parallel
# ---------------------------------------------------------------------------

async def test_parallel_all_pass():
    ctx = make_ctx(tty0=MockBiStream(b""), tty1=MockBiStream(b""))
    par = Parallel([
        (PassOracle("a"), "tty0"),
        (PassOracle("b"), "tty1"),
    ])
    verdict, out = await par(ctx, 5.0)
    assert isinstance(verdict, Matched)


async def test_parallel_any_fail_propagates():
    ctx = make_ctx(tty0=MockBiStream(b""), tty1=MockBiStream(b""))
    par = Parallel([
        (PassOracle("ok"), "tty0"),
        (FailOracle("broken"), "tty1"),
    ])
    verdict, _ = await par(ctx, 5.0)
    assert isinstance(verdict, Error)


async def test_parallel_new_streams_merged():
    async def adds_stream(ctx, timeout):
        ctx.streams["vm0"] = MockBiStream(b"vm output")
        return Matched("ok"), ctx

    ctx = make_ctx(tty0=MockBiStream(b""))
    par = Parallel([(adds_stream, "tty0")])
    verdict, out = await par(ctx, 5.0)
    assert "vm0" in out.streams


async def test_parallel_disjoint_stream_assertion():
    ctx = make_ctx(tty0=MockBiStream(b""))
    par = Parallel([
        (PassOracle(), "tty0"),
        (PassOracle(), "tty0"),  # duplicate — should raise
    ])
    with pytest.raises(RuntimeError, match="overlapping streams"):
        await par(ctx, 5.0)


# ---------------------------------------------------------------------------
# Race
# ---------------------------------------------------------------------------

async def test_race_first_match_wins():
    fast = MockBiStream(b"READY\r\n")
    slow = InfiniteStream(b"nothing\r\n")
    ctx = make_ctx(tty0=fast, tty1=slow)

    choice_fast = Choice("tty0", [ChoiceOption(re.compile(rb"READY"), "tty0_ready")])
    choice_slow = Choice("tty1", [ChoiceOption(re.compile(rb"DONE"), "tty1_done")])

    race = Race([(choice_fast, "tty0"), (choice_slow, "tty1")])
    verdict, _ = await race(ctx, 5.0)
    assert verdict == Matched("tty0_ready")


async def test_race_disjoint_stream_assertion():
    ctx = make_ctx(tty0=MockBiStream(b""))
    race = Race([
        (PassOracle(), "tty0"),
        (PassOracle(), "tty0"),
    ])
    with pytest.raises(RuntimeError, match="overlapping streams"):
        await race(ctx, 5.0)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

def test_list_recorder_accumulates():
    from engine.recorder import OracleStarted
    rec = ListRecorder()
    rec.emit(OracleStarted(oracle_type="PatternOracle", stream_name="tty0"))
    rec.emit(OracleVerdict(oracle_type="PatternOracle", verdict_type="Matched",
                           verdict_label="ok", elapsed=0.1))
    assert len(rec.events) == 2
    assert len(rec.verdicts()) == 1


def test_null_recorder_is_silent():
    from engine.recorder import OracleStarted
    rec = NullRecorder()
    rec.emit(OracleStarted(oracle_type="X", stream_name=None))
    rec.close()


def test_chain_recorder_writes_jsonl(tmp_path):
    import json
    from engine.recorder import ChainRecorder, OracleStarted
    rec = ChainRecorder(tmp_path)
    rec.emit(OracleStarted(oracle_type="PatternOracle", stream_name="tty0"))
    rec.close()
    lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["event_type"] == "OracleStarted"
    assert obj["oracle_type"] == "PatternOracle"
