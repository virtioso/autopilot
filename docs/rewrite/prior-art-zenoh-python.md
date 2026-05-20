# Adapter Library: zenoh-python

**Verdict: Use. Reconnection is automatic, discovery via liveliness tokens is clean, and the asyncio bridge pattern (callback → `call_soon_threadsafe` → queue) is well-established.**

---

## Role in Autopilot

zenoh-python backs `adapters/vcmux.py` — the VCMux Source oracle that discovers VM guest consoles dynamically on the Linux target path and adds them as named BiStreams to StreamContext. VMs come up asynchronously after board boot; the oracle subscribes to a wildcard key expression and receives notifications as each VM console key goes live.

---

## Pub/Sub Model

Zenoh is a unified data-in-motion protocol built on a **key/expression tree**. Every resource lives at a path like `vm/console0/console/stdout`. The three primitives:

- **Publisher**: attaches to a key, sends `put()` samples
- **Subscriber**: receives samples from any key matching its key expression (exact or wildcard)
- **Queryable / Get**: request-reply on the same key tree

Zenoh is not broker-mandatory. Autopilot operates in **client mode**, connecting to a `zenohd` router running on the target side (the Zenoh bridge to `virtioso-muxd`).

Zenoh is **message-boundary-preserving**: each `publisher.put(payload)` results in exactly one `Sample` at every subscriber. Bytes from the virtioso-muxd arrive chunked at pty-read granularity (~line or read-buffer size). Concatenate chunks in order to reconstruct the byte stream — no special framing needed beyond that.

---

## Session Creation

```python
import zenoh

# Default config — discovers router via UDP multicast
with zenoh.open(zenoh.Config()) as session:
    ...

# Explicit TCP connect to zenohd on the board
config = zenoh.Config()
config.insert_json5("mode", '"client"')
config.insert_json5("connect/endpoints", '["tcp/orin-agx-board:7447"]')

with zenoh.open(config) as session:
    ...
```

Always manage the session as a context manager or call `.close()` explicitly. On Python exit, finalizers may race with the library thread and hang the process if the session is still open.

---

## Asyncio Integration — No Native Async API

**zenoh-python has no native `async/await` API.** The library is a PyO3 Rust extension; Zenoh's Tokio runtime is opaque to Python. Bridge via queue:

```python
import asyncio, zenoh

async def console_source(session: zenoh.Session, key_expr: str):
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1024)

    def on_sample(sample: zenoh.Sample):
        # Fires on Zenoh internal thread — never block, never call Zenoh APIs here
        loop.call_soon_threadsafe(queue.put_nowait, bytes(sample.payload.to_bytes()))

    sub = session.declare_subscriber(key_expr, on_sample)
    try:
        while True:
            chunk = await queue.get()
            yield chunk  # async generator → feed BiStream write side
    finally:
        sub.undeclare()
```

**Critical constraint:** Do not call any Zenoh API method from inside a subscriber callback — it may deadlock. The callback fires on a Zenoh internal thread; use `loop.call_soon_threadsafe` to hand off to asyncio and return immediately.

---

## VM Console Discovery — Liveliness Tokens

**Wildcard key expression:** `*` matches a single path chunk; `**` matches multiple chunks.

```
vm/*/console/stdout   →   matches vm/console0/console/stdout, vm/vm1/console/stdout, …
```

**Liveliness tokens** are the right primitive for dynamic console discovery — they signal when a key goes live or disappears, distinct from data samples:

```python
def on_liveness(sample: zenoh.Sample):
    key = str(sample.key_expr)
    if sample.kind == zenoh.SampleKind.PUT:
        # New VM console appeared — create BiStream
        asyncio.run_coroutine_threadsafe(add_console_bistream(key), event_loop)
    elif sample.kind == zenoh.SampleKind.DELETE:
        # VM console gone — remove BiStream from StreamContext
        asyncio.run_coroutine_threadsafe(remove_console_bistream(key), event_loop)

liveness_sub = session.liveliness().declare_subscriber(
    "vm/*/console/stdout",
    on_liveness,
    history=True,   # receive tokens already present at subscription time
)
```

`history=True` catches VMs that were already up before Autopilot subscribed — essential for correct startup sequencing.

An alternative: a plain wildcard data subscriber on `vm/*/console/stdout` will receive data samples from any matching key as it starts publishing, even if not yet present at subscription time. Simpler, but gives no appeared/disappeared signal — only data. Use liveliness for the Source oracle that needs to know exactly when to create a new BiStream entry in StreamContext.

---

