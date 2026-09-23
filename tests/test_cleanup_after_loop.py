"""Cleanup must see an exit AFTER the event loop has stopped, which is when it runs.

engine/runtime.py calls ctx.cleanup() in its `finally`, outside asyncio.run --
so asyncio's child watcher is no longer running and proc.returncode never leaves
None. The cleanup polled that field alone, so its 3 s wait was unconditional:
every process in every run was SIGKILLed, including ones that exited on the
SIGTERM immediately, and process.exited_cleanly was logged zero times across
every run this rig has made. Anything written after SIGTERM went with it.

The existing tests did not catch it because they call ctx.cleanup() INSIDE
asyncio.run, where the watcher does update the field -- a check exercised under
a condition its subject never meets.
"""
import asyncio, io, time
import structlog
from adapters.process import SpawnProcessOracle
from engine.oracle import StreamContext


def _capture():
    cap = []
    structlog.configure(processors=[lambda l, m, e: cap.append(dict(e)) or e,
                                    structlog.processors.JSONRenderer()],
                        logger_factory=structlog.PrintLoggerFactory(io.StringIO()))
    return cap


def _spawn(cmd, name):
    """Spawn under a loop, then let the loop finish -- the runtime's own shape."""
    ctx = StreamContext()
    async def main():
        _, c = await SpawnProcessOracle(cmd, name, None)(ctx, 5)
        return c
    return asyncio.run(main())


def test_a_process_that_takes_sigterm_is_not_sigkilled_after_the_loop_stops():
    cap = _capture()
    ctx = _spawn(["sleep", "300"], "victim")     # default SIGTERM disposition
    t = time.monotonic()
    ctx.cleanup()                                 # loop is gone, as in runtime.py
    elapsed = time.monotonic() - t
    assert any(e.get("event") == "process.exited_cleanly" for e in cap), \
        "cleanup never saw the exit: it is polling a field only the loop updates"
    assert not any(e.get("event") == "process.sigkill" for e in cap), \
        "a process that took SIGTERM was SIGKILLed anyway"
    assert elapsed < 1.0, f"waited {elapsed:.1f}s for an exit that had happened"


def test_a_process_that_ignores_sigterm_is_still_sigkilled():
    """The arm that makes the fix fail if it mistakes 'alive' for 'gone'."""
    cap = _capture()
    ctx = _spawn(["python3", "-c",
                  "import signal,time\n"
                  "signal.signal(signal.SIGTERM, lambda *a: None)\n"
                  "time.sleep(300)\n"], "stubborn")
    time.sleep(0.4)                               # let it install the handler
    ctx.cleanup()
    assert any(e.get("event") == "process.sigkill" for e in cap), \
        "a process that ignores SIGTERM must still be SIGKILLed"
    assert not any(e.get("event") == "process.exited_cleanly" for e in cap)


def test_a_process_already_dead_before_cleanup_is_not_signalled():
    cap = _capture()
    ctx = _spawn(["true"], "gone")
    time.sleep(0.3)
    ctx.cleanup()
    assert not any(e.get("event") in ("process.sigterm", "process.sigkill") for e in cap), \
        "cleanup signalled a process that had already exited"
