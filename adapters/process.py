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

    THE PIPE IS DRAINED CONTINUOUSLY, not only when an oracle reads. Before this,
    a spawned process's stdout was read only while a readiness pattern was being
    matched; after that nothing pulled, the 64 KiB pipe filled, and a chatty
    process then blocked on its next write -- alive, silent, answering nothing
    (the ExMeBus server, run five) -- or died on the write error (runs six and
    nine, exit 1 with no traceback, because the traceback goes to the same full
    pipe). Ten runs' `streams/<name>.raw` held only each process's banner, so
    the evidence of the cause was missing for the same reason as the cause. A
    pump task now reads the pipe as fast as the process writes, queues chunks for
    read(), and the tee sees everything the process said.

    When the process exits, read() returns b"" (EOF) after the queued data.
    """

    def __init__(self, proc: asyncio.subprocess.Process, tee=None) -> None:
        self._proc = proc
        self._tee = tee                       # a binary file: written by the pump, not by read()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._eof = False
        self._pump = asyncio.get_running_loop().create_task(self._drain())

    async def _drain(self) -> None:
        assert self._proc.stdout is not None
        try:
            while True:
                chunk = await self._proc.stdout.read(65536)
                if not chunk:
                    break
                if self._tee is not None:
                    try:
                        self._tee.write(chunk); self._tee.flush()
                    except (OSError, ValueError):
                        pass                  # a closed tee must not stop the drain
                await self._queue.put(chunk)
        finally:
            self._eof = True
            await self._queue.put(b"")

    async def read(self, n: int = 4096) -> bytes:
        chunk = await self._queue.get()
        if len(chunk) > n:
            rest = chunk[n:]
            # put the remainder back at the FRONT: order is the stream's only contract
            items = [rest]
            while not self._queue.empty():
                items.append(self._queue.get_nowait())
            for it in items:
                self._queue.put_nowait(it)
            return chunk[:n]
        return chunk

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
        ready_pattern: bytes | str | None,
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

        # A spawned process that dies on its own is otherwise invisible: cleanup
        # sees returncode already set and returns without a word, so the run
        # record shows a process that was started and never stopped. Log the
        # exit when it happens, with the wait status -- negative is the signal
        # that killed it, which is the one fact a silent death leaves behind.
        async def _watch_exit(name: str = self._stream_name) -> None:
            rc = await proc.wait()
            if rc < 0:
                log.warning("process.exited", name=name, pid=proc.pid, returncode=rc,
                            signal=-rc)
            else:
                log.info("process.exited", name=name, pid=proc.pid, returncode=rc)
        asyncio.get_running_loop().create_task(_watch_exit())

        # The tee is the pump's, so streams/<name>.raw holds everything the process
        # wrote, read or not; registered directly rather than through add_stream,
        # whose tee only sees what an oracle reads.
        tee = None
        result_dir = ctx.metadata.get("result_dir")
        if result_dir:
            streams_dir = Path(str(result_dir)) / "streams"
            streams_dir.mkdir(exist_ok=True)
            tee = open(streams_dir / f"{self._stream_name}.raw", "wb")
            ctx.register_cleanup(self._stream_name + ".raw", tee.close)
        stream = ProcessBiStream(proc, tee=tee)

        # Register kill hook BEFORE readiness check (W29).
        # If readiness times out, ctx.cleanup() will kill the process.
        ctx.register_cleanup(
            self._stream_name,
            make_process_cleanup(proc, self._stream_name),
        )

        ctx.streams[self._stream_name] = stream
        if self._preprocess:
            from engine.primitives import FilterBiStream
            ctx.streams[self._stream_name] = FilterBiStream(ctx.streams[self._stream_name])

        # ready_pattern=None: this process prints nothing a chain can wait on
        # (a wine plant, say) and its readiness is asserted by the NEXT step on
        # another stream. Returns Matched("spawned"), which is a weaker label on
        # purpose -- a chain that stops here has not shown the process is ready.
        if self._ready_pattern is None:
            log.info("spawn.spawned_no_readiness", name=self._stream_name, pid=proc.pid)
            return Matched("spawned"), ctx

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
        failure_stops: bool = False,
        capture_name: str | None = None,
        env_extra: dict[str, str] | None = None,
        cwd: Path | str | None = None,
        markers: bool = False,
    ) -> None:
        self._cmd = cmd
        self._success_codes = success_exit_codes
        self._success_label = success_label
        self._failure_label = failure_label
        self._failure_stops = failure_stops
        self._capture_name = capture_name
        self._env_extra = env_extra or {}
        self._cwd = str(cwd) if cwd else None
        self._markers = markers

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
        if self._markers:
            self._emit_markers(ctx, stdout)

        exit_code = proc.returncode
        log.debug("run_process.done", cmd=self._cmd, exit_code=exit_code)

        if exit_code in self._success_codes:
            return Matched(self._success_label), ctx
        if self._failure_label is not None:
            if self._failure_stops:
                return Error(self._failure_label), ctx      # ends the enclosing sequence, reason = the label
            return Matched(self._failure_label), ctx
        return Error(f"exit={exit_code}"), ctx

    # A tool that took a frame or saw an incident says so on stdout, one JSON
    # object per line behind a fixed word, and the run's event log carries it
    # as the recorder's own event -- so `incidents` can cut the window around
    # it later and nothing in the tool knows the recorder exists:
    #     FRAME {"path": ..., "sha256": ..., "trigger": ..., "t_host": ...}
    #     INCIDENT {"t_host": ..., "trigger": ..., "frame": ..., "note": ...}
    # A line that names the word but does not parse is an error event in the
    # log, never dropped: a marker lost is a window nobody can cut.
    def _emit_markers(self, ctx: StreamContext, stdout: bytes) -> None:
        import json
        from engine.recorder import FrameSaved, Incident
        recorder = ctx.metadata.get("recorder")
        if recorder is None:
            return
        kinds = {b"FRAME": FrameSaved, b"INCIDENT": Incident}
        for raw in stdout.splitlines():
            word, _, rest = raw.partition(b" ")
            cls = kinds.get(word)
            if cls is None:
                continue
            try:
                recorder.emit(cls(**json.loads(rest)))
            except (ValueError, TypeError) as exc:
                log.error("run_process.marker_unreadable", cmd=self._cmd, line=raw[:200], error=repr(exc))
