"""A pattern that never arrives ends within the step's timeout, not never.

An unwrapped readiness wait on a process that printed nothing outlived a
20-minute chain timeout by 24 minutes (evk-dtc-20260913T163932Z)."""
import asyncio, time
from engine.primitives import PatternOracle
from engine.oracle import StreamContext, TimeoutVerdict


class Silent:
    async def read(self, n=4096):
        await asyncio.sleep(3600)
        return b""


def test_pattern_gives_up_at_its_timeout():
    ctx = StreamContext(); ctx.streams["s"] = Silent()
    t0 = time.monotonic()
    v, _ = asyncio.run(PatternOracle("s", b"READY", label="ok")(ctx, 0.5))
    assert isinstance(v, TimeoutVerdict) and time.monotonic() - t0 < 2.0
