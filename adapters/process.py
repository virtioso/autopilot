"""
Process adapter: ProcessBiStream and SpawnProcessOracle.

ProcessBiStream wraps asyncio.Process stdin/stdout as a BiStream.

SpawnProcessOracle spawns a long-lived background process (simulator daemon,
service under test), creates a ProcessBiStream from its stdout, and applies
a PatternOracle to detect a readiness signal before returning. This is the
`map_command_source` extended with a readiness gate.

Critical ordering rule (W29): the kill cleanup_hook is registered IMMEDIATELY
after the process is spawned, BEFORE the readiness check. If the readiness
pattern never appears and Timeout fires, the cleanup_hook is already in
ctx.cleanup_hooks and ctx.cleanup() will kill the process. Without this
ordering, a timed-out process would run indefinitely, holding port bindings
and interfering with subsequent chain runs.

Process lifecycle:
  - spawn with start_new_session=True to get a process group leader
  - on cleanup: SIGTERM to the process group (kills children too)
  - if still running after 3s grace: SIGKILL

The old system used os.killpg(proc.pid, signal.SIGTERM) for own_process_group.
asyncio.create_subprocess_exec with start_new_session=True achieves the same.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import structlog

from engine.oracle import Error, Matched, StreamContext, Verdict
from engine.primitives import PatternOracle

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# ProcessBiStream
# ---------------------------------------------------------------------------

class ProcessBiStream:
    """
    BiStream backed by an asyncio.Process stdin/stdout.

    stdout and stderr are merged into stdout (stderr=STDOUT) so that error
    messages from the process are visible to pattern-matching oracles.
    This matches the old system's behaviour (SourceBinding merges stderr).

    When the process exits, read() returns b"" (EOF).
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    async def read(self, n: int = 4096) -> bytes:
        assert self._proc.stdout is not None
        return await self._proc.stdout.read(n)

    async def write(self, data: bytes) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    @property
    def pid(self) -> int:
        return self._proc.pid

    async def wait(self) -> int:
        """Wait for the process to exit and return its exit code."""
        return await self._proc.wait()

    async def exit_code(self) -> int | None:
        """Non-blocking: return exit code if process has exited, else None."""
        return self._proc.returncode


# ---------------------------------------------------------------------------
# Process cleanup helper
# ---------------------------------------------------------------------------

def make_process_cleanup(proc: asyncio.subprocess.Process, name: str) -> callable:
    """
    Return a synchronous cleanup function that terminates the process.

    Sends SIGTERM to the process group (kills any child processes too).
    If the process doesn't exit within 3 seconds, sends SIGKILL.

    The cleanup function is synchronous because ctx.cleanup() is synchronous.
    Waiting for the process to exit is done with a blocking wait() with
    timeout — acceptable in cleanup because this runs after the event loop
    has finished the oracle task.

    SIGKILL caveat: documented in the plan (W10). If the process ignores
    SIGTERM and the 3s timeout elapses, SIGKILL is sent. If Autopilot itself
    receives SIGKILL, this cleanup does not run — manual recovery needed.
    """
    def cleanup() -> None:
        if proc.returncode is not None:
            return  # already exited
        try:
            # Kill the whole process group so child processes are also killed.
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            log.debug("process.sigterm", name=name, pid=proc.pid)
        except ProcessLookupError:
            return  # already gone
        except Exception as exc:
            log.warning("process.sigterm_failed", name=name, error=repr(exc))

        # Synchronous wait with timeout — runs outside the event loop during cleanup.
        import time
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                log.debug("process.exited_cleanly", name=name)
                return
            time.sleep(0.1)

        # Still running — escalate to SIGKILL.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            log.warning("process.sigkill", name=name, pid=proc.pid)
        except Exception:
            pass

    return cleanup


# ---------------------------------------------------------------------------
# SpawnProcessOracle
# ---------------------------------------------------------------------------

