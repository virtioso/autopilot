"""
Docker adapter: DockerLogBiStream and DockerContainerOracle.

Uses docker-py 7.1.0 with executor bridging. docker-py's log streaming API
is a blocking generator; bridging into asyncio via run_in_executor is the
standard pattern (see docs/rewrite/prior-art-docker-sdk.md).

The old system streams container logs by SSH-ing to the target and running
`docker logs -f <container>` as a subprocess source. This adapter uses the
Docker daemon API directly when the daemon is reachable — avoiding the SSH
subprocess overhead and giving typed access to container lifecycle.

Key design decisions:
  - auto_remove=False always (docker-py issue #2655: NotFound race when
    streaming logs after container exits with auto_remove=True).
  - demux=True on logs(): docker-py splits the 8-byte mux header internally.
    We recombine stdout+stderr into a single BiStream (matches old behaviour
    where stderr was merged into stdout via subprocess stderr=STDOUT).
  - Cleanup: container.stop(timeout=10) then container.remove(force=True).
    Register in ctx.cleanup_hooks BEFORE starting the log pump.
  - Backpressure: use await queue.put() (not put_nowait) in the producer.
    W19 specifies this — put_nowait silently drops bytes when the queue fills.

DockerLogBiStream is read-only (containers do not have a stdin BiStream path
in Autopilot's usage — commands go via SSHCommandOracle). write() is a no-op
that raises NotImplementedError so misuse is caught at development time.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import docker
import docker.errors
import structlog

from engine.oracle import Error, Matched, StreamContext, Verdict

log = structlog.get_logger()

# Shared executor for blocking docker-py calls.
# 8 workers is generous for Autopilot's scale (tens of containers, not thousands).
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="docker")


# ---------------------------------------------------------------------------
# DockerLogBiStream
# ---------------------------------------------------------------------------

class DockerLogBiStream:
    """
    Read-only BiStream backed by a Docker container's combined log stream.

    Bytes are produced by a background producer coroutine that bridges the
    blocking docker-py log generator via run_in_executor. The producer pushes
    chunks into an asyncio.Queue; read() pops from the queue.

    EOF is signalled by None in the queue (container exited and Docker flushed
    remaining buffers). Subsequent read() calls after EOF return b"".

    write() raises NotImplementedError — Docker containers in Autopilot's usage
    do not have a stdin path (commands go via SSHCommandOracle).
    """

    def __init__(
        self,
        queue: asyncio.Queue[bytes | None],
        container: docker.models.containers.Container,
    ) -> None:
        self._q = queue
        self._container = container
        self._eof = False

    async def read(self, n: int = 4096) -> bytes:
        if self._eof:
            return b""
        chunk = await self._q.get()
        if chunk is None:
            self._eof = True
            return b""
        return chunk

    async def write(self, data: bytes) -> None:
        raise NotImplementedError(
            "DockerLogBiStream is read-only. "
            "Use SSHCommandOracle to send commands to the container."
        )

    @property
    def container(self) -> docker.models.containers.Container:
        return self._container


# ---------------------------------------------------------------------------
# Log producer coroutine
# ---------------------------------------------------------------------------

async def _stream_container_logs(
    container: docker.models.containers.Container,
    queue: asyncio.Queue[bytes | None],
) -> None:
    """
    Bridge blocking docker-py log generator into the asyncio queue.

    demux=True: docker-py handles the 8-byte mux header internally, yielding
    (stdout_bytes_or_None, stderr_bytes_or_None) tuples. We merge both into
    the queue (matching the old system's merged stream behaviour).

    Cancellation: if this coroutine is cancelled while the executor thread is
    blocked on next(), the thread is not immediately interrupted. The generator
    is closed via gen.close() in the finally block, which closes the underlying
    urllib3 socket and causes the blocked thread to unblock within one read
    timeout (~30s by default). Acceptable for Autopilot's use case.
    """
    loop = asyncio.get_running_loop()
    gen = container.logs(stream=True, follow=True, demux=True)
    try:
        while True:
            item = await loop.run_in_executor(
                _executor, lambda: next(gen, None)
            )
            if item is None:
                break
            stdout, stderr = item
            chunk = (stdout or b"") + (stderr or b"")
            if chunk:
                await queue.put(chunk)   # backpressure: await, not put_nowait (W19)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(
            "docker.log_producer_error",
            container=container.name,
            error=repr(exc),
        )
    finally:
        try:
            gen.close()
        except Exception:
            pass
        await queue.put(None)  # EOF sentinel — always sent, even on error/cancel


# ---------------------------------------------------------------------------
# DockerContainerOracle
# ---------------------------------------------------------------------------

class DockerContainerOracle:
    """
    Source oracle: run a Docker container and register its log stream in ctx.

    The oracle:
    1. Starts the container (detached)
    2. Registers stop+remove cleanup_hook BEFORE starting the log pump (W29)
    3. Creates DockerLogBiStream and registers it in ctx.streams[stream_name]
    4. Starts the log producer as a background asyncio task
    5. Optionally waits for a readiness pattern in the log stream
    6. Returns Matched(ready_label) or the readiness oracle's verdict

    If ready_pattern is None, returns Matched("container_started") immediately
    after the container is up (without waiting for any log output).

    connect_via_daemon: if True, use docker.from_env() (connects to local Docker
    daemon). If the daemon is on a remote host accessible only via SSH, set
    connect_via_daemon=False and use SSHCommandOracle to run the container,
    then use this oracle only for log streaming (advanced usage).
    """

    def __init__(
        self,
        image: str,
        stream_name: str,
        *,
        name: str | None = None,
        command: str | list[str] | None = None,
        environment: dict[str, str] | None = None,
        network_mode: str = "host",
        volumes: dict | None = None,
        ready_pattern: bytes | str | None = None,
        ready_label: str = "container_ready",
        queue_maxsize: int = 256,
        preprocess: bool = True,
    ) -> None:
        self._image = image
        self._stream_name = stream_name
        self._name = name
        self._command = command
        self._environment = environment or {}
        self._network_mode = network_mode
        self._volumes = volumes or {}
        self._ready_pattern = ready_pattern
        self._ready_label = ready_label
        self._queue_maxsize = queue_maxsize
        self._preprocess = preprocess

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        loop = asyncio.get_running_loop()

        log.info(
            "docker.starting",
            image=self._image,
            name=self._name,
            stream=self._stream_name,
        )

        try:
            client = await loop.run_in_executor(
                _executor, docker.from_env
            )
            container = await loop.run_in_executor(
                _executor,
                lambda: client.containers.run(
                    self._image,
                    name=self._name,
                    command=self._command,
                    environment=self._environment,
                    network_mode=self._network_mode,
                    volumes=self._volumes,
                    detach=True,
                    remove=False,  # never auto_remove with streaming (issue #2655)
                ),
            )
        except docker.errors.DockerException as exc:
            log.warning("docker.start_failed", image=self._image, error=repr(exc))
            return Error(f"docker_start_failed: {exc}"), ctx

        log.info("docker.started", container=container.name, id=container.short_id)

        # Register cleanup BEFORE log pump — if readiness check times out,
        # cleanup will still stop and remove the container (W29).
        def stop_and_remove() -> None:
            try:
                container.stop(timeout=10)
            except docker.errors.NotFound:
                pass
            except Exception as exc:
                log.warning("docker.stop_error", container=container.name, error=repr(exc))
            try:
                container.remove(force=True)
            except docker.errors.NotFound:
                pass
            except Exception as exc:
                log.warning("docker.remove_error", container=container.name, error=repr(exc))

        ctx.register_cleanup(self._stream_name, stop_and_remove)

        # Create BiStream and start log producer
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._queue_maxsize)
        bio: object = DockerLogBiStream(queue, container)
        if self._preprocess:
            from engine.primitives import FilterBiStream
            bio = FilterBiStream(bio)
        ctx.streams[self._stream_name] = bio

        # Background task — W22: do not capture ctx in the task, only the queue
        producer_task = asyncio.create_task(
            _stream_container_logs(container, queue),
            name=f"docker_log_producer_{self._stream_name}",
        )
        ctx.register_cleanup(self._stream_name, producer_task.cancel)

        if self._ready_pattern is None:
            return Matched("container_started"), ctx

        # Wait for readiness pattern in the log stream
        from engine.primitives import PatternOracle
        readiness = PatternOracle(
            self._stream_name,
            self._ready_pattern,
            label=self._ready_label,
        )
        verdict, ctx = await readiness(ctx, timeout)
        if not isinstance(verdict, Matched):
            log.warning(
                "docker.readiness_failed",
                container=container.name,
                verdict=type(verdict).__name__,
            )
        return verdict, ctx
