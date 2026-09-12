"""run_process captures land in the run directory, not only in ctx.metadata."""
import asyncio, json, tempfile
from pathlib import Path
from engine.runtime import ChainRunner


def test_capture_written_to_captures_dir():
    d = Path(tempfile.mkdtemp())
    chain = {"oracle": "run_process", "cmd": ["sh", "-c", "echo energised 4 rails"],
             "success_exit_codes": [0], "success_label": "ok", "failure_label": "bad",
             "capture_name": "energise"}
    cp = d / "c.json"; cp.write_text(json.dumps(chain))
    r = ChainRunner(cp, timeout=10.0, result_base=d, run_id="run")
    asyncio.run(r.run())
    got = (r.result_dir / "captures" / "energise.txt").read_bytes()
    assert b"energised 4 rails" in got
