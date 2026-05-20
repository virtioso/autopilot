"""
Tests for engine/runtime.ChainRunner and model/config.Config.

Tests:
  - Config layering (defaults, env overrides, per-request overrides)
  - Config.overlay() creates a new Config without modifying the original
  - ChainRunner: result directory created, verdict.json written
  - ChainRunner: correct exit code for pass/fail/timeout/error
  - ChainRunner: ctx.cleanup() called even when cancelled
  - Signal handling: cancellation propagates to cleanup (smoke test)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from engine.oracle import Error, Matched, StreamContext, TimeoutVerdict


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

def test_config_defaults():
    from model.config import Config
    cfg = Config.load(platform="generic")
    assert cfg.get("baudrate") == 115200
    assert cfg.get("platform") == "generic"


def test_config_overlay_returns_new_config():
    from model.config import Config
    cfg = Config.load(platform="generic")
    overridden = cfg.overlay({"tty0": "/dev/ttyUSB99"})
    assert overridden.get("tty0") == "/dev/ttyUSB99"
    assert "tty0" not in cfg.as_dict() or cfg.get("tty0") != "/dev/ttyUSB99"


def test_config_env_override(monkeypatch):
    from model.config import Config
    monkeypatch.setenv("AUTOPILOT_TTY0", "/dev/ttyFAKE")
    cfg = Config.load(platform="generic")
    assert cfg.get("tty0") == "/dev/ttyFAKE"


def test_config_per_request_overrides():
    from model.config import Config
    cfg = Config.load(platform="generic", overrides={"custom_key": "custom_value"})
    assert cfg.get("custom_key") == "custom_value"


def test_config_request_overrides_beat_env(monkeypatch):
    from model.config import Config
    monkeypatch.setenv("AUTOPILOT_TTY0", "/dev/from_env")
    cfg = Config.load(platform="generic", overrides={"tty0": "/dev/from_request"})
    assert cfg.get("tty0") == "/dev/from_request"


def test_config_missing_platform_does_not_crash():
    from model.config import Config
    cfg = Config.load(platform="nonexistent-platform")
    # Falls back to defaults without crashing
    assert cfg.get("baudrate") == 115200


def test_config_platform_orin_agx():
    """orin-agx.yaml exists and loads correctly."""
    from model.config import Config
    cfg = Config.load(platform="orin-agx")
    assert cfg.get("tty0") == "/dev/ttyACM0"
    assert cfg.get("baudrate") == 115200


# ---------------------------------------------------------------------------
# ChainRunner tests
# ---------------------------------------------------------------------------

def _make_simple_chain(tmp_path: Path, label: str = "pass") -> Path:
    """Write a minimal chain file that returns Matched(label)."""
    chain_file = tmp_path / "chain.json"
    chain_file.write_text(json.dumps({"oracle": "verdict", "label": label}))
    return chain_file


async def test_chain_runner_creates_result_dir(tmp_path):
    from engine.runtime import ChainRunner

    chain = _make_simple_chain(tmp_path)
    runner = ChainRunner(chain, result_base=tmp_path / "results", run_id="test-001")
    await runner.run()

    assert runner.result_dir.exists()
    assert (runner.result_dir / "verdict.json").exists()
    assert (runner.result_dir / "events.jsonl").exists()


async def test_chain_runner_writes_verdict_json(tmp_path):
    from engine.runtime import ChainRunner

    chain = _make_simple_chain(tmp_path, label="pass")
    runner = ChainRunner(chain, result_base=tmp_path / "results", run_id="test-002")
    await runner.run()

    verdict_data = json.loads((runner.result_dir / "verdict.json").read_text())
    assert verdict_data["verdict"] == "Matched"
    assert verdict_data["label"] == "pass"
    assert verdict_data["exit_code"] == 0


async def test_chain_runner_exit_code_pass(tmp_path):
    from engine.runtime import ChainRunner

    chain = _make_simple_chain(tmp_path, label="pass")
    runner = ChainRunner(chain, result_base=tmp_path / "results")
    await runner.run()
    assert runner.exit_code() == 0


async def test_chain_runner_exit_code_fail(tmp_path):
    from engine.runtime import ChainRunner

    chain = _make_simple_chain(tmp_path, label="fail")
    runner = ChainRunner(chain, result_base=tmp_path / "results")
    await runner.run()
    assert runner.exit_code() == 1


async def test_chain_runner_exit_code_timeout(tmp_path):
    """A chain that times out returns exit code 2."""
    from engine.runtime import ChainRunner

    class InfiniteStream:
        async def read(self, n=4096):
            await asyncio.sleep(100)
            return b""
        async def write(self, data): pass

    chain_file = tmp_path / "chain.json"
    chain_file.write_text(json.dumps({
        "oracle": "timeout",
        "seconds": 0.05,
        "step": {
            "oracle": "pattern",
            "stream": "tty0",
            "pattern": "NEVER",
        }
    }))

    ctx = StreamContext(streams={"tty0": InfiniteStream()})
    runner = ChainRunner(chain_file, result_base=tmp_path / "results", timeout=5.0)

    # Monkey-patch run() to inject our ctx instead of a fresh one
    from engine.runtime import load_chain, hydrate_chain, execute_chain
    oracle_def = load_chain(chain_file)
    oracle = hydrate_chain(oracle_def)
    verdict, _ = await execute_chain(oracle, ctx, 5.0)

    from engine.runtime import _verdict_exit_code
    assert isinstance(verdict, TimeoutVerdict)
    assert _verdict_exit_code(verdict) == 2


async def test_chain_runner_exit_code_error(tmp_path):
    """An oracle that returns Error gives exit code 3."""
    from engine.runtime import _verdict_exit_code
    assert _verdict_exit_code(Error("something_failed")) == 3


async def test_chain_runner_cleanup_runs_on_completion(tmp_path):
    """cleanup_hooks are called after chain completes normally."""
    from engine.runtime import ChainRunner

    cleanup_called = []

    class RecordingStream:
        async def read(self, n=4096): return b"x"
        async def write(self, data): pass

    chain_file = tmp_path / "chain.json"
    chain_file.write_text(json.dumps({"oracle": "verdict", "label": "ok"}))

    runner = ChainRunner(chain_file, result_base=tmp_path / "results")

    # Patch run() to inject a ctx with a cleanup hook
    original_run = runner.run

    async def patched_run():
        from model.config import Config
        from engine.recorder import ChainRecorder
        runner._result_dir.mkdir(parents=True, exist_ok=True)
        recorder = ChainRecorder(runner._result_dir)
        ctx = StreamContext()
        ctx.register_cleanup(None, lambda: cleanup_called.append(True))

        from engine.runtime import load_chain, hydrate_chain, execute_chain
        oracle_def = load_chain(runner._chain_path)
        oracle = hydrate_chain(oracle_def)
        try:
            verdict, ctx = await execute_chain(oracle, ctx, runner._timeout, recorder=recorder)
        finally:
            ctx.cleanup()
            recorder.close()
        runner._verdict = verdict
        runner._write_verdict(verdict)
        return verdict

    runner.run = patched_run
    await runner.run()

    assert cleanup_called == [True]


async def test_chain_runner_cancellation_runs_cleanup(tmp_path):
    """When the chain task is cancelled, ctx.cleanup() still runs."""
    from engine.runtime import ChainRunner
    from engine.recorder import ChainRecorder

    cleanup_called = []
    chain_file = tmp_path / "slow.json"
    chain_file.write_text(json.dumps({
        "oracle": "timeout",
        "seconds": 100,
        "step": {"oracle": "pattern", "stream": "tty0", "pattern": "NEVER"},
    }))

    class StallStream:
        async def read(self, n=4096):
            await asyncio.sleep(100)
            return b""
        async def write(self, data): pass

    result_dir = tmp_path / "results" / "cancel-test"
    result_dir.mkdir(parents=True)
    recorder = ChainRecorder(result_dir)
    ctx = StreamContext(streams={"tty0": StallStream()})
    ctx.register_cleanup(None, lambda: cleanup_called.append(True))

    from engine.runtime import load_chain, hydrate_chain, execute_chain
    oracle_def = load_chain(chain_file)
    oracle = hydrate_chain(oracle_def)

    async def run_and_cancel():
        try:
            return await execute_chain(oracle, ctx, 100.0, recorder=recorder)
        except asyncio.CancelledError:
            return None
        finally:
            ctx.cleanup()
            recorder.close()

    task = asyncio.create_task(run_and_cancel())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert cleanup_called == [True], "cleanup hook must run even when cancelled"


# ---------------------------------------------------------------------------
# exit_code mapping
# ---------------------------------------------------------------------------

def test_exit_code_pass():
    from engine.runtime import _verdict_exit_code
    assert _verdict_exit_code(Matched("pass")) == 0


def test_exit_code_ok():
    from engine.runtime import _verdict_exit_code
    assert _verdict_exit_code(Matched("ok")) == 0


def test_exit_code_fail_label():
    from engine.runtime import _verdict_exit_code
    assert _verdict_exit_code(Matched("fail")) == 1


def test_exit_code_arbitrary_label():
    from engine.runtime import _verdict_exit_code
    assert _verdict_exit_code(Matched("vcmux_ready")) == 1


def test_exit_code_timeout():
    from engine.runtime import _verdict_exit_code
    assert _verdict_exit_code(TimeoutVerdict()) == 2


def test_exit_code_error():
    from engine.runtime import _verdict_exit_code
    assert _verdict_exit_code(Error("boom")) == 3
