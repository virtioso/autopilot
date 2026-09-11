"""
adapters/can.py: CanSourceOracle demux + NodeSetOracle, with no CAN socket.

The producer is a python one-liner printing candump -L lines, so the arms run
anywhere. The negative arm is the one that matters: withhold one node and the
verdict must name it.
"""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from adapters.can import CanSourceOracle, NodeSetOracle, heartbeat_pattern, nodes_from_manifest
from engine.oracle import Error, Matched, StreamContext


def producer(nodes: list[int], repeat: int = 3, period: float = 0.05) -> list[str]:
    script = textwrap.dedent(f"""
        import sys, time
        nodes = {nodes!r}
        t = 1757600000.0
        for i in range({repeat}):
            for n in nodes:
                print("(%.6f) vcan0 %03X#05" % (t, 0x700 + n)); t += 0.001
            print("(%.6f) vcan0 18FEF100#00000000" % t)        # a J1939 frame -> other
            print("garbage line")                                # -> unparsed
            sys.stdout.flush(); time.sleep({period})
        time.sleep(5)
    """)
    return [sys.executable, "-u", "-c", script]


MANIFEST = {
    "serial": "TEST", "numbering": "PHYBUSN_AT_LINUX",
    "nodes": [
        {"id": 47, "bus": 0, "gate": "nmt"}, {"id": 57, "bus": 0, "gate": "nmt"},
        {"id": 97, "bus": 0, "gate": "nmt"}, {"id": 48, "bus": 0, "gate": "probe"},
        {"id": 90, "bus": 0, "gate": "monitor"}, {"id": 51, "bus": 2, "gate": "nmt"},
    ],
}


async def bring_up(on_bus: list[int], required: list[int]):
    ctx = StreamContext()
    v, ctx = await CanSourceOracle("can0", producer(on_bus), nodes=required + [90])(ctx, 5.0)
    assert v == Matched("source_up")
    try:
        return await NodeSetOracle("can0", required, per_node_timeout=1.0)(ctx, 5.0)
    finally:
        ctx.cleanup()


async def test_all_nodes_present():
    v, ctx = await bring_up([47, 57, 97, 90], [47, 57, 97])
    assert v == Matched("nodes_up")
    ns = ctx.metadata["can0/node_set"]
    assert ns["present"] == [47, 57, 97] and ns["absent"] == []
    st = ctx.metadata["can0/stats"]
    assert st["routed"] >= 3 and st["dropped"] == 0   # counted at the moment the gate closed, not at EOF


async def test_one_node_absent_is_named():
    v, ctx = await bring_up([47, 97, 90], [47, 57, 97])
    assert isinstance(v, Error) and v.reason == "nodes_absent: 57"
    ns = ctx.metadata["can0/node_set"]
    assert ns["present"] == [47, 97] and ns["absent"] == [57]


async def test_wrong_state_does_not_count():
    """A node heartbeating an unlisted state is not 'up' for that state set."""
    ctx = StreamContext()
    v, ctx = await CanSourceOracle("can0", producer([47]), nodes=[47])(ctx, 5.0)
    assert v == Matched("source_up")
    v, ctx = await NodeSetOracle("can0", [47], per_node_timeout=0.5, states=("7F",))(ctx, 5.0)
    ctx.cleanup()
    assert isinstance(v, Error) and v.reason == "nodes_absent: 47"


async def test_frames_for_unlisted_nodes_are_dropped_and_counted():
    ctx = StreamContext()
    v, ctx = await CanSourceOracle("can0", producer([47, 61]), nodes=[47])(ctx, 5.0)
    v, ctx = await NodeSetOracle("can0", [47], per_node_timeout=1.0)(ctx, 5.0)
    ctx.cleanup()
    assert v == Matched("nodes_up")
    assert ctx.metadata["can0/stats"]["dropped"] >= 1


async def test_missing_substream_is_an_error_not_a_pass():
    ctx = StreamContext()
    v, ctx = await CanSourceOracle("can0", producer([47]), nodes=[47])(ctx, 5.0)
    v, ctx = await NodeSetOracle("can0", [47, 57], per_node_timeout=0.5)(ctx, 5.0)
    ctx.cleanup()
    assert isinstance(v, Error) and "57" in v.reason and "substream" in v.reason


def test_heartbeat_pattern_is_node_specific():
    import re
    p = re.compile(heartbeat_pattern(47))
    assert p.search(b"(1.0) vcan0 72F#05")
    assert not p.search(b"(1.0) vcan0 72E#05")       # node 46
    assert not p.search(b"(1.0) vcan0 5AF#05")       # SDO response, not heartbeat


def test_nodes_from_manifest(tmp_path):
    p = tmp_path / "m.json"; p.write_text(json.dumps(MANIFEST))
    assert nodes_from_manifest(p, 0, "nmt") == [47, 57, 97]
    assert nodes_from_manifest(p, 2, "nmt") == [51]
    with pytest.raises(ValueError):
        nodes_from_manifest(p, 1, "nmt")             # no nodes on bus 1: refuse, not []
    with pytest.raises(ValueError):
        NodeSetOracle("x", [], 1.0)                  # empty gate refused


def test_empty_manifest_is_refused(tmp_path):
    p = tmp_path / "m.json"; p.write_text("{}")
    with pytest.raises(ValueError):
        nodes_from_manifest(p, 0)


async def test_raw_candump_is_teed_to_results(tmp_path):
    ctx = StreamContext()
    ctx.metadata["result_dir"] = str(tmp_path)
    v, ctx = await CanSourceOracle("can0", producer([47]), nodes=[47])(ctx, 5.0)
    v, ctx = await NodeSetOracle("can0", [47], per_node_timeout=1.0)(ctx, 5.0)
    ctx.cleanup()
    assert v == Matched("nodes_up")
    raw = (tmp_path / "streams" / "can0.raw").read_bytes()
    assert b" 72F#05" in raw and b"garbage line" in raw      # verbatim, unparsed lines included
