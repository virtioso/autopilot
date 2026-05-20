# Autopilot Rewrite: Context Recovery

> Read this at the start of any session to get up to speed in under 5 minutes.  
> Check `tracker.md` for current step status. The full plan is in `index.md`.

---

## What We Are Building

A rewrite of Autopilot's chain execution engine using an **oracle combinator model**:

```
Oracle: (StreamContext, float) → (Verdict, StreamContext)
```

Every step in a chain — pattern matching, command execution, stream discovery, Robot Framework — is an oracle. Combinators (`Sequence`, `Choice`, `Race`, `Parallel`, `Timeout`, `Repeat`) compose them. The engine routes on verdict labels; patterns and platform specifics live in adapter oracle implementations.

**Why:** `chain_runtime.py` is a 3,561-line monolith. The rewrite separates engine (combinators), adapters (transports), and model (chain JSON schema). New platforms add adapters only.

---

## Where Things Live

| Location | Branch | Purpose |
|---|---|---|
| `~/autopilot/` | `backup` | Old code — runnable reference, untouched |
| `~/autopilot-rewrite/` | `rewrite` (orphan) | New code — active development |
| `~/autopilot/docs/rewrite/index.md` | `backup` | Full architecture plan (778 lines, W1–W29) |
| `~/autopilot/docs/rewrite/tracker.md` | `backup` | **Current step and status** |
| `~/autopilot/docs/rewrite/context-recovery.md` | `backup` | This file |

The `rewrite` branch is an **orphan** — no shared history with `backup`. After the rewrite is stable, `git rebase -i --root` on the `rewrite` branch cleans history; `git format-patch --root | git am` replays it onto a fresh repo.

---

## Architecture: Key Decisions (Locked In)

These were resolved during planning (W1–W29). Do not redesign them.

**StreamContext mutation model:**
- Mutable in-place within one asyncio task
- `ctx.fork()` at Parallel/Race: shallow copy of stream dict + **empty** cleanup_hooks list
- `ctx.merge(child, include_streams)` at join: copies named streams + migrates their cleanup_hooks; runs cleanup for excluded streams

**cleanup_hooks:** `list[tuple[str | None, Callable]]` — keyed by stream name or None for global hooks. Register IMMEDIATELY after resource acquisition, before any condition check.

**Verdict type:**
```python
@dataclass(frozen=True)
class Matched:
    label: str
    def is_matched(self, label=None): return label is None or self.label == label

@dataclass(frozen=True)
class TimeoutVerdict: pass   # NOT "Timeout" — that name is taken by the Timeout combinator

@dataclass(frozen=True)
class Error:
    reason: str
```
Use Python 3.10+ structural pattern matching: `match verdict: case Matched(label=l): ...`

**Combinators:**
- `Sequence(A, B)` — short-circuits on non-matched verdict; deadline via outer `Timeout` (NOT inside Sequence)
- `Choice(oracles, max_buf=1MiB)` — same stream, multiple patterns, bytearray buffer
- `Race(branches)` — disjoint streams; TaskGroup; cancel losers + cleanup
- `Parallel(branches)` — disjoint streams; TaskGroup; `gather(return_exceptions=True)`; merge on join
- `Timeout(oracle, t)` — `asyncio.wait_for`; returns `(TimeoutVerdict, ctx)` on expiry
- `Repeat.monitor(oracle, max_iter, backoff)` — continues on matched, stops on timeout/error
- `Repeat.poll(oracle, success_label, max_iter, backoff)` — stops on matched(success_label), retries on timeout

**Disjoint-stream constraint:** Parallel AND Race branches may NOT read the same named stream. Asserted at runtime.

**asyncio.TaskGroup** for Parallel and Race (Python 3.12+ required — fixes asyncio.wait_for cancellation bug bpo-46707).

**OracleFactory:** Two-phase load. Pydantic deserializes JSON → `OracleDef`. `OracleFactory.hydrate()` recursively builds live Oracle instances. Adapters register via `OracleFactory.register("uart_pattern", UARTPatternOracle)` at import time. Discriminated union on `oracle: Literal[...]` field.

**Daemon:** Single asyncio event loop; `asyncio.Queue` serializes chain execution (one at a time); MCP server as co-task in same loop; Unix socket IPC with line-delimited JSON events; in-memory run registry (last 100 runs).

**ChainRecorder:** Fixed event vocabulary — `OracleStarted`, `OracleVerdict`, `StreamBytesRead`, `CleanupHookRan`. Writes `results/<run_id>/events.jsonl`. Thread via StreamContext metadata (not global). `NullRecorder`/`ListRecorder` for tests.

**Libraries:** pyserial-asyncio-fast, asyncssh, docker-py 7.1.0, zenoh-python (Linux target only), structlog, pytest-asyncio ≥1.0 with `asyncio_mode = "auto"`.

**CancelledError rule:** Never `except BaseException` or bare `except:` outside top-level shutdown handler. Always re-raise CancelledError in cleanup paths.

