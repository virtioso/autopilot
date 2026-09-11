"""engine/incidents.py: cut windows from teed streams around Incident markers."""
import json
from pathlib import Path

from engine.incidents import cut, line_time, main


def make_run(tmp_path: Path) -> Path:
    run = tmp_path / "run"; (run / "streams").mkdir(parents=True)
    t0 = 1757600000.0
    can = b"".join(b"(%.6f) vcan0 72F#05\n" % (t0 + i) for i in range(0, 100))          # 0..99 s
    (run / "streams" / "can0.raw").write_bytes(can)
    met = b"".join(b"t_host=%.1f red=%.2f\n" % (t0 + i, 0.1 if i < 50 else 0.9) for i in range(100))
    (run / "streams" / "frames.raw").write_bytes(met)
    (run / "streams" / "console.raw").write_bytes(b"no timestamps here\nat all\n")
    ev = [{"event_type": "OracleStarted", "oracle_type": "x", "stream_name": None, "timestamp": 1.0},
          {"event_type": "Incident", "t_host": t0 + 50, "trigger": "red>0.8", "frame": "frames/000.png",
           "t_source": None, "note": ""}]
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ev))
    return run


def test_line_time():
    assert line_time(b"(1757600000.123456) vcan0 72F#05") == 1757600000.123456
    assert line_time(b"t_host=1757600000.5 red=0.83") == 1757600000.5
    assert line_time(b"garbage") is None


def test_window_is_cut_per_stream(tmp_path):
    run = make_run(tmp_path)
    s = cut(run, before=5, after=2)
    inc = s["incidents"][0]
    assert inc["streams"] == {"can0": 8, "frames": 8}           # t0+45..t0+52 inclusive
    assert inc["unsliceable"] == ["console"]
    lines = (run / "incidents" / "000" / "can0.log").read_bytes().splitlines()
    assert lines[0].startswith(b"(1757600045.") and lines[-1].startswith(b"(1757600052.")
    assert not (run / "incidents" / "000" / "console.log").exists()
    j = json.loads((run / "incidents" / "000" / "incident.json").read_text())
    assert j["trigger"] == "red>0.8" and j["frame"] == "frames/000.png"


def test_recut_wider_overwrites(tmp_path):
    run = make_run(tmp_path)
    cut(run, 5, 2)
    s = cut(run, 30, 10)
    assert s["incidents"][0]["streams"]["can0"] == 41


def test_no_event_log_is_could_not_look(tmp_path):
    (tmp_path / "r").mkdir()
    assert main([str(tmp_path / "r")]) == 2


def test_no_incidents_is_zero_and_says_so(tmp_path, capsys):
    run = tmp_path / "r"; run.mkdir()
    (run / "events.jsonl").write_text("")
    assert main([str(run)]) == 0
    assert "no Incident markers" in capsys.readouterr().out
