"""A spawned process that dies on its own is logged with its wait status.

Before this, cleanup saw returncode already set and returned silently, so a
server killed mid-run left a record showing it started and was never stopped
(evk-dtc-20260912T155413Z: exmebus-server vanished at t+61 s with no event).
"""
import asyncio, io, os, signal
import structlog
from adapters.process import SpawnProcessOracle
from engine.oracle import StreamContext


def _capture():
    cap = []
    structlog.configure(processors=[lambda l, m, e: cap.append(dict(e)) or e,
                                    structlog.processors.JSONRenderer()],
                        logger_factory=structlog.PrintLoggerFactory(io.StringIO()))
    return cap


def test_signal_death_is_logged_with_signal():
    cap = _capture()
    async def main():
        ctx = StreamContext()
        _, ctx = await SpawnProcessOracle(["sleep", "30"], "victim", None)(ctx, 5)
        pid = next(e["pid"] for e in cap if e.get("event") == "spawn.started")
        os.kill(pid, signal.SIGKILL)
        await asyncio.sleep(0.5)
        ctx.cleanup()
    asyncio.run(main())
    ex = [e for e in cap if e.get("event") == "process.exited"]
    assert ex and ex[0]["signal"] == 9 and ex[0]["returncode"] == -9


def test_clean_exit_is_logged_without_signal():
    cap = _capture()
    async def main():
        ctx = StreamContext()
        await SpawnProcessOracle(["true"], "clean", None)(ctx, 5)
        await asyncio.sleep(0.3)
        ctx.cleanup()
    asyncio.run(main())
    ex = [e for e in cap if e.get("event") == "process.exited"]
    assert ex and ex[0]["returncode"] == 0 and "signal" not in ex[0]
