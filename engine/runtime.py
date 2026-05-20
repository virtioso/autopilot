"""
Chain executor: load → parse → hydrate → execute → record.

Usage (low-level):
    from engine.runtime import run_chain
    from engine.oracle import StreamContext

    ctx = StreamContext()
    verdict, ctx = await run_chain(
        Path("chains/sel4test.json"),
        ctx,
        timeout=600.0,
    )

Usage (high-level, production):
    from engine.runtime import ChainRunner

    runner = ChainRunner(
        Path("chains/sel4test.json"),
        timeout=600.0,
        platform="orin-agx",
    )
    verdict = await runner.run()
    sys.exit(runner.exit_code())

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

ChainRunner: full lifecycle manager — result dir setup, recorder wiring,
  ctx.cleanup() on exit, verdict.json writing, exit code mapping.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog

from engine.oracle import Error, Matched, StreamContext, TimeoutVerdict, Verdict
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


# ---------------------------------------------------------------------------
# ChainRunner: full lifecycle manager
# ---------------------------------------------------------------------------

# Verdict → exit code mapping for CLI use
_EXIT_CODES = {
    "pass": 0,
    "ok": 0,
}
_EXIT_TIMEOUT = 2
_EXIT_ERROR = 3
_EXIT_FAIL = 1


def _verdict_exit_code(verdict: Verdict) -> int:
    if isinstance(verdict, Matched):
        return _EXIT_CODES.get(verdict.label, _EXIT_FAIL)
    if isinstance(verdict, TimeoutVerdict):
        return _EXIT_TIMEOUT
    return _EXIT_ERROR  # Error


class ChainRunner:
    """
    Full chain execution lifecycle.

    Manages:
    - Run ID and result directory creation
    - StreamContext initialisation (config values into ctx.metadata)
    - ChainRecorder wiring (events.jsonl in result_dir)
    - ctx.cleanup() on exit regardless of outcome
    - Writing verdict.json to result_dir
    - Mapping verdict → exit code for CLI

    Usage:
        runner = ChainRunner(Path("chains/sel4test.json"), platform="orin-agx")
        verdict = await runner.run()
        sys.exit(runner.exit_code())
    """

    def __init__(
        self,
        chain_path: Path,
        *,
        result_base: Path | None = None,
        run_id: str | None = None,
        timeout: float = 3600.0,
        platform: str | None = None,
        config_overrides: dict | None = None,
    ) -> None:
        self._chain_path = chain_path
        self._timeout = timeout
        self._platform = platform
        self._config_overrides = config_overrides or {}
        self._result_base = result_base or Path("results")

        if run_id is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            run_id = f"{chain_path.stem}-{ts}"
        self._run_id = run_id
        self._result_dir = self._result_base / run_id

        self._verdict: Verdict | None = None

    @property
    def result_dir(self) -> Path:
        return self._result_dir

    @property
    def run_id(self) -> str:
        return self._run_id

    def exit_code(self) -> int:
        """Return process exit code based on the last verdict. Call after run()."""
        if self._verdict is None:
            return _EXIT_ERROR
        return _verdict_exit_code(self._verdict)

    async def run(self) -> Verdict:
        """
        Execute the chain end-to-end.

        Always calls ctx.cleanup() in a finally block even if cancelled.
        Writes verdict.json to result_dir on completion.
        Returns the verdict (Matched, TimeoutVerdict, or Error).
        """
        from model.config import Config

        self._result_dir.mkdir(parents=True, exist_ok=True)
        recorder = ChainRecorder(self._result_dir)

        config = Config.load(
            platform=self._platform,
            overrides=self._config_overrides,
        )

        ctx = StreamContext()
        ctx.metadata["config"] = config.as_dict()
        ctx.metadata["run_id"] = self._run_id
        ctx.metadata["chain"] = self._chain_path.stem
        ctx.metadata["result_dir"] = str(self._result_dir)

        log.info(
            "runner.start",
            run_id=self._run_id,
            chain=str(self._chain_path),
            platform=config.get("platform"),
            timeout=self._timeout,
        )

        try:
            oracle_def = load_chain(self._chain_path)
            oracle = hydrate_chain(oracle_def)
            verdict, ctx = await execute_chain(
                oracle,
                ctx,
                self._timeout,
                recorder=recorder,
                chain_name=self._chain_path.stem,
            )
        except Exception as exc:
            log.error("runner.unhandled_error", error=repr(exc))
            verdict = Error(f"unhandled: {exc}")
        finally:
            ctx.cleanup()
            recorder.close()

        self._verdict = verdict
        self._write_verdict(verdict)

        log.info(
            "runner.done",
            run_id=self._run_id,
            verdict=type(verdict).__name__,
            label=getattr(verdict, "label", None),
            exit_code=self.exit_code(),
        )
        return verdict

    def _write_verdict(self, verdict: Verdict) -> None:
        summary = {
            "run_id": self._run_id,
            "chain": self._chain_path.stem,
            "verdict": type(verdict).__name__,
            "label": getattr(verdict, "label", None),
            "exit_code": _verdict_exit_code(verdict),
        }
        if isinstance(verdict, Error):
            summary["reason"] = verdict.reason
        path = self._result_dir / "verdict.json"
        try:
            path.write_text(json.dumps(summary, indent=2) + "\n")
        except OSError as exc:
            log.warning("runner.verdict_write_failed", error=repr(exc))
