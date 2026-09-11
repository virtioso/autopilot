# Testing Infrastructure: pytest-asyncio and structlog

---

## pytest-asyncio

**Verdict: Use. Set `asyncio_mode = "auto"` from the start — it is designed for exactly this: a codebase that is asyncio throughout with no sync adapters.**

### Configuration

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

With `asyncio_mode = "auto"`, every `async def test_*` is collected as an asyncio test automatically. No `@pytest.mark.asyncio` boilerplate on every test.

The alternative modes:
- **`strict`**: requires `@pytest.mark.asyncio` on every test and `@pytest_asyncio.fixture` on every async fixture. Useful when asyncio and trio coexist in the same suite. No benefit for Autopilot.
- **`strict` / per-test mark**: pure noise at scale. Avoid.

One caveat with auto mode: any `async def` in a test module is claimed as an asyncio coroutine, including helper functions that are not test functions. Naming convention (`test_*`) avoids surprises.

**Recommended version:** `>=1.0` (post-deprecation-removal API). Pin `>=1.0,<2`.

### The `event_loop` Fixture — Removed in 1.0

pytest-asyncio 1.0 (released May 25, 2025) removed the `event_loop` fixture entirely. Do not use it; do not copy examples that use it.

The replacement:
- **`loop_scope`** on markers and fixtures: `@pytest_asyncio.fixture(loop_scope="module")` controls loop lifetime declaratively.
- **`asyncio.get_running_loop()`** inside async fixtures replaces any code that previously injected the loop via parameter.
- **`asyncio_event_loop_policy` fixture**: override to use uvloop or another policy.

Default `loop_scope="function"` gives full isolation — each test gets a fresh event loop. Use function scope unless a concrete reason forces module or session scope.

### Concrete Oracle Unit Test

```python
# tests/test_pattern_oracle.py
import asyncio
import pytest
import pytest_asyncio
from autopilot.engine.oracle import StreamContext, Verdict
from autopilot.adapters.pattern import PatternOracle


class MockBiStream:
    """Feed a byte buffer into an oracle as if it were a live serial stream."""

    def __init__(self, data: bytes):
        self._reader = asyncio.StreamReader()
        self._reader.feed_data(data)
        self._reader.feed_eof()

    async def read(self, n: int = 4096) -> bytes:
        return await self._reader.read(n)

    async def readuntil(self, sep: bytes) -> bytes:
        return await self._reader.readuntil(sep)

    async def write(self, data: bytes) -> None:
        pass  # no-op for read-only oracle tests


@pytest_asyncio.fixture
async def ready_stream() -> MockBiStream:
    return MockBiStream(b"BOOT\r\nREADY\r\n")


async def test_pattern_oracle_matches(ready_stream):
    ctx = StreamContext(streams={"tty0": ready_stream}, metadata={})
    oracle = PatternOracle(stream="tty0", pattern=rb"READY")

    verdict, new_ctx = await oracle(ctx, timeout=1.0)

    assert verdict == Verdict.matched("pass")


async def test_pattern_oracle_timeout():
    stream = MockBiStream(b"BOOT\r\n")  # no READY — hits EOF
    ctx = StreamContext(streams={"tty0": stream}, metadata={})
    oracle = PatternOracle(stream="tty0", pattern=rb"READY")

    verdict, _ = await asyncio.wait_for(oracle(ctx, timeout=0.1), timeout=0.5)

    assert verdict == Verdict.timeout()


async def test_streamcontext_is_not_mutated(ready_stream):
    """Oracle must return a new StreamContext, not mutate the input."""
    ctx = StreamContext(streams={"tty0": ready_stream}, metadata={"run_id": "abc"})
    oracle = PatternOracle(stream="tty0", pattern=rb"READY")

    verdict, new_ctx = await oracle(ctx, timeout=1.0)

    assert new_ctx is not ctx
    assert new_ctx.metadata["run_id"] == "abc"   # metadata preserved
```

### Timeouts in Tests

pytest-asyncio does not provide `@pytest.mark.asyncio(timeout=N)` as a first-class feature. Use `asyncio.wait_for` — this tests the actual oracle timeout path, not just the test harness:

```python
async def test_oracle_respects_timeout():
    stream = MockBiStream(b"nothing useful")
    ctx = StreamContext(streams={"tty0": stream}, metadata={})
    oracle = PatternOracle(stream="tty0", pattern=rb"READY")

    verdict, _ = await asyncio.wait_for(oracle(ctx, timeout=0.1), timeout=0.5)
    assert verdict == Verdict.timeout()
```

Python 3.11+ `asyncio.timeout(N)` context manager is cleaner syntax for the same thing.

### TaskGroup in Tests

`asyncio.TaskGroup` works natively — pytest-asyncio has no special handling needed:

```python
async def test_parallel_oracles():
    results = []
    async with asyncio.TaskGroup() as tg:
        tg.create_task(run_oracle(results, "a"))
        tg.create_task(run_oracle(results, "b"))
    assert len(results) == 2
```

`TaskGroup` propagates all exceptions as `ExceptionGroup`. Test error paths with `pytest.raises(ExceptionGroup)` or Python 3.11 `except*`.

### Gotchas