class SpawnProcessOracle:
    """
    Spawn a long-lived background process and detect its readiness.

    Use for: simulator daemons (virtual-exertus, virtual-mid), services under
    test (isengard-can-bridge), or any process that needs to be running before
    subsequent oracles execute.

    The oracle:
    1. Spawns the process with asyncio.create_subprocess_exec
    2. Creates a ProcessBiStream from stdout+stderr
    3. Registers kill cleanup_hook in ctx (BEFORE readiness check — W29)
    4. Registers the stream in ctx.streams under stream_name
    5. Applies PatternOracle to detect ready_pattern on stream_name
    6. Returns Matched(ready_label) on success, or the pattern oracle's
       verdict on failure (TimeoutVerdict if no match, Error on EOF/overflow)

    env_extra: dict of environment variables merged with os.environ.
    cwd: working directory for the spawned process.
    """

    def __init__(
        self,
        cmd: list[str],
        stream_name: str,
        ready_pattern: bytes | str,
        *,
        ready_label: str = "ready",
        env_extra: dict[str, str] | None = None,
        cwd: Path | str | None = None,
        preprocess: bool = True,
    ) -> None:
        self._cmd = cmd
        self._stream_name = stream_name
        self._ready_pattern = ready_pattern
        self._ready_label = ready_label
        self._env_extra = env_extra or {}
        self._cwd = str(cwd) if cwd else None
        self._preprocess = preprocess

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        import os as _os
        env = {**_os.environ, **self._env_extra}

        log.info(
            "spawn.starting",
            name=self._stream_name,
            cmd=self._cmd,
            cwd=self._cwd,
        )
        proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE,
            env=env,
            cwd=self._cwd,
            # start_new_session=True makes the process a session leader,
            # so os.killpg can signal the entire process tree on cleanup.
            start_new_session=True,
        )
        log.info("spawn.started", name=self._stream_name, pid=proc.pid)

        stream = ProcessBiStream(proc)

        # Register kill hook BEFORE readiness check (W29).
        # If readiness times out, ctx.cleanup() will kill the process.
        ctx.register_cleanup(
            self._stream_name,
            make_process_cleanup(proc, self._stream_name),
        )

        if self._preprocess:
            from engine.primitives import FilterBiStream
            stream = FilterBiStream(stream)
        ctx.streams[self._stream_name] = stream

        # Wait for readiness signal on the process stdout.
        readiness = PatternOracle(
            self._stream_name,
            self._ready_pattern,
            label=self._ready_label,
        )
        verdict, ctx = await readiness(ctx, timeout)
        if not isinstance(verdict, Matched):
            log.warning(
                "spawn.readiness_failed",
                name=self._stream_name,
                verdict=type(verdict).__name__,
            )
            return verdict, ctx

        log.info(
            "spawn.ready",
            name=self._stream_name,
            pid=proc.pid,
            label=self._ready_label,
        )
        return Matched(self._ready_label), ctx


# ---------------------------------------------------------------------------
# RunProcessOracle
# ---------------------------------------------------------------------------

class RunProcessOracle:
    """
    Run a subprocess to completion and return a verdict based on exit code.

    Use for: analyze_logs, run_robot (before the RF-specific oracle is built),
    or any one-shot subprocess that does not produce a BiStream for subsequent
    oracles.

    stdout and stderr are captured and stored in ctx.metadata[stream_name]
    as bytes, so subsequent oracles can inspect the output if needed.
    """

    def __init__(
        self,
        cmd: list[str],
        *,
        success_exit_codes: frozenset[int] = frozenset({0}),
        success_label: str = "ok",
        failure_label: str | None = None,
        capture_name: str | None = None,
        env_extra: dict[str, str] | None = None,
        cwd: Path | str | None = None,
    ) -> None:
        self._cmd = cmd
        self._success_codes = success_exit_codes
        self._success_label = success_label
        self._failure_label = failure_label
        self._capture_name = capture_name
        self._env_extra = env_extra or {}
        self._cwd = str(cwd) if cwd else None

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        import os as _os
        env = {**_os.environ, **self._env_extra}

        log.debug("run_process.start", cmd=self._cmd)
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=self._cwd,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return Error("process_timeout"), ctx
        except OSError as exc:
            return Error(f"spawn_failed: {exc}"), ctx

        if self._capture_name:
            ctx.metadata[self._capture_name] = stdout

        exit_code = proc.returncode
        log.debug("run_process.done", cmd=self._cmd, exit_code=exit_code)

        if exit_code in self._success_codes:
            return Matched(self._success_label), ctx
        if self._failure_label is not None:
            return Matched(self._failure_label), ctx
        return Error(f"exit={exit_code}"), ctx