---

## Step 2 Findings (setup_demo / check_verdict)

- **`setup_demo`**: Decomposes into `VCMuxSourceOracle` (source creation) + display side-effect (`pipe-pane -I` tee). No special oracle class needed. The `tail -F` mechanism in the old code is replaced.
- **`check_verdict.artifact_grep`**: SSH + poll `grep -q`. Maps to `Poll(SSHCommandOracle(...))`.
- **`check_verdict.tmux_capture`**: Polls `tmux capture-pane` — **deprecated in rewrite**, replace with Pattern oracle on BiStream.
- **`set_test_verdict`**: Trivial. Maps to `VerdictOracle`: immediate `Matched(label)` + recorder side-effect.
- **VerdictOracle stays simple** — the complexity in check_verdict was a workaround for absent raw stream access.

---

## Module Structure

```
autopilot-rewrite/
├── engine/
│   ├── oracle.py          # Verdict, BiStream Protocol, StreamContext, Oracle Protocol
│   ├── combinators.py     # Sequence, Choice, Race, Timeout, Parallel, Repeat
│   ├── runtime.py         # Chain executor: load → hydrate → execute → record
│   └── recorder.py        # ChainRecorder, NullRecorder, ListRecorder
├── adapters/
│   ├── uart.py            # pyserial-asyncio-fast
│   ├── ssh.py             # asyncssh
│   ├── process.py         # subprocess / spawn_process
│   ├── docker.py          # docker-py 7.1.0; DockerLogBiStream + DockerContainerOracle
│   ├── vcmux.py           # in-process VCMux 0xfe parser; VCMuxParser, NvidiaTCUFilter, VCMuxSourceOracle
│   ├── robot.py           # RobotFrameworkOracle + parse_rf_output (output.xml)
│   └── interactive.py     # ConsoleBridge + _session_registry + InteractiveOracle
├── model/
│   ├── chain.py           # Pydantic OracleDef discriminated union + OracleFactory
│   └── config.py          # Layered config (defaults → platform → env → request)
├── platforms/
│   ├── orin-agx.yaml
│   └── qemu-generic.yaml
├── daemon.py
├── client.py
└── mcp_server.py
```

---

## What To Do Next

Check `tracker.md` for current step. Then:

1. Steps 3–7 are **in progress**: all engine, adapter, schema, and runtime code is done. 100 tests pass.
   - `engine/`: oracle.py, combinators.py, recorder.py, primitives.py, **runtime.py** (new)
   - `adapters/`: uart.py, ssh.py, process.py, docker.py, vcmux.py, robot.py, interactive.py
   - `model/chain.py`: Pydantic OracleDef (19 types) + OracleFactory
   - `chains/`: post_test_fallback_noop.json, wait_for_elfloader.json, sel4test.json (simplified)
2. **Hardware validation pending** (4 tests skip-marked in `tests/test_adapters.py`): UART loopback, SSH to target.
3. **Next: Step 0** — pre-rewrite baselines on real hardware. Then finish migrating hardware-dependent chains (boot_stock_linux, sel4test full flow, vm chains).

**Key new APIs:**
```python
# Load and run a chain file
from engine.runtime import run_chain
verdict, ctx = await run_chain(Path("chains/wait_for_elfloader.json"), ctx, timeout=300)

# Parse + hydrate manually
from model.chain import OracleFactory
oracle_def = OracleFactory.parse({"oracle": "choice", "stream": "tty0", "options": [...]})
oracle = OracleFactory.hydrate(oracle_def)
verdict, ctx = await oracle(ctx, timeout)
```

**Chain JSON format:**
```json
{
  "oracle": "sequence",
  "steps": [
    { "oracle": "uart_source", "stream": "tty0", "device": "$AUTOPILOT_TTY0" },
    { "oracle": "timeout", "seconds": 300, "step": {
        "oracle": "choice", "stream": "tty0",
        "options": [
          { "pattern": "ELF-loader started on CPU", "label": "pass" },
          { "pattern": "not recognized", "label": "fail" }
        ]
    }}
  ]
}
```

**Key test to run on hardware (step 5/6 completion):**
```bash
cd ~/autopilot-rewrite
# Remove @pytest.mark.skip from test_uart_open_and_read in tests/test_adapters.py
# Set TARGET_IP env var, remove skip from SSH tests
TARGET_IP=<board-ip> python3 -m pytest tests/test_adapters.py -v -k "uart or ssh"
```

**Key test to run on hardware (step 5 completion):**
```bash
cd ~/autopilot-rewrite
# Remove @pytest.mark.skip from test_uart_open_and_read in tests/test_adapters.py
# Set TARGET_IP env var, remove skip from SSH tests
TARGET_IP=<board-ip> python3 -m pytest tests/test_adapters.py -v -k "uart or ssh"
```

When updating: edit `tracker.md` current step + status table. Update this file if key decisions change or new findings emerge from reading old code.