- **Dangling tasks**: if an oracle spawns a background task without awaiting it, it may outlive the test. Use `asyncio.TaskGroup` or explicit cancellation in fixture teardown. Manifests as `Task was destroyed but it is pending!`.
- **StreamReader re-use**: once `feed_eof()` is called, a `StreamReader` cannot be reset. Create a new `MockBiStream` per test.
- **Loop policy leakage**: if a test installs a global loop policy without restoring it, later tests silently use it. Scope the `asyncio_event_loop_policy` fixture to the test that needs it.

---

## structlog

**Verdict: Use. `contextvars` integration propagates structured context (request_id, oracle_type, stream_name) across `await` boundaries natively, without thread-locals or global state.**

### Why Not stdlib `logging`

stdlib `logging` requires encoding structured fields into message strings or using `LoggerAdapter`/`extra={}`, both fragile at scale. structlog builds log events as plain dicts through a configurable processor pipeline. Adding `request_id` to every log line in a task is one `bind_contextvars()` call — not 40 `extra={"request_id": x}` arguments.

structlog is roughly 2x faster than stdlib logging for structured messages: no `LogRecord` object, no threading lock on every handler dispatch, processor chain frozen on first use.

### `contextvars` Integration for asyncio

structlog uses Python's `contextvars.ContextVar` (not `threading.local`):

- **Within the same task, across `await`**: context persists. `bind_contextvars(request_id="x")` at task entry is visible to all log calls in that coroutine, even after multiple awaits.
- **Spawned child tasks** (`create_task`, `TaskGroup`): Python copies the current `Context` snapshot into child tasks at spawn time. The child inherits the parent's fields at the moment of spawning. Later `bind_contextvars` calls in the parent do not affect the child, and vice versa — correct isolated semantics.
- **`clear_contextvars()`**: call at the entry point of each top-level operation to prevent stale fields from a previous chain leaking in.

```python
from structlog.contextvars import bind_contextvars, clear_contextvars

async def invoke_oracle(oracle, ctx, timeout, *, request_id):
    bind_contextvars(
        request_id=request_id,
        oracle_type=type(oracle).__name__,
        stream_name=list(ctx.streams.keys()),
    )
    log.debug("oracle.start", timeout=timeout)
    try:
        verdict, new_ctx = await oracle(ctx, timeout=timeout)
        log.info("oracle.complete", verdict=str(verdict))
        return verdict, new_ctx
    except asyncio.TimeoutError:
        log.warning("oracle.timeout")
        raise
    except Exception:
        log.exception("oracle.error")
        raise
```

Every log call in `invoke_oracle` — and in any coroutine it awaits — automatically includes `request_id`, `oracle_type`, and `stream_name` without being passed around explicitly.

### Dev vs CI Configuration

```python
import os, logging, structlog

def configure_logging(json_logs: bool | None = None) -> None:
    if json_logs is None:
        json_logs = os.getenv("CI", "false").lower() == "true"

    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer = structlog.processors.JSONRenderer() if json_logs \
               else structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
```

Call once at application entry and once in `conftest.py`'s session-scoped fixture.

### stdlib `logging` Integration

Third-party libraries (asyncssh, pyserial, aiohttp) use stdlib `logging`. Route them through structlog's pipeline:

```python
logging.basicConfig(
    format="%(message)s",
    handlers=[structlog.stdlib.ProcessorFormatter.wrap_for_formatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(),
            foreign_pre_chain=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.processors.TimeStamper(fmt="iso"),
            ],
        )
    )],
)
```

This gives unified output: structlog events and stdlib events appear in the same format.

### High-Frequency DEBUG Logging

At DEBUG level, Autopilot logs every byte read from serial. The critical optimisation:

```python
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    cache_logger_on_first_use=True,
)
```

`make_filtering_bound_logger` generates a class with level-checking inlined. When the effective level is INFO or above, `log.debug(...)` is literally a `return None` — zero overhead. Set the effective level to INFO in production; enable DEBUG only when diagnosing a specific stream.

### Maintenance Status

- Maintained by Hynek Schlawack (also: `attrs`, `cryptography`)
- **Version**: 25.5.0 (2025), continuous releases since 2013
- API is extremely stable; `contextvars` module production-ready since Python 3.7
- **Recommended version:** `>=24.0`

---

## Summary

| | Decision |
|---|---|
| pytest mode | `asyncio_mode = "auto"`, `loop_scope = "function"` |
| event_loop fixture | Do not use — removed in pytest-asyncio 1.0 |
| Timeouts in tests | `asyncio.wait_for(oracle(...), timeout=N)` |
| TaskGroup tests | Works natively; catch `ExceptionGroup` for error paths |
| anyio | Skip — no multi-backend requirement |
| structlog context | `clear_contextvars()` at chain entry; `bind_contextvars()` per oracle |
| High-freq DEBUG | `make_filtering_bound_logger` + `cache_logger_on_first_use` |
| Dev/CI output | `ConsoleRenderer(colors=True)` vs `JSONRenderer()` branched on `CI` env var |

*Sources: [pytest-asyncio docs](https://pytest-asyncio.readthedocs.io), [pytest-asyncio 1.0 migration](https://pytest-asyncio.readthedocs.io/en/stable/reference/changelog.html), [structlog contextvars](https://www.structlog.org/en/stable/contextvars.html), [structlog performance](https://www.structlog.org/en/stable/performance.html)*
