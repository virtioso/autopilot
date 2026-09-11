# Adapter Library: Docker SDK for Python

**Verdict: Use `docker` (docker-py) 7.1.0 with a dedicated `ThreadPoolExecutor`. Do not use `aiodocker` — it is classified inactive and has an unacceptable maintenance risk for vehicle-control orchestration.**

---

## Two SDK Variants

| | `docker` (docker-py) | `aiodocker` |
|---|---|---|
| Maintainer | Docker Inc. | Community (aio-libs) |
| Version | 7.1.0 (2024) | 0.24.0 (last release 12+ months ago) |
| asyncio fit | Blocking; requires executor bridging | Native `async for` |
| API surface | Full (containers, exec, images, volumes, swarm) | Partial; exec support minimal |
| Verdict | **Use** | **Avoid** |

`aiodocker` yields decoded strings from `log()`, not raw bytes — you lose the 8-byte Docker mux header, making stdout/stderr discrimination harder. The executor bridging overhead for docker-py is negligible at Autopilot's scale (tens of containers, not thousands).

---

## Docker Log Multiplexing Header

For containers without a TTY (`tty=False`, the default), every log chunk is prefixed with an 8-byte binary header:

```
Byte 0:    Stream type — 1=stdout, 2=stderr (0=stdin, reserved)
Bytes 1-3: Reserved (0x00 0x00 0x00)
Bytes 4-7: Payload length (big-endian uint32)
Bytes 8…:  Payload
```

Parse manually: `stream_type, length = struct.unpack('>BxxxI', header)`.

Use `demux=True` to have docker-py handle this automatically:

```python
for stdout, stderr in container.logs(stream=True, follow=True, demux=True):
    if stdout:
        handle_stdout(stdout)
    if stderr:
        handle_stderr(stderr)
```

For TTY containers (`tty=True`) stdout and stderr are merged into a raw byte stream — no header. ROS 2 stacks typically run without a TTY, so the 8-byte header is always present.

---

## Bridging Blocking Generator into asyncio

`container.logs(stream=True, follow=True)` returns a blocking Python generator. Bridge it with `run_in_executor`:

```python
import asyncio, docker
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=8)

async def stream_logs(container) -> AsyncGenerator[bytes, None]:
    loop = asyncio.get_running_loop()
    gen = container.logs(stream=True, follow=True)
    while True:
        chunk = await loop.run_in_executor(_executor, lambda: next(gen, None))
        if chunk is None:
            break
        yield chunk
```

**Cancellation:** if the asyncio task is cancelled while the thread is blocked on `next()`, the thread is not interrupted. Call `gen.close()` (which closes the underlying urllib3 socket) from a `finally` block or task cancellation handler.

---

## Container Lifecycle API

```python
client = docker.from_env()

# Launch
container = client.containers.run(
    image="ros:humble",
    name="autopilot-vehicle-stack",
    detach=True,
    environment={"ROS_DOMAIN_ID": "42"},
    network_mode="host",       # ROS 2 DDS requires host networking
    volumes={"/mnt/maps": {"bind": "/maps", "mode": "ro"}},
    remove=False,              # manage removal explicitly — never use auto_remove with streaming
)

container.reload()             # refresh .status after external changes
print(container.status)        # "running" | "exited" | "created"

container.stop(timeout=10)     # SIGTERM, waits up to 10s, then SIGKILL
result = container.wait()      # blocks; returns {"StatusCode": int, "Error": str|None}
container.remove(force=True)
```