## Writing to VM Console Stdin

```python
# Pre-declare publisher — more efficient for repeated writes
pub = session.declare_publisher(
    "vm/console0/console/stdin",
    reliability=zenoh.Reliability.RELIABLE(),
    congestion_control=zenoh.CongestionControl.BLOCK(),  # never drop stdin
)

pub.put(b"uname -a\n")
pub.put(b"ls /\n")

pub.undeclare()  # on teardown

# One-shot without pre-declared publisher
session.put(
    "vm/console0/console/stdin",
    b"reboot\n",
    congestion_control=zenoh.CongestionControl.BLOCK(),
)
```

**Always use `CongestionControl.BLOCK()` for stdin** — silently dropped commands corrupt the interactive session.

---

## Reliability Model

| Setting | Publisher | Subscriber |
|---|---|---|
| `Reliability.RELIABLE()` | Informs network | Requests delivery guarantee |
| `CongestionControl.BLOCK()` | Blocks if queues full | — |
| `CongestionControl.DROP()` | Drops if queues full | — |

For console streams:
- **stdout subscribe**: `Reliability.RELIABLE()` — dropped bytes corrupt the view
- **stdin publish**: `CongestionControl.BLOCK()` + `Reliability.RELIABLE()` — commands must not be dropped

---

## Reconnection — Automatic, No Application Code Needed

When the connection to `zenohd` is lost, the Zenoh runtime detects the broken link, reconnects on a backoff schedule, and re-announces all declared publishers and subscribers automatically. Subscriber callbacks simply stop firing during the outage and resume after reconnect. No application-level reconnection logic is needed.

Liveliness subscriptions also recover correctly: on reconnect, you receive a fresh `PUT` liveliness event for each active token, allowing you to reconcile the set of live consoles and re-create any BiStreams that were in-flight when the connection dropped.

---

## Full VCMux Source Oracle Pattern

```python
import asyncio, zenoh

async def vcmux_source_oracle(
    ctx: StreamContext,
    session: zenoh.Session,
    pattern: str = "vm/*/console/stdout",
) -> tuple[Verdict, StreamContext]:
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()

    def on_liveness(sample: zenoh.Sample):
        key = str(sample.key_expr)
        if sample.kind != zenoh.SampleKind.PUT:
            return
        # Create per-console queue and subscriber
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1024)
        sub = session.declare_subscriber(
            key,
            lambda s, _q=q: loop.call_soon_threadsafe(
                _q.put_nowait, bytes(s.payload.to_bytes())
            ),
        )
        stdin_key = key.replace("stdout", "stdin")
        pub = session.declare_publisher(
            stdin_key,
            reliability=zenoh.Reliability.RELIABLE(),
            congestion_control=zenoh.CongestionControl.BLOCK(),
        )
        # Register BiStream in StreamContext under the console name
        name = key.split("/")[1]   # e.g. "console0"
        ctx.streams[name] = ZenohBiStream(queue=q, publisher=pub, subscriber=sub)
        loop.call_soon_threadsafe(ready.set)

    liveness_sub = session.liveliness().declare_subscriber(pattern, on_liveness, history=True)
    ctx.cleanup_hooks.append(liveness_sub.undeclare)

    await asyncio.wait_for(ready.wait(), timeout=30.0)
    return Verdict.matched("consoles_ready"), ctx
```

---

## Throughput at Console Scale

At 115200 bps (~11.5 KB/s), a serial console is roughly 0.0001% of Zenoh's measured throughput ceiling (67 Gbps on loopback). There are no throughput concerns at this data rate. The main practical consideration: if the virtioso-muxd calls `put()` per byte or per character, sample overhead dominates. Better to publish per pty read-buffer (~64+ bytes). At 115200 bps this naturally produces ~12 bytes/ms per read — negligible.

---

## Maintenance Status

- **Version**: 1.9.0 (released April 10, 2026) — active Eclipse Zenoh team
- **API stability**: 1.0 marked the stable baseline (late 2024); 1.x series has followed a consistent API
- **Python support**: 3.8–3.12 binary wheels (aarch64 Linux included — Orin AGX native)
- **Production use**: ROS 2 `rmw_zenoh` middleware, IIoT deployments
- **Implementation**: PyO3 Rust extension — no pure-Python performance concerns

*Sources: [zenoh-python docs](https://zenoh-python.readthedocs.io), [GitHub](https://github.com/eclipse-zenoh/zenoh-python), [Zenoh reliability blog](https://zenoh.io/blog/2021-06-14-zenoh-reliability/)*
