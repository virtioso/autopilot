"""
Cut the context around each Incident marker out of a run's teed streams.

    python -m engine.incidents results/<run> [--before 30] [--after 10]

An Incident event (engine/recorder.py) is a wall-clock timestamp and a
trigger. Every stream the run read is in results/<run>/streams/<name>.raw,
verbatim, and lines that carry their own wall-clock timestamp can be sliced
by it without any per-run state:

    candump -L     (1757600000.123456) vcan0 72F#05
    metric lines   t_host=1757600000.5 red=0.83 ...

Lines without a recognised timestamp are not sliced -- a stream with none
produces no window and is listed as `unsliceable` rather than silently
empty, because "nothing in the window" and "could not tell where the window
is" must not read the same.

Output: results/<run>/incidents/<k>/{incident.json, <stream>.log ...}. Re-running
with a different window overwrites; the raw streams are never touched.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CANDUMP_TS = re.compile(rb"^\((\d+\.\d+)\)")
METRIC_TS = re.compile(rb"^t_host=(\d+\.?\d*)")


def line_time(line: bytes) -> float | None:
    m = CANDUMP_TS.match(line) or METRIC_TS.match(line)
    return float(m.group(1)) if m else None


def incidents(run_dir: Path) -> list[dict]:
    ev = run_dir / "events.jsonl"
    if not ev.exists():
        raise FileNotFoundError(f"{ev}: no event log; cannot know where the incidents are")
    out = []
    for line in ev.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("event_type") == "Incident":
            out.append(rec)
    return out


def cut(run_dir: Path, before: float, after: float) -> dict:
    """Returns a summary: per incident, per stream, the line count in the window,
    plus the streams that carried no timestamp at all."""
    marks = incidents(run_dir)
    streams = sorted((run_dir / "streams").glob("*.raw")) if (run_dir / "streams").is_dir() else []
    summary = {"run": str(run_dir), "before": before, "after": after, "incidents": []}
    for k, inc in enumerate(marks):
        t0 = float(inc["t_host"])
        d = run_dir / "incidents" / f"{k:03d}"
        d.mkdir(parents=True, exist_ok=True)
        rec = {"index": k, "t_host": t0, "trigger": inc["trigger"], "frame": inc.get("frame"),
               "t_source": inc.get("t_source"), "window": [t0 - before, t0 + after],
               "streams": {}, "unsliceable": []}
        for raw in streams:
            name = raw.stem
            n_in, n_ts = 0, 0
            with raw.open("rb") as f, (d / f"{name}.log").open("wb") as o:
                for line in f:
                    t = line_time(line)
                    if t is None:
                        continue
                    n_ts += 1
                    if t0 - before <= t <= t0 + after:
                        o.write(line); n_in += 1
            if n_ts == 0:
                rec["unsliceable"].append(name)
                (d / f"{name}.log").unlink()
            else:
                rec["streams"][name] = n_in
        (d / "incident.json").write_text(json.dumps(rec, indent=1) + "\n")
        summary["incidents"].append(rec)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--before", type=float, default=30.0)
    ap.add_argument("--after", type=float, default=10.0)
    a = ap.parse_args(argv)
    try:
        s = cut(Path(a.run_dir), a.before, a.after)
    except FileNotFoundError as exc:
        print(f"incidents: {exc}", file=sys.stderr)
        return 2
    if not s["incidents"]:
        print(f"incidents: {a.run_dir}: event log read, no Incident markers")
        return 0
    for inc in s["incidents"]:
        parts = " ".join(f"{k}={v}" for k, v in inc["streams"].items()) or "-"
        print(f"incident {inc['index']:03d} t={inc['t_host']:.3f} {inc['trigger']}: {parts}"
              + (f"  unsliceable={inc['unsliceable']}" if inc["unsliceable"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
