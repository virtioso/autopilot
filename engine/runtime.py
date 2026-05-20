"""
Chain executor: load → parse → hydrate → execute → record.

Usage:
    from engine.runtime import run_chain
    from engine.oracle import StreamContext
    from engine.recorder import NullRecorder

    ctx = StreamContext()
    verdict, ctx = await run_chain(
        Path("chains/sel4test.json"),
        ctx,
        timeout=600.0,
    )

The runtime is intentionally thin — orchestration logic lives in the oracle
tree itself (Sequence, Choice, Timeout, ...). The runtime only loads, hydrates,
executes, and records.

load_chain(path): reads JSON, parses to OracleDef (Pydantic validation).
  Raises: json.JSONDecodeError, pydantic.ValidationError

hydrate_chain(oracle_def): delegates to OracleFactory.hydrate().
  Raises: ValueError for unknown oracle types.

execute_chain(oracle, ctx, timeout, recorder): calls oracle(ctx, timeout).
  Records OracleStarted and OracleVerdict events around execution.
  Always returns (verdict, ctx) — never raises on verdict.

run_chain(...): convenience wrapper combining all three phases.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import structlog

from engine.oracle import StreamContext, Verdict
from engine.recorder import ChainRecorder, NullRecorder

log = structlog.get_logger()


def load_chain(path: Path) -> object:
    """
    Read and parse a chain JSON file into an OracleDef.

    The JSON may be either a bare OracleDef object (has an 'oracle' field)
    or a ChainDef wrapper (has a 'root' field). Both are supported.

    Raises json.JSONDecodeError on invalid JSON, pydantic.ValidationError
    on schema violations.
    """
    from model.chain import OracleFactory

    data = json.loads(path.read_text())

    # Bare OracleDef: has 'oracle' discriminator field
    if "oracle" in data:
        return OracleFactory.parse(data)

    # ChainDef wrapper: has 'root' field
    if "root" in data:
        return OracleFactory.parse(data["root"])

    raise ValueError(
        f"Chain file {path} has neither 'oracle' nor 'root' field. "
        "Expected a bare OracleDef or a ChainDef wrapper."
    )


def hydrate_chain(oracle_def: object) -> object:
    """Build a live oracle from an OracleDef."""
    from model.chain import OracleFactory
    return OracleFactory.hydrate(oracle_def)


async def execute_chain(
    oracle: object,
    ctx: StreamContext,
    timeout: float,
    recorder: ChainRecorder | None = None,
    chain_name: str | None = None,
) -> tuple[Verdict, StreamContext]:
    """
    Execute a hydrated oracle and record start/verdict events.

    recorder defaults to NullRecorder if not provided.
    chain_name is used only for logging; it does not affect execution.
    """
    from engine.recorder import NullRecorder, OracleStarted, OracleVerdict

    if recorder is None:
        recorder = NullRecorder()

    label = chain_name or getattr(oracle, "__name__", type(oracle).__name__)
    log.info("runtime.chain_start", chain=label, timeout=timeout)

    recorder.emit(OracleStarted(oracle_type=label, stream_name=None))
    t0 = time.monotonic()

    verdict, ctx = await oracle(ctx, timeout)

    elapsed = time.monotonic() - t0
    recorder.emit(
        OracleVerdict(
            oracle_type=label,
            verdict_type=type(verdict).__name__,
            verdict_label=getattr(verdict, "label", None),
            elapsed=elapsed,
        )
    )

    log.info(
        "runtime.chain_done",
        chain=label,
        verdict=type(verdict).__name__,
        label=getattr(verdict, "label", None),
        elapsed_s=round(elapsed, 3),
    )
    return verdict, ctx


async def run_chain(
    path: Path,
    ctx: StreamContext | None = None,
    timeout: float = 3600.0,
    recorder: ChainRecorder | None = None,
) -> tuple[Verdict, StreamContext]:
    """
    Full pipeline: load → hydrate → execute.

    ctx defaults to an empty StreamContext if not provided.
    timeout defaults to 1 hour.
    recorder defaults to NullRecorder.
    """
    if ctx is None:
        ctx = StreamContext()

    oracle_def = load_chain(path)
    oracle = hydrate_chain(oracle_def)
    return await execute_chain(
        oracle,
        ctx,
        timeout,
        recorder=recorder,
        chain_name=path.stem,
    )