**Never use `auto_remove=True` when streaming logs** — it creates a race between the daemon removing the container and `wait()` returning, causing a `NotFound` exception (docker-py issue #2655).

---

## Detecting Container Exit While Streaming

Run `container.wait()` concurrently with log streaming:

```python
async def run_and_stream(loop, client, image, **kwargs):
    container = await loop.run_in_executor(
        _executor, lambda: client.containers.run(image, detach=True, **kwargs)
    )

    wait_task = asyncio.create_task(
        loop.run_in_executor(_executor, container.wait)
    )

    async for chunk in stream_logs(container):
        yield chunk  # emit to BiStream / StreamContext

    result = await wait_task
    return result["StatusCode"]   # 0 = clean exit, non-zero = crash
```

The `logs(follow=True)` generator naturally exhausts after the container exits and Docker flushes remaining log buffers. Joining the wait task after the generator is exhausted is safe.

---

## Source Oracle → BiStream Mapping

The **Source oracle** for Docker launches a container and adds a named read-only BiStream (container log output) to StreamContext:

```python
async def docker_source_oracle(ctx: StreamContext, name: str, image: str, **kwargs):
    loop = asyncio.get_running_loop()
    container = await loop.run_in_executor(
        _executor, lambda: ctx.client.containers.run(image, detach=True, **kwargs)
    )
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)

    async def producer():
        async for chunk in stream_logs(container):
            await queue.put(chunk)
        await queue.put(None)  # EOF sentinel

    asyncio.create_task(producer())
    ctx.streams[name] = DockerLogBiStream(queue=queue, container=container)
    return Verdict.matched("source_ready"), ctx
```

---

## Exec into Running Container (Interactive Oracle)

Use the low-level `APIClient` with `socket=True` for interactive stdin/stdout — this gives a raw socket that is a natural BiStream (bidirectional byte channel):

```python
api = docker.APIClient()

exec_id = api.exec_create(
    container.id,
    cmd=["/bin/bash"],
    stdin=True, stdout=True, stderr=True,
    tty=False,   # False → 8-byte mux header; True → merged raw bytes
)["Id"]

sock = api.exec_start(exec_id, socket=True)

# Write to stdin
sock.sendall(b"ros2 topic echo /cmd_vel\n")

# Read from stdout (raw bytes with mux header if tty=False)
data = sock.recv(4096)
sock.close()
```

With `tty=True`, the socket is a raw merged byte stream — simpler for terminal emulation but no stdout/stderr discrimination.

---

## Error Handling

```python
from docker.errors import APIError, NotFound, DockerException
from requests.exceptions import ConnectionError, ReadTimeout

RETRYABLE = (ConnectionError, ReadTimeout)

async def safe_run(loop, client, image, **kwargs):
    for attempt in range(3):
        try:
            return await loop.run_in_executor(
                _executor,
                lambda: client.containers.run(image, detach=True, **kwargs)
            )
        except RETRYABLE:
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError("Docker daemon unreachable after 3 attempts")
```

Known issues:
- **Issue #3278**: early error response from daemon raises `ConnectionError` instead of `APIError` — catch both.
- **Issue #2561**: `logs(stream=True)` can buffer and delay delivery for containers that write rarely — this is a Docker daemon log-driver artifact (log-driver `json-file` has a 4KB flush buffer by default; use `--log-driver=journald` or set `max-size` to force smaller flushes).

---

## Key Takeaways for the Oracle Model

1. **Source oracle** = `containers.run(detach=True)` + `logs(stream=True, follow=True)` pumped via executor into an `asyncio.Queue`, surfaced as a named BiStream in StreamContext. Generator exhaustion = container exit = oracle emits verdict.
2. **8-byte mux header always present for non-TTY containers.** Use `demux=True` or strip manually.
3. **Exit detection**: run `container.wait()` concurrently with log streaming; join after generator exhausts. Non-zero exit code → `Verdict.error("crash")`.
4. **Interactive oracle** = `exec_start(socket=True)` — raw socket is a natural BiStream.
5. **Teardown**: always `container.stop(timeout=10)` then `container.remove(force=True)`. Never `auto_remove=True` when streaming.

---

*Sources: [docker-py docs](https://docker-py.readthedocs.io), [multiplexing guide](https://docker-py.readthedocs.io/en/stable/user_guides/multiplex.html), [docker-py issues #2655, #2561, #3278](https://github.com/docker/docker-py/issues), [Docker logs binary format](https://ahmet.im/blog/docker-logs-api-binary-format-explained/)*
