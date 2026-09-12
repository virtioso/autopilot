"""
CAN adapter: CanSourceOracle (a bus demuxed by node id) and NodeSetOracle.

A CAN bus is one stream carrying frames from every node, and the engine's
Parallel/Race combinators require every branch to read a DISTINCT stream. So a
bus cannot be watched for N nodes by N PatternOracles on one stream. This
adapter spawns `candump -L <iface>` (or any command emitting the same line
format), pumps its lines into per-node substreams, and registers each under
`<name>/node/<id>`. A PatternOracle on `<name>/node/47` then sees only node
47's traffic, and Parallel over 25 of them is legal.

Line format (candump -L):   (1757600000.123456) vcan0 72F#05
Node id = COB-ID & 0x7F for 11-bit ids; 29-bit (J1939) frames are not node
addressed in this sense and go to `<name>/other`. Frames for nodes nobody
asked for go nowhere.

NodeSetOracle is the bring-up gate: given the node ids a manifest says must
answer on this bus, wait for a heartbeat from each within `timeout`, in
parallel, and return Matched("nodes_up") or Error("nodes_absent: 47 57").
Absence is an Error, not a Matched, because a Sequence must stop on it: a
partial node set does not give a partially working machine
(Norsmart3 docs/architecture/canopen/ten-node-bring-up.md). The present and
absent sets are also written to ctx.metadata[<name>/node_set] so the verdict
can be read after the run.

Both are pure asyncio + candump; nothing here opens a CAN socket itself, so
the same code runs against a recorded `candump -L` file replayed by `cat`.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import structlog

from engine.combinators import Parallel, ParallelBranchResult, Timeout
from engine.oracle import Error, Matched, StreamContext, Verdict
from engine.primitives import PatternOracle
from adapters.process import ProcessBiStream, make_process_cleanup

log = structlog.get_logger()

LINE_RE = re.compile(rb"^\((\d+\.\d+)\)\s+(\S+)\s+([0-9A-Fa-f]+)#")


class QueueBiStream:
    """A BiStream fed by a pump; write() is refused (the bus is written elsewhere)."""

    def __init__(self) -> None:
        self._reader = asyncio.StreamReader()

    def feed(self, data: bytes) -> None:
        self._reader.feed_data(data)

    def feed_eof(self) -> None:
        self._reader.feed_eof()

    async def read(self, n: int = 4096) -> bytes:
        return await self._reader.read(n)

    async def write(self, data: bytes) -> None:
        raise RuntimeError("a demuxed CAN substream is read-only")


class CanSourceOracle:
    """
    Spawn a candump-format producer and demux it into per-node substreams.

    cmd: e.g. ["candump", "-L", "vcan0"]; anything writing candump -L lines.
    nodes: the node ids to create substreams for. Frames for other nodes are
        counted and dropped; the count is in ctx.metadata[<name>/dropped].
    Returns Matched("source_up") as soon as the process is running; it does not
    wait for traffic, because an empty bus is a legitimate state to observe.
    """

    def __init__(self, name: str, cmd: list[str], nodes: list[int]) -> None:
        self._name = name
        self._cmd = cmd
        self._nodes = sorted(set(nodes))

    async def __call__(self, ctx: StreamContext, timeout: float) -> tuple[Verdict, StreamContext]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            return Error(f"can_source_spawn_failed: {exc}"), ctx
        ctx.register_cleanup(self._name, make_process_cleanup(proc, self._name))

        # The raw line stream goes through add_stream so the run's results hold
        # streams/<name>.raw verbatim: that file is what an incident window is
        # cut from afterwards. The pump reads through the tee, not around it.
        ctx.add_stream(self._name, ProcessBiStream(proc))
        raw = ctx.streams[self._name]

        subs: dict[int, QueueBiStream] = {n: QueueBiStream() for n in self._nodes}
        other = QueueBiStream()
        for n, s in subs.items():
            ctx.streams[f"{self._name}/node/{n}"] = s
        ctx.streams[f"{self._name}/other"] = other
        stats = {"lines": 0, "routed": 0, "dropped": 0, "unparsed": 0}
        ctx.metadata[f"{self._name}/stats"] = stats

        def route(line: bytes) -> None:
            stats["lines"] += 1
            m = LINE_RE.match(line)
            if not m:
                stats["unparsed"] += 1
                return
            cob = int(m.group(3), 16)
            if cob > 0x7FF:
                other.feed(line)
                return
            node = cob & 0x7F
            s = subs.get(node)
            if s is None:
                stats["dropped"] += 1
                return
            stats["routed"] += 1
            s.feed(line)

        async def pump() -> None:
            buf = b""
            while True:
                chunk = await raw.read(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line, buf = buf[:nl + 1], buf[nl + 1:]
                    route(line)
            if buf:
                route(buf)
            for s in subs.values():
                s.feed_eof()
            other.feed_eof()


        task = asyncio.create_task(pump(), name=f"can-pump:{self._name}")
        ctx.register_cleanup(self._name, task.cancel)
        log.info("can_source.up", name=self._name, cmd=self._cmd, nodes=len(self._nodes), pid=proc.pid)
        return Matched("source_up"), ctx


def heartbeat_pattern(node: int, states: tuple[str, ...] = ("00", "04", "05", "7F")) -> bytes:
    """A candump -L line carrying node's heartbeat (COB 0x700+id) whose FIRST byte is
    one of `states`. The vendor's heartbeat is two bytes (72F#7F00, 72F#7F80 -- state
    then a toggle), so the state byte may be followed by more hex; measured on FCA0128
    2026-09-12, where a one-byte pattern missed 563 heartbeats from node 47."""
    return rf" {0x700 + node:03X}#({'|'.join(states)})(?:[0-9A-Fa-f]{{2}})*(?![0-9A-Fa-f])".encode()


class NodeSetOracle:
    """
    Wait for a heartbeat from every node in `nodes` on source `name`, in
    parallel, each under `per_node_timeout`. Matched("nodes_up") when all
    arrive; Error("nodes_absent: <ids>") otherwise. ctx.metadata[<name>/node_set]
    records present, absent and how long each took.
    """

    def __init__(self, name: str, nodes: list[int], per_node_timeout: float,
                 states: tuple[str, ...] = ("00", "04", "05", "7F")) -> None:
        self._name = name
        self._nodes = sorted(set(nodes))
        self._t = per_node_timeout
        self._states = states
        if not self._nodes:
            raise ValueError("NodeSetOracle with no nodes cannot fail and is not a gate")

    async def __call__(self, ctx: StreamContext, timeout: float) -> tuple[Verdict, StreamContext]:
        missing_streams = [n for n in self._nodes if f"{self._name}/node/{n}" not in ctx.streams]
        if missing_streams:
            return Error(f"no substream for nodes {missing_streams}; can_source must list them"), ctx
        branches = []
        for n in self._nodes:
            s = f"{self._name}/node/{n}"
            branches.append((Timeout(PatternOracle(s, heartbeat_pattern(n, self._states), label=str(n)), self._t), s))
        nodes = self._nodes

        def reducer(results: list[ParallelBranchResult]) -> tuple[Verdict, set[str]]:
            present = [nodes[r.idx] for r in results if isinstance(r.verdict, Matched)]
            absent = [nodes[r.idx] for r in results if not isinstance(r.verdict, Matched)]
            ctx.metadata[f"{self._name}/node_set"] = {
                "required": nodes, "present": present, "absent": absent, "per_node_timeout": self._t,
            }
            if absent:
                return Error("nodes_absent: " + " ".join(map(str, absent))), set()
            return Matched("nodes_up"), set()

        verdict, ctx = await Parallel(branches, reducer=reducer)(ctx, timeout)
        log.info("node_set.verdict", name=self._name, verdict=verdict)
        return verdict, ctx


def nodes_from_manifest(path: str | Path, bus: int, tier: str = "nmt") -> list[int]:
    """Node ids on physical bus `bus` whose gate tier is `tier`, from an
    iomux2manifest file. Refuses a manifest that lacks the fields rather than
    returning an empty list, which would be a gate that cannot fail."""
    m = json.loads(Path(path).read_text())
    for k in ("serial", "nodes", "numbering"):
        if k not in m:
            raise ValueError(f"{path}: not an iomux2manifest file (no {k!r})")
    ids = [n["id"] for n in m["nodes"] if n["bus"] == bus and n["gate"] == tier]
    if not ids:
        raise ValueError(f"{path}: no {tier}-tier nodes on bus {bus}; buses present: "
                         f"{sorted({n['bus'] for n in m['nodes']})}")
    return ids


class MachineUpOracle:
    """
    The whole machine's start gate: every `nmt`-tier node on EVERY bus the
    manifest declares, in parallel, one NodeSetOracle per bus.

    sources maps the manifest's physical bus number to the can_source name
    that carries it (e.g. {0: "can0", 2: "can2"}). A bus that has nmt-tier
    nodes in the manifest but no source here is refused up front: leaving a
    bus out is how a gate silently narrows, and the machine does not start on
    a bus nobody watched. Buses with no nmt-tier nodes (J1939, spare) need no
    source. Verdict: Matched("machine_up") or Error("nodes_absent: bus0:57 bus2:112").
    """

    def __init__(self, manifest: str | Path, sources: dict[int, str], per_node_timeout: float,
                 tier: str = "nmt") -> None:
        m = json.loads(Path(manifest).read_text())
        by_bus: dict[int, list[int]] = {}
        for n in m.get("nodes", []):
            if n.get("gate") == tier:
                by_bus.setdefault(n["bus"], []).append(n["id"])
        if not by_bus:
            raise ValueError(f"{manifest}: no {tier}-tier nodes on any bus")
        unwatched = sorted(b for b in by_bus if b not in sources)
        if unwatched:
            raise ValueError(f"{manifest}: buses {unwatched} carry {tier}-tier nodes "
                             f"{ {b: by_bus[b] for b in unwatched} } but have no source")
        self._serial = m.get("serial")
        self._gates = [(b, sources[b], NodeSetOracle(sources[b], ids, per_node_timeout))
                       for b, ids in sorted(by_bus.items())]

    @property
    def buses(self) -> list[int]:
        return [b for b, _, _ in self._gates]

    async def __call__(self, ctx: StreamContext, timeout: float) -> tuple[Verdict, StreamContext]:
        # Each NodeSet reads its own source's substreams, so the branches are
        # stream-disjoint; the primary name given to Parallel is the source name.
        branches = [(g, src) for _, src, g in self._gates]
        gates = self._gates

        def reducer(results: list[ParallelBranchResult]) -> tuple[Verdict, set[str]]:
            absent = []
            for r in results:
                bus, src, _ = gates[r.idx]
                # A branch runs on a fork; its metadata does not merge back by
                # itself. Copy the per-bus record up so the run can read it.
                ns = r.fork.metadata.get(f"{src}/node_set", {})
                ctx.metadata[f"{src}/node_set"] = ns
                if not isinstance(r.verdict, Matched):
                    ids = ns.get("absent") or ["?"]
                    absent.append(f"bus{bus}:" + ",".join(map(str, ids)))
            ctx.metadata["machine_up"] = {"serial": self._serial, "buses": [b for b, _, _ in gates],
                                          "absent": absent}
            if absent:
                return Error("nodes_absent: " + " ".join(absent)), set()
            return Matched("machine_up"), set()

        verdict, ctx = await Parallel(branches, reducer=reducer)(ctx, timeout)
        log.info("machine_up.verdict", serial=self._serial, verdict=verdict)
        return verdict, ctx
