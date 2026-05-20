"""
Chain schema, factory, and runtime tests.

Tests:
  - Pydantic schema parsing (JSON → OracleDef)
  - OracleFactory hydration (OracleDef → live oracle)
  - End-to-end execution via engine/runtime.py using mock streams
  - Migrated chain files in chains/ directory

No hardware required. Mock BiStreams substitute for real UART/SSH/process
streams so oracle execution can be verified without any physical devices.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from engine.oracle import Error, Matched, StreamContext, TimeoutVerdict
from engine.combinators import Timeout
from engine.recorder import ListRecorder

# Chains directory (relative to project root, resolved at test time)
CHAINS_DIR = Path(__file__).parent.parent / "chains"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class MockBiStream:
    """Feed fixed bytes then stall (simulates stream with no more data pending)."""
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._reader: asyncio.StreamReader | None = None

    def _get_reader(self) -> asyncio.StreamReader:
        if self._reader is None:
            self._reader = asyncio.StreamReader()
            self._reader.feed_data(self._data)
            self._reader.feed_eof()
        return self._reader

    async def read(self, n: int = 4096) -> bytes:
        return await self._get_reader().read(n)

    async def write(self, data: bytes) -> None:
        pass


def make_ctx(**streams) -> StreamContext:
    return StreamContext(streams=dict(streams))


def parse_oracle(data: dict):
    from model.chain import OracleFactory
    return OracleFactory.parse(data)


def hydrate(oracle_def):
    from model.chain import OracleFactory
    return OracleFactory.hydrate(oracle_def)


# ---------------------------------------------------------------------------
# Schema parsing — VerdictDef
# ---------------------------------------------------------------------------

def test_parse_verdict_def():
    d = parse_oracle({"oracle": "verdict", "label": "pass"})
    from model.chain import VerdictDef
    assert isinstance(d, VerdictDef)
    assert d.label == "pass"


def test_parse_verdict_defaults():
    d = parse_oracle({"oracle": "verdict"})
    assert d.label == "ok"


# ---------------------------------------------------------------------------
# Schema parsing — PatternDef
# ---------------------------------------------------------------------------

def test_parse_pattern_def():
    d = parse_oracle({
        "oracle": "pattern",
        "stream": "tty0",
        "pattern": "READY",
        "label": "boot_done",
    })
    from model.chain import PatternDef
    assert isinstance(d, PatternDef)
    assert d.stream == "tty0"
    assert d.pattern == "READY"
    assert d.label == "boot_done"


def test_parse_pattern_max_buf():
    d = parse_oracle({"oracle": "pattern", "stream": "s", "pattern": "X", "max_buf": 512})
    assert d.max_buf == 512


# ---------------------------------------------------------------------------
# Schema parsing — SequenceDef (recursive)
# ---------------------------------------------------------------------------

def test_parse_sequence_with_nested_oracles():
    d = parse_oracle({
        "oracle": "sequence",
        "steps": [
            {"oracle": "verdict", "label": "step1"},
            {"oracle": "verdict", "label": "step2"},
        ]
    })
    from model.chain import SequenceDef, VerdictDef
    assert isinstance(d, SequenceDef)
    assert len(d.steps) == 2
    assert isinstance(d.steps[0], VerdictDef)
    assert d.steps[0].label == "step1"


# ---------------------------------------------------------------------------
# Schema parsing — TimeoutDef (recursive)
# ---------------------------------------------------------------------------

def test_parse_timeout_with_nested_step():
    d = parse_oracle({
        "oracle": "timeout",
        "seconds": 30.0,
        "step": {"oracle": "verdict", "label": "ok"},
    })
    from model.chain import TimeoutDef, VerdictDef
    assert isinstance(d, TimeoutDef)
    assert d.seconds == 30.0
    assert isinstance(d.step, VerdictDef)


# ---------------------------------------------------------------------------
# Schema parsing — ChoiceDef
# ---------------------------------------------------------------------------

def test_parse_choice_def():
    d = parse_oracle({
        "oracle": "choice",
        "stream": "tty0",
        "options": [
            {"pattern": "PASS", "label": "pass"},
            {"pattern": "FAIL", "label": "fail"},
        ]
    })
    from model.chain import ChoiceDef
    assert isinstance(d, ChoiceDef)
    assert len(d.options) == 2
    assert d.options[0].label == "pass"


# ---------------------------------------------------------------------------
# Schema parsing — unknown oracle type
# ---------------------------------------------------------------------------

def test_parse_unknown_oracle_type_raises():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        parse_oracle({"oracle": "does_not_exist", "x": 1})


# ---------------------------------------------------------------------------
# Factory hydration — VerdictOracle
# ---------------------------------------------------------------------------

async def test_hydrate_verdict_executes():
    d = parse_oracle({"oracle": "verdict", "label": "pass"})
    oracle = hydrate(d)
    ctx = make_ctx()
    verdict, out = await oracle(ctx, 5.0)
    assert verdict == Matched("pass")
    assert out is ctx


# ---------------------------------------------------------------------------
# Factory hydration — PatternOracle
# ---------------------------------------------------------------------------

async def test_hydrate_pattern_matches():
    d = parse_oracle({
        "oracle": "pattern",
        "stream": "tty0",
        "pattern": "READY",
        "label": "boot_done",
    })
    oracle = hydrate(d)
    ctx = make_ctx(tty0=MockBiStream(b"boot...\r\nREADY\r\n"))
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("boot_done")


# ---------------------------------------------------------------------------
# Factory hydration — ChoiceOracle
# ---------------------------------------------------------------------------

async def test_hydrate_choice_first_match_wins():
    d = parse_oracle({
        "oracle": "choice",
        "stream": "tty0",
        "options": [
            {"pattern": "PASS", "label": "pass"},
            {"pattern": "FAIL", "label": "fail"},
        ]
    })
    oracle = hydrate(d)
    ctx = make_ctx(tty0=MockBiStream(b"...PASS...\r\n"))
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("pass")


async def test_hydrate_choice_second_option_matches():
    d = parse_oracle({
        "oracle": "choice",
        "stream": "tty0",
        "options": [
            {"pattern": "PASS", "label": "pass"},
            {"pattern": "FAIL", "label": "fail"},
        ]
    })
    oracle = hydrate(d)
    ctx = make_ctx(tty0=MockBiStream(b"...FAIL...\r\n"))
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("fail")


# ---------------------------------------------------------------------------
# Factory hydration — Sequence
# ---------------------------------------------------------------------------

async def test_hydrate_sequence_threads_context():
    d = parse_oracle({
        "oracle": "sequence",
        "steps": [
            {"oracle": "verdict", "label": "step1"},
            {"oracle": "verdict", "label": "step2"},
        ]
    })
    oracle = hydrate(d)
    ctx = make_ctx()
    verdict, out = await oracle(ctx, 5.0)
    assert verdict == Matched("step2")
    assert out is ctx


async def test_hydrate_sequence_short_circuits_on_error():
    d = parse_oracle({
        "oracle": "sequence",
        "steps": [
            {"oracle": "pattern", "stream": "tty0", "pattern": "NEVER", "label": "x"},
            {"oracle": "verdict", "label": "should_not_reach"},
        ]
    })
    oracle = hydrate(d)
    ctx = make_ctx(tty0=MockBiStream(b"no match"))
    verdict, _ = await oracle(ctx, 5.0)
    # Pattern hits EOF → Error, sequence short-circuits
    assert isinstance(verdict, Error)


# ---------------------------------------------------------------------------
# Factory hydration — Timeout
# ---------------------------------------------------------------------------

async def test_hydrate_timeout_returns_timeout_verdict():
    class StallStream:
        async def read(self, n=4096):
            await asyncio.sleep(100)
            return b""
        async def write(self, data): pass

    d = parse_oracle({
        "oracle": "timeout",
        "seconds": 0.05,
        "step": {"oracle": "pattern", "stream": "tty0", "pattern": "NEVER"},
    })
    oracle = hydrate(d)
    ctx = make_ctx(tty0=StallStream())
    verdict, _ = await oracle(ctx, 5.0)
    assert isinstance(verdict, TimeoutVerdict)


# ---------------------------------------------------------------------------
# Factory hydration — SpawnProcessOracle via schema
# ---------------------------------------------------------------------------

async def test_hydrate_spawn_process():
    d = parse_oracle({
        "oracle": "spawn_process",
        "cmd": ["bash", "-c", "echo ready; sleep 30"],
        "stream": "proc0",
        "ready_pattern": "ready",
        "ready_label": "proc_ready",
    })
    oracle = hydrate(d)
    ctx = make_ctx()
    verdict, out = await oracle(ctx, 5.0)

    assert verdict == Matched("proc_ready")
    assert "proc0" in out.streams
    stream = out.streams["proc0"]
    out.cleanup()
    await asyncio.wait_for(stream.wait(), timeout=3.0)


# ---------------------------------------------------------------------------
# Factory hydration — RunProcess via schema
# ---------------------------------------------------------------------------

async def test_hydrate_run_process_success():
    d = parse_oracle({
        "oracle": "run_process",
        "cmd": ["echo", "hello"],
        "success_label": "done",
    })
    oracle = hydrate(d)
    ctx = make_ctx()
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("done")


async def test_hydrate_run_process_failure():
    d = parse_oracle({
        "oracle": "run_process",
        "cmd": ["false"],
        "failure_label": "failed",
    })
    oracle = hydrate(d)
    ctx = make_ctx()
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("failed")


# ---------------------------------------------------------------------------
# runtime.load_chain — migrated chain files
# ---------------------------------------------------------------------------

async def test_load_chain_noop():
    from engine.runtime import load_chain
    oracle_def = load_chain(CHAINS_DIR / "post_test_fallback_noop.json")
    from model.chain import VerdictDef
    assert isinstance(oracle_def, VerdictDef)
    assert oracle_def.label == "pass"


async def test_load_chain_wait_for_elfloader():
    from engine.runtime import load_chain
    from model.chain import TimeoutDef
    oracle_def = load_chain(CHAINS_DIR / "wait_for_elfloader.json")
    assert isinstance(oracle_def, TimeoutDef)
    assert oracle_def.seconds == 300


async def test_load_chain_sel4test():
    from engine.runtime import load_chain
    from model.chain import SequenceDef
    oracle_def = load_chain(CHAINS_DIR / "sel4test.json")
    assert isinstance(oracle_def, SequenceDef)
    assert len(oracle_def.steps) >= 2


# ---------------------------------------------------------------------------
# runtime.run_chain — end-to-end execution
# ---------------------------------------------------------------------------

async def test_run_chain_noop():
    from engine.runtime import run_chain
    verdict, ctx = await run_chain(CHAINS_DIR / "post_test_fallback_noop.json")
    assert verdict == Matched("pass")


async def test_run_chain_wait_for_elfloader_pass():
    """wait_for_elfloader chain: tty0 contains ELF-loader string → Matched("pass")."""
    from engine.runtime import hydrate_chain, load_chain, execute_chain

    oracle_def = load_chain(CHAINS_DIR / "wait_for_elfloader.json")
    oracle = hydrate_chain(oracle_def)

    ctx = make_ctx(tty0=MockBiStream(b"Starting up...\r\nELF-loader started on CPU:0\r\n"))
    verdict, _ = await execute_chain(oracle, ctx, timeout=5.0)
    assert verdict == Matched("pass")


async def test_run_chain_wait_for_elfloader_fail():
    """wait_for_elfloader chain: 'not recognized' pattern → Matched("fail")."""
    from engine.runtime import hydrate_chain, load_chain, execute_chain

    oracle_def = load_chain(CHAINS_DIR / "wait_for_elfloader.json")
    oracle = hydrate_chain(oracle_def)

    ctx = make_ctx(tty0=MockBiStream(
        b"'sel4.img' is not recognized as an internal or external command\r\n"
    ))
    verdict, _ = await execute_chain(oracle, ctx, timeout=5.0)
    assert verdict == Matched("fail")


async def test_run_chain_wait_for_elfloader_timeout():
    """wait_for_elfloader chain: no pattern in stream → TimeoutVerdict."""
    class InfiniteStream:
        async def read(self, n=4096):
            await asyncio.sleep(10)
            return b"nothing"
        async def write(self, data): pass

    from engine.runtime import hydrate_chain, load_chain, execute_chain

    oracle_def = load_chain(CHAINS_DIR / "wait_for_elfloader.json")
    oracle = hydrate_chain(oracle_def)

    ctx = make_ctx(tty0=InfiniteStream())
    # Wrap in outer Timeout to terminate in test time (the chain's 300s is too long)
    wrapped = Timeout(oracle, 0.05)
    verdict, _ = await wrapped(ctx, 0.05)
    assert isinstance(verdict, TimeoutVerdict)


# ---------------------------------------------------------------------------
# runtime.run_chain — recorder integration
# ---------------------------------------------------------------------------

async def test_run_chain_records_verdict():
    from engine.runtime import run_chain
    recorder = ListRecorder()
    await run_chain(CHAINS_DIR / "post_test_fallback_noop.json", recorder=recorder)

    from engine.recorder import OracleVerdict
    verdicts = recorder.of_type(OracleVerdict)
    assert len(verdicts) == 1
    assert verdicts[0].verdict_type == "Matched"
    assert verdicts[0].verdict_label == "pass"


# ---------------------------------------------------------------------------
# chain_ref — load and execute a referenced chain file
# ---------------------------------------------------------------------------

async def test_chain_ref_hydrates_and_executes(tmp_path):
    """chain_ref loads an external JSON file and executes it as an oracle."""
    sub_chain = tmp_path / "sub.json"
    sub_chain.write_text(json.dumps({"oracle": "verdict", "label": "sub_done"}))

    from engine.runtime import load_chain, hydrate_chain, execute_chain
    oracle_def = load_chain.__func__ if hasattr(load_chain, '__func__') else load_chain

    # Parse a chain that references sub.json
    from model.chain import OracleFactory
    data = {"oracle": "chain_ref", "path": str(sub_chain)}
    d = OracleFactory.parse(data)
    oracle = OracleFactory.hydrate(d)

    ctx = make_ctx()
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("sub_done")
