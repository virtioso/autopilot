"""A spawned process nobody reads must not block on a full stdout pipe, and
streams/<name>.raw must hold everything it wrote.

Ten EVK runs had every process's .raw file end at its readiness banner: the
tee wrote only what an oracle read, nothing read after readiness, the 64 KiB
pipe filled, and the ExMeBus server then wedged (pid alive) or died (exit 1,
traceback lost into the same pipe). The pump drains continuously."""
import asyncio, io, json, tempfile
from pathlib import Path
import structlog
from adapters.process import SpawnProcessOracle
from engine.oracle import StreamContext

BIG = 1_000_000                              # ~15 x the pipe buffer


def _capture():
    cap = []
    structlog.configure(processors=[lambda l, m, e: cap.append(dict(e)) or e,
                                    structlog.processors.JSONRenderer()],
                        logger_factory=structlog.PrintLoggerFactory(io.StringIO()))
    return cap


def test_unread_chatty_process_finishes_and_is_recorded():
    cap = _capture()
    d = Path(tempfile.mkdtemp())
    script = f"echo READY; python3 -c \"import sys; sys.stdout.write('x'*{BIG}); sys.stdout.flush()\"; echo DONE"
    async def main():
        ctx = StreamContext(); ctx.metadata["result_dir"] = str(d)
        v, ctx = await SpawnProcessOracle(["sh", "-c", script], "chatty", "READY")(ctx, 10)
        assert type(v).__name__ == "Matched"
        # nobody reads the stream; the process must still finish on its own
        for _ in range(100):
            if any(e.get("event") == "process.exited" for e in cap): break
            await asyncio.sleep(0.1)
        ctx.cleanup()
    asyncio.run(main())
    ex = [e for e in cap if e.get("event") == "process.exited"]
    assert ex and ex[0]["returncode"] == 0, "the process blocked on a full pipe (or died)"
    raw = (d / "streams" / "chatty.raw").read_bytes()
    assert raw.count(b"x") == BIG and raw.endswith(b"DONE\n"), f"tee holds {len(raw)} bytes"
