"""run_process failure_stops: a non-success exit ends the sequence with the
failure_label as the Error's reason, instead of a Matched that lets a later
step's verdict win (the dev chain's first-fail check)."""
import asyncio, json, tempfile
from pathlib import Path
from engine.runtime import ChainRunner


def _run(stops):
    d = Path(tempfile.mkdtemp())
    chain = {"oracle": "sequence", "steps": [
        {"oracle": "run_process", "cmd": ["false"], "success_label": "ok",
         "failure_label": "first_test_failed", "failure_stops": stops},
        {"oracle": "verdict", "label": "pass"}]}
    (d / "c.json").write_text(json.dumps(chain))
    r = ChainRunner(d / "c.json", timeout=10.0, result_base=d, run_id="run")
    asyncio.run(r.run())
    return json.load(open(r.result_dir / "verdict.json"))


def test_failure_stops_ends_sequence_with_label_as_reason():
    v = _run(True)
    assert v["verdict"] == "Error" and v["reason"] == "first_test_failed"


def test_default_failure_label_lets_sequence_continue():
    v = _run(False)
    assert v["verdict"] == "Matched" and v["label"] == "pass"
