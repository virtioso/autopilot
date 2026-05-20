# Autopilot Rewrite: Oracle Combinator Architecture

## What Autopilot Actually Is

Autopilot is a **chain-based orchestration runtime** for heterogeneous embedded and virtualization testing. It started as an Orin AGX kernel test harness but now orchestrates:

- Physical hardware (Orin AGX with UEFI reset-line handshakes, USB relay power control)
- seL4 hypervisor VMs (vcmuxer/VCMux multiplexing, dynamic VM console discovery)
- QEMU (ARM64/x86-64, router-based console discovery)
- Docker containers (Linux-based target system running user services)
- Pub/sub middleware (Zenoh bridge, virtioso-muxd)
- AI-driven interactive debugging (MCP server exposing console sessions to Claude)

The underlying ideas — structured workflow composition, parallel fork/join, async task coordination, signal-based synchronization — are right. The problems are that the flat JSON graph encoding is the wrong shape for expressing oracle composition, and that a single file has accreted all of the implementation.

---

## The Central Architectural Problem

### Autopilot Is Not Quite an Oracle — But It Keeps Becoming One

The system has an unresolved identity: it was designed as a **transport and orchestration layer** but pattern matching on streams has gradually made it a de-facto oracle — it decides whether tests passed by watching for regex patterns.

With Robot Framework in the picture, RF *is* the oracle. Autopilot's role should be to carry RF to the hardware and carry results back — not to re-interpret RF's output via regex.

Yet removing all pattern matching is impossible: Autopilot *must* match patterns to navigate hardware state (boot menu acquired, EFI prompt visible, login succeeded, VM console alive). Pattern matching for navigation is inherent to hardware orchestration.

The current chain model conflates two distinct concerns under the same `wait_pattern` / `case` / `pass` / `fail` primitives:

1. **Navigation patterns** — "are we in the right state to proceed?"
2. **Verdict patterns** — "did the test pass?"

---

## The Oracle Model: Central Proposal for a Rewrite

### Oracles as First-Class Types

Every pattern match in Autopilot — from "did we see the UEFI prompt?" to "did Robot Framework exit successfully?" — is an oracle. The oracle type is:

```
Oracle: (StreamContext, Timeout) → (Verdict, StreamContext)
```

- **StreamContext**: a named registry of `BiStream` instances, plus runtime metadata (chain aliases, lifecycle config). Oracles look up streams by name (`tty0`, `vm0_guest_console_sink`) from the registry rather than receiving a single stream. See the StreamContext and BiStream sections below.
- **Timeout**: a deadline for the oracle to produce a verdict
- **Verdict**: a structured result — `matched(label)`, `timeout`, `error(reason)`
- **StreamContext** (output): the updated registry after the oracle ran — read cursors advanced, new streams added, or existing streams replaced

This model unifies two cases that the earlier `(BiStream, Timeout) → (Verdict, Stream)` formulation handled separately:

- **Remainder** (cursor advance): "wait for UEFI prompt" advances the read cursor on `tty0`; the updated StreamContext carries that position to the next oracle. Today there is no cursor, no ownership of "what did this step consume" — bytes are shared non-deterministically.

- **New stream** (registry mutation): "launch docker container" adds a new named stream (Docker logs) to the registry. "vcmuxer start" replaces `tty0`/`tty1` with multiplexed streams. The next oracle finds the updated registry.

### The State Machine Is Pattern-Free

With canonical oracles, the chain execution engine contains **zero hardcoded patterns**. It routes on verdict labels only. Platform-canonical patterns (UEFI shell prompt, serial escape sequences, extlinux menu) live in oracle implementations (`UEFIShellOracle`, `ExtlinuxMenuOracle`). Test-specific patterns live as oracle parameters in chain JSON. The chain JSON already partially does this — `wait_pattern` takes a `pattern` field. The new design makes this explicit: chain JSON configures oracle instances rather than parameterizing a monolithic step handler.

### StreamContext: Named Stream Registry

The oracle type uses `StreamContext` rather than a single stream because existing chains reference streams by persistent names — `tty0`, `vm0_guest_console_sink`, `driver_vm_console` — and any oracle can reference any named stream, not only "the stream the previous oracle produced."

`StreamContext` is a mutable map from stream name to `BiStream`, plus runtime metadata (chain aliases, lifecycle config), and a `cleanup_hooks: list[Callable]` for resources that must be released when the context is discarded. It is threaded through oracle execution. Sequential composition threads the updated context from A to B. `Parallel` snapshots the context before branching and merges on join.

**`cleanup_hooks` fork semantics:** when a `StreamContext` is forked for `Parallel` or `Race`, each child context starts with an *empty* `cleanup_hooks` list — not a copy of the parent's. Resources created *within* a branch register into that branch's hooks; they are cleaned up if the branch is discarded. Resources created *before* the fork belong to the parent context's lifetime and are not touched when a branch loses. This distinction is critical: without it, a losing Race branch would clean up resources that the winning branch still holds.

**Critical constraint: parallel and racing branches must reference disjoint streams.** A live UART or SSH stream is a character device — bytes consumed by branch A are gone and branch B cannot replay them. Forking a read cursor on a live stream is physically impossible. Therefore, `Parallel` *and* `Race` branches must each reference a different named stream from the registry; the engine asserts this at runtime for both combinators. If two branches genuinely need the same source, a Source oracle must first create a named tee (a pump that fans out one `BiStream` to two independent queues) before the fork. This matches how existing chains work in practice: parallel branches monitor different sources (`tty0` vs `vm_console_sink`), never the same one.

### Oracle Combinators

| Combinator | Semantics | Maps to current |
|---|---|---|
| `Sequence(A, B)` | Run A; pass updated StreamContext to B | Chained steps |
| `Choice(A, B, ...)` | Race patterns on the **same** stream; first match wins. Internally maintains a `bytearray` buffer — appends each `read()` chunk and tries all patterns via `re.search` on every append; first to match wins and the buffer position advances past the match. Equivalent to pexpect's `expect([r1, r2])`. Takes a `max_buf: int` parameter (default 1 MiB); if the buffer exceeds this before any pattern matches, the combinator yields `error("buffer_overflow")` rather than growing unboundedly. | `case` step |
| `Race(A, B, ...)` | Race **across different named streams**; each oracle references its own stream from the registry; first verdict wins. Subject to the same disjoint-stream constraint as `Parallel` — branches may not read the same named stream. `Race` calls `ctx.cleanup()` on every losing branch before discarding it. | `wait_pattern` outcomes across multiple sources |
| `Timeout(oracle, t)` | Fail with `timeout` verdict if deadline exceeded | `on_timeout` routing |
| `Parallel(A, B)` | Each branch gets a **snapshot** of the StreamContext with its own read cursors; join collects verdicts | `split` / `join` |
| `Repeat.monitor(oracle, *, max_iter, backoff)` / `Repeat.poll(oracle, success_label, *, max_iter, backoff)` | Two named factory functions encoding the two intent-specific defaults. **`monitor`**: continues on `matched`, stops on `timeout` or `error` — for infinite monitoring loops. **`poll`**: stops on `matched(success_label)`, retries on `timeout`, stops on `error` — for "retry until connected" patterns. The raw `Repeat(oracle, stop_on=..., ...)` constructor requires `stop_on` explicitly with no default, preventing the footgun where the wrong default silently loops or exits. `max_iter` exhausted → `error("max_iter_exceeded")`. | `monitor_ftrace_*` infinite loops, recovery retries |

**`Choice` vs `Race`:** `Choice` races multiple patterns on the *same* stream (one read cursor, first regex wins — the `case` step). `Race` runs independent oracles concurrently on *different* named streams (each with its own cursor — multi-source `wait_pattern`). Both return the verdict + updated StreamContext of the winner.

**`Parallel` is distinct from both.** Each branch gets a snapshot of the StreamContext so branches cannot interfere with each other's read positions. Reducer semantics (`any_pass`, `all_pass`) collect verdicts at join. This is the monitor branch model.

### Multi-Head Execution Is Preserved — Loops via `Repeat`

With no backward-compatibility constraint, both concerns are expressed directly in the combinator model:

- **Multiple simultaneous read heads** = `Parallel` with a snapshotted StreamContext. The monitor branch fires independently while the main flow runs. Each branch has its own read cursors on a copy of the StreamContext.
- **Recovery loops** = `Repeat(Sequence(MainFlow, RecoveryOracle))`. Rather than backward edges in a flat graph, the retry structure is expressed as a combinator. The iteration count, stop condition, and back-off are first-class parameters of `Repeat`.

The old flat graph with `next` pointer cycles is replaced by explicit `Repeat` — clearer intent, statically analysable (no cycle detection needed), and directly testable with a mock StreamContext.

### Oracle Classes (Full Taxonomy from Existing Chains)

All existing step types map to oracle classes. No step type requires a mechanism outside the oracle model:

| Oracle class | Examples | Mechanism |
|---|---|---|
| **Pattern** | `wait_pattern`, `case` | Read from named stream in StreamContext; emit verdict on regex match. **Known challenge:** on UART, kernel log messages arrive interleaved with application output and can prevent a pattern from matching (e.g., a `[timestamp]` message bisects `login:`). The oracle must tolerate this — either via retry-on-timeout, configurable noise filters, or line-based rather than stream-based scanning. |
| **Race** | `wait_pattern` with multi-source outcomes | `Race` combinator: each sub-oracle reads a different named stream; first verdict wins |
| **Command** | `ssh_cmd`, `send_cmd`, `reboot` | Write to named stream, read response; side effects allowed. **PTY echo:** when using a PTY-backed BiStream, the line discipline echoes the sent command back into the read stream before the response arrives. The Command oracle must either disable echo via ptyprocess (`p.setecho(False)`) or account for the echo in its response pattern. |
| **Poll** | `ssh_wait_ready` | `Repeat(Command(...))` with retry interval and total timeout |
| **Source** | `map_source`, `map_command_source`, `map_vcmux_source`, `spawn_process` | Add/replace named streams in StreamContext; PTY display attachment is a side effect of stream creation, not a separate step. `spawn_process` is the variant for long-lived background processes (simulator daemons, services under test): spawns the process, creates a stdout BiStream, applies a Pattern oracle to detect a readiness signal, then registers a kill as a cleanup_hook. This is `map_command_source` extended with a readiness gate. **Transition atomicity:** the in-process VCMux parser (Option B) ensures no bytes are dropped during the UART→multiplexed-streams transition — the pump loop runs continuously before and after stream registration, buffering bytes per-stream from the moment they arrive. |
| **Infrastructure** | `vcan_setup`, `vcan_teardown` | Host-side environment setup with no target hardware involved (e.g. virtual network interfaces via `ip link`). Teardown is registered as a cleanup_hook at setup time so it runs even when a later oracle fails — matching Robot Framework's suite teardown guarantee. Modelled as a Command oracle that writes nothing to a BiStream and whose sole output is a cleanup_hook side-effect. |
| **Upload** | `upload_efi`, `upload_file`, `upload_kernel` | SCP side effect; verdict on completion or checksum match |
| **Process** | `analyze_logs`, `run_robot` | Run subprocess; verdict on exit code or structured result; no stream consumed during the oracle call itself. For Robot Framework: runs `robot --outputdir <dir> <suite>`, awaits exit, reads `output.xml` via the RF result adapter, writes `results/<id>/robot/verdict.json` as a structured artifact side-effect, then returns `Verdict.matched("rf_pass")` or `Verdict.matched("rf_fail")`. The chain routes on the label; the structured per-test failure data lives in the artifact file and is retrieved separately by the MCP server or CI consumer. |
| **Interactive** | `interactive_console` | Expose named `BiStream` to human/AI; yield on completion signal |
| **Sync** | `signal_wait`, `wait_router_session`, `relay`, `purge_sources` | Synchronization primitives; advance StreamContext state |
| **Meta** | `set_overrides` | Modify StreamContext config (chain aliases, lifecycle); immediate verdict |
| **Verdict** | `check_verdict`, `setup_demo`, `set_test_verdict` | Record test-level outcome as side effect; immediate verdict — collapses the current two-step `set_test_verdict` + terminal pattern |
| **Terminal** | `pass`, `fail` | Propagate verdict to enclosing combinator (Sequence, Parallel, etc.) |

**`map_window` / `map_router_session_panes` disappear** as explicit step types: in the rewrite, attaching a PTY to a tmux pane is a side effect of Source oracle creation (the stream is created with a display target), not a separate oracle. This matches the PTY tee architecture: Python creates the BiStream and simultaneously attaches a display copy to tmux via libtmux.

All oracles are the same type; their verdict semantics differ by intent:
- **Navigation oracles** (`wait_pattern` for "Shell>") — verdict means "we reached this hardware state"
- **Lightweight verdict oracles** (`wait_pattern` for "Tests passed") — verdict means "we saw the pass/fail banner"
- **Proper verdict oracles** (RF oracle, exit code oracle) — verdict carries a label (`rf_pass`/`rf_fail`); structured test results are written to disk as artifact side-effects and retrieved separately

The chain author decides which intent applies; the engine is indifferent.

### The BiStream Type

`BiStream` is a single bidirectional channel — read and write go to the same underlying resource (UART fd, SSH channel, subprocess stdio). The `write_source` field found on `send_cmd` in some existing chains is a bug from the CAmkES-VM integration, not an intended design.

`BiStream` covers three distinct oracle behaviors:

- **Read-only** (pattern matchers): `wait_pattern`, `case`, boot prompt detection — consume bytes, emit verdict
- **Write-then-read** (command oracles): `ssh_cmd`, `send_cmd`, `uefi_shell_run` — write a command, then read for the response pattern
- **Interactive** (handoff oracles): expose both directions to an external actor; yield on a completion signal rather than a pattern match

### The Interactive Oracle: Bringing Up to State, Then Handing Off

A key use case is: Autopilot navigates hardware to a known state, then yields control to a human or AI for direct interaction via UART or SSH (visible as a PTY and/or tmux pane).

The "bring up to state, then hand off" pattern in the oracle model:

```
Sequence(
    BootOracle,          # navigation: get to known hardware state
    LoginOracle,         # navigation: authenticate
    InteractiveOracle    # handoff: expose BiStream to human or AI
)
```

`InteractiveOracle` is an oracle (not a combinator) that:
1. Attaches the session to a PTY and/or tmux pane (human visibility)
2. Simultaneously exposes read/write via the MCP server (AI access via Claude)
3. Waits for a completion signal — tmux keybinding, MCP `close_session` call, or timeout
4. Produces a verdict from that signal and returns the updated StreamContext to the next oracle

**Critical design point:** the same session is visible in tmux AND accessible to Claude via MCP simultaneously. Those are two views onto the same `BiStream`, not competing modes. A human can observe what the AI is doing, or take over, without mode-switching.

The seam between navigation and interaction is explicit: navigation oracles bring the system to a ready state; the interactive oracle is the handoff point where control moves from Autopilot's pattern-matching to a human or AI actor. After the session closes, the enclosing `Sequence` continues — Autopilot proceeds to artifact collection, teardown, or the next oracle.

`InteractiveOracle` is an oracle (not a combinator) and belongs in `adapters/interactive.py`. The core engine is unaffected.

### Robot Framework as a Process Oracle

Robot Framework is a domain-specific **test oracle**: it runs `.robot` keyword suites that encode acceptance criteria and produces binary pass/fail with precise per-test failure messages. Autopilot orchestrates the environment; RF decides what the evidence means.

The `RobotFrameworkOracle` (a Process oracle in `adapters/robot.py`) does:
1. Runs `robot --outputdir <dir> <suite>` as a subprocess
2. Awaits exit (within timeout)
3. Reads `output.xml` via `adapters/rf_xml.py` (thin adapter, ~30 lines)
4. Writes `results/<run_id>/robot/verdict.json` as a structured artifact
5. Returns `Verdict.matched("rf_pass")` or `Verdict.matched("rf_fail")`

The chain routes on the label. The structured per-test failure detail (`{pass: 11, fail: 1, failures: [{suite, test, message}]}`) lives in the artifact file and is retrieved by the MCP server or CI consumer after the chain completes. This is the standard oracle pattern: labels route the chain, artifacts carry the evidence.

**Host-only chains.** RF-based chains typically run entirely on the Linux host — virtual network interfaces are kernel features, simulator processes are local, no target hardware is involved. The oracle model handles this without special casing: oracles that reference no UART or SSH BiStream simply do not add them to StreamContext. The engine is indifferent to whether a chain targets hardware.

**Teardown guarantee.** `InfrastructureOracle` and `SpawnProcessOracle` register their teardown actions as `cleanup_hooks` at creation time. Because the engine calls `ctx.cleanup()` on the final context regardless of how the chain exits (pass, fail, timeout, error, SIGTERM), teardown always runs — no zombie processes or stale interfaces survive a failing RF test.

→ For the concrete chain shape, process names, interface layout, and RF test suite details specific to a given project, see that project's Autopilot integration notes.

### Relation to pexpect

`pexpect.expect()` has a similar intuition (match and consume up to the match) and does support writing via `sendline()`. What it lacks: no native `Sequence`, `Choice`, or `Race` as first-class composable types; no StreamContext (single stream only); no `Parallel` with forked cursors. The oracle library would be a proper combinator layer on top of raw byte streams, testable with mock StreamContexts without hardware.

### Prior Art Survey

A search for existing tools confirms the problem space is real but no off-the-shelf solution covers all requirements. Key findings:

**labgrid** ([github](https://github.com/labgrid-project/labgrid)) is the closest prior art: an open-source embedded systems control library handling serial, SSH, power control, and pytest integration with distributed resource management. It confirms the architecture is non-trivial and worth learning from — particularly for resource allocation (board locking, distributed queue management). It does not have a chain-based workflow model, oracle composition, or the "bring up to state, hand off to human/AI" interactive pattern. Notably, `labgrid/driver/power/` contains standalone HTTP/SNMP clients for 25+ PDU models that are directly importable as power adapter backends without adopting the rest of labgrid. → [detailed study](prior-art-labgrid.md)

**pexpect** has a similar intuition to the Pattern oracle (match and consume up to the match) and does support writing via `sendline()`. What it lacks: no native `Sequence`, `Choice`, or `Race` as first-class composable types; no StreamContext (single stream only); no `Parallel` with forked cursors. **ptyprocess** (pexpect's PTY transport layer) is a viable foundation for PTY-backed BiStream implementations. → [detailed study](prior-art-pexpect-ptyprocess.md)

**pyte** ([github](https://github.com/selectel/pyte)) is a pure Python VT100 emulator that parses terminal output in-process. Tempting for avoiding subprocess `capture-pane` calls, but it renders bytes into a character grid — same data-fidelity loss as tmux `capture-pane`. Not usable for oracle pattern matching on raw streams; usable on the display side of the tee.

**Zellij, Ratatui, Textual** — modern and capable for display, but none provide raw PTY stream management with Python-native programmatic access. Display frameworks, not stream owners. → [detailed study](prior-art-display-alternatives.md)

**Verdict:** No library solves "raw byte stream + human display + programmatic access + concurrent multiple streams" in one package. The PTY tee architecture (Python owns raw bytes, display layer gets a copy) is the right model and is validated by the ecosystem. The BiStream + InteractiveOracle design is novel.

### Recommended Library Foundations

These replace current ad-hoc implementations in the rewrite:

| Library | Replaces | Role |
|---|---|---|
| **libtmux** | Raw `subprocess("tmux ...")` calls in `tmux_ui.py` | Typed Python API over tmux IPC for display/pane management. **Pre-1.0: API breaks across minor versions — pin the version.** Display tee via `pipe-pane -I 'cat <pty-slave-path>'`. |
| **ptyprocess** | Ad-hoc PTY handling scattered across modules | Raw PTY fd access used by PTY-based adapters to back their `BiStream` instances |
| **pyserial-asyncio-fast** | Synchronous `pyserial` thread loops | Async UART adapter — `asyncio.StreamReader/StreamWriter` over serial. Use `pyserial-asyncio-fast`, NOT the original `pyserial-asyncio` (original blocks the event loop and is being deprecated by Home Assistant 2026-07) |
| **asyncssh** | Synchronous paramiko / subprocess ssh calls | Asyncio-native SSH — command execution, SFTP upload, interactive PTY sessions. Paramiko is synchronous and thread-based; incompatible with the asyncio commitment. |
| **docker SDK (docker-py 7.1.0)** | Ad-hoc subprocess docker calls | Container lifecycle + log stream tailing for the Docker adapter. Log stream framing uses an 8-byte Docker multiplexing header (stdout/stderr + length). Use docker-py; aiodocker is inactive (last release 12+ months ago). |
| **zenoh-python** | Ad-hoc Zenoh calls in vcmuxer integration | Pub/sub for the Linux target/Docker path only — NOT applicable to the CAmkES/seL4 UART path (seL4 has no networking). For seL4: in-process VCMux parser (see below). |
| **structlog** | Direct `print()` and unstructured logging | Structured context logging (request_id, oracle_type, stream_name) with asyncio `contextvars` propagation across tasks. Significantly better than stdlib `logging` for correlated multi-stream orchestration logs. |
| **pytest-asyncio** | — (no tests currently) | Asyncio-native test infrastructure for oracle unit tests with mock StreamContexts. Use `asyncio_mode = "auto"` for a test suite built from scratch. |
| **labgrid power backends** (selective import) | — | `labgrid/driver/power/` contains standalone HTTP/SNMP clients for 25+ PDU models, importable without adopting labgrid's architecture. Reuse specific backends (e.g., `labgrid.driver.power.gude`) directly as power control adapters. |
| **labgrid** (study only) | — | Resource allocation patterns (coordinator/exporter/place model); do not adopt wholesale |

→ [libtmux and pyserial-asyncio-fast detailed study](prior-art-libtmux-pyserial-asyncio.md)  
→ [asyncssh detailed study](prior-art-asyncssh.md)  
→ [Docker SDK for Python detailed study](prior-art-docker-sdk.md)  
→ [zenoh-python detailed study](prior-art-zenoh-python.md)  
→ [VCMux transport options — in-process parser vs vcmuxer subprocess vs Zenoh](vcmux-transport-options.md)  
→ [pytest-asyncio and structlog detailed study](prior-art-pytest-asyncio-structlog.md)

**Display strategy fork:** the plan currently commits to libtmux + `pipe-pane -I` for human display. An alternative upgrade path exists: **pyte + Textual** (Python-native TUI, eliminates tmux hard dependency, richer multi-stream dashboard). Recommended approach: start with libtmux (it is what exists and works), defer Textual migration to a post-rewrite phase if the display layer becomes a pain point. The oracle and MCP paths are unaffected by this choice — it only affects the human display side of the tee.

---

## Structural Problems (Current Codebase)

### 1. `chain_runtime.py` Is a 3,561-Line Monolith (32+ Step Types)

Handles serial UART I/O, ANSI stripping, SSH, SCP, Docker log tailing, vcmuxer process lifecycle, QEMU router integration, parallel fork/join, tmux pane management, signal coordination, and artifact recording — all in one file with a large if-elif dispatch. Every new platform adds more step types here.

**Rewrite direction:** Core engine handles branch/outcome/combinator/recording. Platform adapters (`uart`, `ssh`, `docker`, `qemu`, `vcmux`) register their oracle types. New platforms add adapters, not if-elif branches.

### 2. Naming Is Frozen in the seL4 Era

`sel4_client.py`, `sel4_mcp_server.py`, `orin_kernel_autopilot.py`, MCP tools `test_sel4_efi` / `check_sel4_test` — all reflect what the project was. They now serve Docker, Zenoh, QEMU, and Linux target workloads.

**Rewrite direction:** `autopilot_client.py`, `autopilot_mcp_server.py`, `autopilot_daemon.py`. MCP tools: `submit_chain`, `check_test`, `get_logs`.

### 3. Chain JSON Is the Wrong Shape, and Has No Type Safety

Chains are raw dicts accessed with `.get()` everywhere. No validated schema. No IDE support. But more fundamentally, the flat step graph with `next` pointer routing was a workaround for the absence of native oracle composition. The format encodes the wrong abstraction — control flow is expressed as a labelled graph with jump targets, where combinators (sequence, choice, parallel) should be expressed directly.

**Rewrite direction:** Design a new chain JSON schema that expresses oracle combinators as first-class constructs. For example:

```json
{ "schema_version": 1, "oracle": "sequence", "steps": [
    { "oracle": "uart_pattern", "pattern": "Shell>", "timeout": 60 },
    { "oracle": "choice", "options": [
        { "pattern": "PASSED", "verdict": "pass" },
        { "pattern": "FAILED", "verdict": "fail" }
    ]}
]}
```

Pydantic models (`OracleDef`, `SequenceDef`, `ChoiceDef`, `RaceDef`, `ParallelDef`, `RepeatDef`) give load-time validation and IDE support. The 36+ existing chains are converted to the new schema — they are the migration corpus, not a format constraint. Every chain JSON must include `"schema_version": 1` from day one; the loader rejects chains with a missing or mismatched version rather than silently misparsing them. This protects against schema evolution during development.

### 4. Configuration Is Flat Env Vars Only

`config.py` is 111 lines of `os.environ.get()` with hardcoded defaults. Multi-platform support jammed into one flat namespace. No per-platform config, no config file support.

**Rewrite direction:** Layered config: defaults → platform profile → env overrides → per-request overrides. Platform config files (`platforms/orin-agx.yaml`, `platforms/qemu-generic.yaml`).

### 5. Error Handling: Broad Catches, Silent Failures

108 instances of `except Exception` and 30 `except Exception: pass`. In a hardware orchestrator, a silent failure means "the test ran but nothing happened." This is a correctness problem.

**Rewrite direction:** Specific exception types per transport layer (`SerialError`, `SSHError`, `OracleError`). Cleanup paths log-and-suppress. Fatal errors propagate to oracle verdict (`error(reason)` → `on_error` routing).

**`CancelledError` interaction with broad catches:** `CancelledError` is a `BaseException` (Python 3.8+), so `except Exception` does not catch it — asyncio cancellation propagates correctly through most code. However, `except BaseException:` or bare `except:` clauses do catch it and will silently block task cancellation if they don't re-raise. This directly conflicts with the W10 signal handling mitigation: if any oracle adapter swallows `CancelledError`, graceful shutdown hangs. **Coding rule for the new codebase:** never use `except BaseException` or bare `except:` outside the top-level shutdown handler. In cleanup paths that use `except Exception`, add an explicit check: `if isinstance(exc, asyncio.CancelledError): raise`. This rule must be stated in the project's contributing guide and enforced in code review.

### 6. No Logging Framework

Direct `print()`, file appends, and stdout JSON with no log levels. Debugging multi-threaded orchestration with parallel branches requires correlating uncoordinated print sites.

**Rewrite direction:** `structlog` with `contextvars`-based context propagation (request_id, oracle_type, stream_name) across `await` boundaries. Debug for stream I/O, info for oracle transitions, warning for retries, error for failures. `make_filtering_bound_logger` makes `log.debug()` a no-op at INFO level — essential when logging every byte of a serial stream.

### 7. No Test Infrastructure

Zero automated tests. Every change is manually validated on live hardware. A rewrite without a test suite is extremely high risk.

**Rewrite direction — build this first:**
1. Unit tests for pure-logic modules: `analyze_sel4log.py`, `tty_match.py`, `config.py` (no migration needed, these survive the rewrite)
2. Oracle unit tests with mock StreamContexts: feed byte sequences into oracles, assert `(verdict, updated_context)` correctness without hardware — written against the new engine as it is built
3. Chain migration tests: as each existing chain is converted to the new combinator schema, the migrated chain is loaded through the new Pydantic validator and executed against a mock StreamContext as a behavioural regression test

---

## Proposed Module Structure (Rewrite)

```
autopilot/
├── engine/
│   ├── oracle.py          # Oracle base type, Verdict, BiStream, StreamContext
│   ├── combinators.py     # Sequence, Choice, Race, Timeout, Parallel, Repeat
│   ├── runtime.py         # Chain executor: combinator evaluation, recording, signal coordination
│   └── recorder.py        # ChainRecorder (extracted from chain_runtime.py)
├── adapters/
│   ├── uart.py            # Serial UART oracle implementations (pyserial-asyncio-fast)
│   ├── ssh.py             # SSH command/wait/upload oracles (asyncssh)
│   ├── process.py         # Subprocess / map_command_source oracles
│   ├── docker.py          # Docker log tailing oracles (docker-py 7.1.0; 8-byte mux header aware)
│   ├── vcmux.py           # VCMux: in-process 0xfe frame parser on UART BiStream (seL4/CAmkES);
│   │                      #   Zenoh bridge possible for Linux target path once virtioso-muxd gains publisher
│   ├── robot.py           # RobotFrameworkOracle + rf_xml adapter; host-only, no BiStream consumed
│   └── interactive.py     # InteractiveOracle: PTY/tmux + MCP handoff; completion via Unix socket signal channel
├── model/
│   ├── chain.py           # Pydantic: OracleDef, SequenceDef, ChoiceDef, RaceDef, ParallelDef, RepeatDef
│   └── config.py          # Layered config with platform profiles
├── platforms/
│   ├── orin-agx.yaml      # Platform-specific oracle defaults (UEFI prompts, baud rates)
│   └── qemu-generic.yaml
├── daemon.py              # Queue polling, lifecycle, tmux UI (thin layer)
├── client.py              # Renamed from sel4_client.py
└── mcp_server.py          # Renamed from sel4_mcp_server.py, domain-neutral MCP tools
```

**No backward compatibility constraint.** Autopilot is used only between the authors. The 36+ existing chains are a **migration corpus** — a behavioural specification of what the rewrite must reproduce, not a format constraint. They get converted to the new schema as part of the rewrite. The new chain JSON is designed purely to express oracle combinators clearly, without legacy workarounds.

---

## Version Control Strategy

### Goals

Three things must hold simultaneously during the rewrite:
1. Old code stays accessible and runnable as a reference (and to capture pre-rewrite baselines — see W4)
2. Rewrite development can be messy — WIP commits, wrong turns, mid-step states are fine
3. The final result has a clean history that reads as if it was built from scratch in logical steps

No single git workflow gives all three automatically. The combination of an **orphan branch** and a **git worktree** does.

### Setup: orphan branch + worktree

```bash
cd ~/autopilot
git switch --orphan rewrite
git commit --allow-empty -m "rewrite: root"
git switch backup                          # return to current code
git worktree add ../autopilot-rewrite rewrite
```

This produces two live checkouts sharing one `.git` object store:

| Path | Branch | Purpose |
|---|---|---|
| `~/autopilot/` | `backup` | Old code — runnable, referenceable, untouched |
| `~/autopilot-rewrite/` | `rewrite` | New code — developed freely |

The `rewrite` branch is an **orphan**: it has no shared commits with `backup` or any other branch. The rewrite history never contaminates the old tree.

During development, both codebases are simultaneously accessible:
- Run the old Autopilot from one terminal, the new one from another
- `git diff backup:chain_runtime.py rewrite:engine/runtime.py` works natively across the two trees — no copying needed

### Development: commit freely

Commit as messily as needed on the `rewrite` branch. WIP commits, typo fixes, reverts — none of it matters because the history is cleaned up before shipping.

### End: squash and replay

When the rewrite is stable, clean the commit history with an interactive rebase from the orphan root:

```bash
cd ~/autopilot-rewrite
git rebase -i --root
```

This squashes WIP commits, reorders, and edits messages into a sequence of logical steps — without affecting any other branch, since the orphan has no shared history. The result reads as if the rewrite was built cleanly from day one.

Verify it is self-contained and portable by replaying on a fresh repo:

```bash
git format-patch --root rewrite -o /tmp/rewrite-patches
mkdir /tmp/autopilot-clean && cd /tmp/autopilot-clean
git init
git am /tmp/rewrite-patches/*.patch
```

If `git am` applies cleanly, the history is ready to publish as a standalone repo.

### Properties

| Property | Mechanism |
|---|---|
| Old code stays runnable | `git worktree` — two checkouts, one object store, no disk duplication |
| Old code referenceable | `git diff backup:file rewrite:file` works natively |
| Messy WIP commits allowed | Orphan branch — no shared history to protect |
| Final history is clean | `git rebase -i --root` before shipping |
| Replay on empty repo | `git format-patch --root` + `git am` on fresh `git init` |

**Known trade-off:** `git log --all` shows two disconnected root commits. Some git GUIs and `git describe` assume a single root and may behave oddly. No impact on CLI use.

---

## Plan Strengths and Known Weaknesses

### Strengths

1. **Clean theoretical grounding** — The oracle/combinator model maps cleanly to all 28 existing step types with no exceptions. StreamContext elegantly handles both cursor advancement (remainder semantics) and stream registry mutation (new/replaced sources) in one concept.
2. **Inherently testable without hardware** — Oracles take a StreamContext and return a verdict. Mock byte sequences can verify oracle behaviour end-to-end before a board is touched. This is the single largest quality-of-life improvement over the current system.
3. **True separation of concerns** — Engine (combinator evaluation), adapters (transport), and model (chain JSON schema) are independent layers. New platforms add adapters only.
4. **asyncio throughout** — Committing to asyncio (not threads) makes concurrency errors visible rather than swallowed. `Parallel` = `asyncio.gather()`, `Race`/`Choice` = `asyncio.wait(return_when=FIRST_COMPLETED)`. Integrates cleanly with `pyserial-asyncio-fast`.

### Known Weaknesses and Mitigations

**W1 — StreamContext merge semantics for `Parallel` and resource cleanup for `Race` are underspecified.**  
When parallel branches create divergent StreamContexts (different cursors, different new streams), "merge on join" is ambiguous. Separately: when `Race` discards losing branches, any resources those branches acquired (SSH connections, Zenoh subscribers, PTYs) are silently leaked.  
*Mitigation:* Merge semantics — union of newly created streams across branches (new streams from any branch are visible after join); no cross-branch cursor merging (branches reference disjoint streams per the constraint above). `Race` cleanup — every oracle that acquires resources registers a `Callable` in `ctx.cleanup_hooks`; the `Race` combinator calls `ctx.cleanup()` on every losing branch's context before discarding it. `cleanup_hooks` is also called by `Parallel` on branch contexts that are not forwarded to the next step.

**W2 — `Timeout` appears at two levels.**  
`Timeout` is both a parameter of the oracle type signature and a combinator, which could lead to two separate mechanisms being built.  
*Mitigation:* Document the relationship explicitly: the `Timeout(oracle, t)` combinator is the chain-author-facing API that sets the execution budget; that budget becomes the `Timeout` parameter threaded into the wrapped oracle. One mechanism, two faces.

**W3 — No incremental path to a working system.**  
The rewrite order leaves the system completely non-functional until step 8. Hardware flaws discovered late are expensive.  
*Mitigation:* The rewrite order (steps 1–4) builds the engine and tests without touching hardware. Step 5 introduces real hardware early: new adapters (built on new `BiStream`/`StreamContext` types) are wired into the old daemon dispatch temporarily. This validates the transport layer on real hardware before the full engine integration at step 8.

**W4 — Chain migration effort is underestimated, and regression baselines are absent.**  
The plan says "migrate one by one" with no sequencing, and "each migrated chain is a behavioural regression test" — but the reference is the current system's behaviour, which is undocumented and produced on real hardware that may not be available during development.  
*Mitigation:* Before the rewrite begins, run the 5–6 most complex chains on real hardware and record their behaviour. Do not use raw byte streams as the regression target — raw UART output is not reproducible (kernel log timestamps differ every boot, timing varies by hundreds of milliseconds). Instead, capture *verdict sequences and key match events*: which pattern matched, on which stream, at what relative offset, producing which verdict. The raw bytes are kept as debugging artifacts but the regression test asserts on the event sequence, not the bytes. Order migration by complexity — simple linear chains first (`boot_stock_linux`, `wait_for_elfloader`), complex parallel chains last (`vm-qemu-virtio`, `vm_common_orin_vcmuxer`). Oracle model flaws surface early on simple cases.

**W5 — `set_overrides` requires late binding of chain references.**  
`set_overrides` mutates chain aliases at runtime. Eagerly-resolved chain references make this a no-op.  
*Mitigation:* State as a design constraint: chain references in oracle definitions are deferred — resolved from StreamContext at execution time, not parse time.

**W6 — `setup_demo` and `check_verdict` are opaque.**  
Their actual mechanics in `chain_runtime.py` haven't been read. Their oracle mapping may not be trivial.  
*Mitigation:* Read `chain_runtime.py`'s implementation of these step types before finalising the Verdict oracle class. Do not assume they are simple.

**W7 — Headless mode for display is unspecified.**  
The plan says PTY display attachment is a side effect of Source oracle creation, but doesn't specify what happens in headless mode (CI, no tmux) or when `map_vcmux_source` replaces an already-displayed source.  
*Mitigation:* Source oracle creation accepts an optional display target (tmux pane, log file, or null). In headless mode the display target is a log file. Stream replacement notifies the previous display target to detach.

**W8 — InteractiveOracle completion signal and MCP data path are both undesigned.**  
The plan says InteractiveOracle "waits for a completion signal — tmux keybinding, MCP `close_session` call, or timeout," but libtmux has no event or callback model. Separately: when Claude (via MCP) calls `write_to_console("ls\n")`, there is no defined path for that write to reach the oracle's `BiStream`, and no defined path for UART output to reach MCP's `read_console()`. The MCP server and the oracle run in different asyncio tasks.  
*Mitigation — completion signal:* A companion Unix socket that the sentinel shell command (a separate process) writes to on keypress; InteractiveOracle awaits the socket via `asyncio.open_unix_connection`. MCP `close_session` writes to the same Unix socket. Timeout wraps the whole wait. (An in-process `asyncio.Queue` is not sufficient — the sentinel command is a shell subprocess.)  
*Mitigation — data path:* InteractiveOracle creates a `ConsoleBridge(read_q: asyncio.Queue[bytes], write_q: asyncio.Queue[bytes], done: asyncio.Event)` and registers it in a session registry keyed by session_id. The oracle pumps the underlying BiStream into `read_q` and drains `write_q` to the BiStream. MCP tools `read_console(session_id)` and `write_console(session_id, data)` look up the bridge by session_id. The registry entry must be removed in a `finally` block — not only in the normal completion path — so that cancellation (SIGTERM, Race loser, timeout) does not leave a stale entry pointing to a closed bridge. MCP lookups of a missing or closed session_id must return a clean `session_closed` error rather than propagating internal asyncio state. Both paths must be designed before implementing `adapters/interactive.py`.

**W9 — `Repeat` stop conditions are unspecified, and a single default serves neither use case well.**  
The plan says `Repeat` "runs oracle until it fails or stop condition is met" but does not define what counts as failure. A single `stop_on` default cannot serve both use cases: monitoring loops (continue on match, stop on timeout) and polling loops (stop on match, retry on timeout). Whichever default is chosen surprises users of the other pattern.  
*Mitigation:* Expose two named factory functions that encode intent explicitly: `Repeat.monitor(oracle, *, max_iter, backoff)` for infinite monitoring (continues on `matched`, stops on `timeout` or `error`) and `Repeat.poll(oracle, success_label, *, max_iter, backoff)` for polling (stops on `matched(success_label)`, retries on `timeout`, stops on `error`). The underlying `Repeat(oracle, stop_on=...)` constructor requires `stop_on` explicitly with no default, so a call without a factory is unambiguous at the call site. `max_iter` exhausted → `error("max_iter_exceeded")`. These factory functions are already reflected in the combinators table above.

**W10 — Signal handling and graceful shutdown are absent from the plan, and SIGKILL is unmitigable.**  
The rewrite plan has no mention of SIGINT or SIGTERM. In hardware orchestration, an unclean shutdown leaves the board in an undefined state: USB relay asserted (board stuck powered off), serial fd held (exclusive lock preventing other tools from connecting), SSH connections leaked, tmux windows orphaned. The current system must handle this; the rewrite must too.  
*Mitigation for SIGINT/SIGTERM:* Install asyncio signal handlers in `daemon.py`; on shutdown, cancel all running oracle tasks with `CancelledError` (which propagates correctly through `except Exception` clauses — see Structural Problem 5 for the `CancelledError` coding rule); each adapter's branch-local `cleanup_hooks` releases hardware resources. The standard pattern is `asyncio.run(main())` with a `try/finally` that awaits an explicit `shutdown()` coroutine. Hardware-critical cleanup (relay release) should be structured to run synchronously as a last resort if the event loop has already exited.  
*Known limitation — SIGKILL:* `kill -9` bypasses all signal handlers and `finally` blocks; no software mitigation is possible. A USB relay left asserted or a serial lock file held after SIGKILL requires a manual recovery procedure. Document the procedure (toggle relay via CLI, remove lock file) in the operational runbook rather than pretending it can be prevented in code.

**W11 — StreamContext mutation semantics are ambiguous: mutable shared object vs. pure functional threading.**  
The plan says StreamContext is "a mutable map" and is "threaded through oracle execution," but also that `Sequence` passes the "updated StreamContext" from A to B, and that `Timeout` wraps an oracle that "returns" a new StreamContext. These two framings are in tension. If oracles mutate the context in-place (like a mutable dict), then: (a) `Timeout` cannot roll back a partially-mutated context when it cancels the inner oracle — the outer code sees a half-modified object with inconsistent state; (b) `Parallel` branch isolation is broken — two tasks sharing a reference to the same StreamContext object will see each other's mutations even though they are supposed to be isolated. If oracles return a new context (pure functional), then: (c) the mutable `cleanup_hooks` list can't be appended to by sub-oracles without returning a new context from every intermediate step. The plan cannot have it both ways.  
*Mitigation:* Pick one model and state it explicitly. The correct choice for the combinator model is **copy-on-fork, mutate-in-place within a task**:
- Within a single sequential execution (one asyncio task), oracles may mutate the StreamContext in-place — they hold the only reference.
- At a fork point (`Parallel`, `Race`), the engine calls `ctx.fork()` which produces a shallow copy of the stream registry and an empty `cleanup_hooks` list. Each branch receives its own forked copy and mutates it freely.
- The `Timeout` combinator wraps its oracle in `asyncio.wait_for`. On cancellation, the inner oracle's task is cancelled. Any mutations the inner oracle made to `ctx` before cancellation are **visible** to the outer Timeout — there is no rollback. This is acceptable: when Timeout fires, it returns `(Verdict.timeout, ctx)` where `ctx` may have partial mutations (e.g. a stream was added mid-way). The chain terminates at that point; the final `ctx.cleanup()` call handles all registered hooks regardless. The key invariant: **a Timeout verdict is always terminal** — no subsequent oracle receives a partially-mutated context and tries to continue as if the timed-out oracle had succeeded.
- Explicitly document in the engine that `ctx` received by an oracle is "owned by this task for the duration of the call" — callers must not retain a reference to the same object after passing it to an oracle.

*Secondary problem created:* If `ctx` is mutated in-place and `Parallel` uses `asyncio.gather()`, both branch tasks share the pre-fork object until `ctx.fork()` is called. The engine implementation must call `fork()` before spawning branch tasks, not after. This is an implementation constraint that must be stated explicitly. Also: `ctx.fork()` must deep-copy the stream registry dict (not just the reference) but may shallow-copy the BiStream objects themselves (BiStreams are not duplicated — each branch holds a reference to the same underlying transport, which is valid because the disjoint-stream constraint prevents two branches from reading the same stream).

**W12 — Sequence error propagation is undefined: does oracle B run after oracle A returns `error(reason)`?**  
The plan says "fatal errors propagate to oracle verdict" (Structural Problem 5) and implies that `error` verdicts cause routing to an `on_error` handler, but `Sequence(A, B)` has no on_error handler — it just chains A then B. If A returns `error(reason)`, the plan gives no specification for whether B runs, whether the error verdict is returned immediately, or whether B receives the error-state context. In the current system, a `fail` verdict on a step terminates the chain — this is the expected behaviour that must be preserved. If Sequence does not short-circuit on error, then every oracle B in `Sequence(A, B)` must defensively check for an error context, which is not the combinator model's intent.  
*Mitigation:* `Sequence` short-circuits on any non-`matched` verdict from A: if A returns `timeout` or `error(reason)`, Sequence immediately returns that same verdict without running B. Only a `matched(label)` verdict from A triggers B. This matches the existing system's behaviour (a failed step stops the chain) and is the natural semantics for sequential composition. Document this as a first-class invariant of `Sequence`:

```
Sequence(A, B):
  (v, ctx) = await A(ctx, timeout)
  if v is not matched(_): return (v, ctx)   # short-circuit
  return await B(ctx, timeout)
```

*Secondary problem:* `Sequence` has a single timeout budget that it passes unchanged to each step. A chain with 10 steps and a 60-second Sequence timeout gives each step 60 seconds individually — the total could be 600 seconds. This is almost certainly not the intent. The natural expectation is that the timeout is a wall-clock deadline shared across all steps. Mitigation: `Sequence` tracks elapsed time and reduces the timeout passed to each subsequent step: `remaining = deadline - asyncio.get_event_loop().time(); await B(ctx, remaining)`. If `remaining <= 0` before B starts, Sequence returns `timeout` immediately. This makes Sequence's timeout a true wall-clock budget.

**W13 — Sub-chain invocation is missing entirely from the oracle model.**  
The current system has `call_chain` to invoke another chain as a sub-step. The plan mentions 36+ existing chains but gives no mechanism for one chain to call another. Without sub-chain invocation: (a) any shared hardware setup sequence (boot to EFI, mount filesystems) must be copy-pasted into every chain that needs it; (b) the existing `call_chain` steps in the migration corpus have no target representation in the new schema. This is not a detail — it is a fundamental modularity gap.  
*Mitigation:* Add a `SubChain` oracle to the taxonomy. `SubChain(chain_id)` looks up `chain_id` in a chain registry (loaded from the `chains/` directory), instantiates the oracle tree for that chain from JSON, and executes it against the current StreamContext. The sub-chain inherits the caller's StreamContext (including all streams and metadata) and its cleanup_hooks are merged into the caller's context at return. This is analogous to a function call: the sub-chain can add streams, advance cursors, and register cleanup_hooks; all effects are visible in the parent context after return.

`SubChain` must handle recursive invocation detection (a chain calling itself) to prevent infinite loops — a runtime check against a call stack in StreamContext metadata is sufficient. `schema_version` must be checked at sub-chain load time, not just at the top-level chain load.

The chain registry is a flat directory of JSON files keyed by filename (without `.json` extension). The daemon loads all chains at startup and re-scans on `SIGHUP`. Chain references in `SubChain` are resolved from this registry at execution time (deferred resolution, consistent with the W5 mitigation).

*Secondary problem created:* Sub-chain execution introduces the possibility that a sub-chain's timeout interacts badly with the parent chain's timeout. `SubChain(chain_id)` receives the parent's remaining timeout — if the sub-chain takes the full budget, the parent has no remaining time. This is correct behaviour: the parent's Timeout combinator will fire. Document that `Timeout(SubChain(...), t)` is the recommended idiom to bound sub-chain execution without consuming the parent's budget.

**W14 — Chain JSON recursive Pydantic serialization is unspecified and non-trivial.**  
The plan proposes Pydantic models (`OracleDef`, `SequenceDef`, `ChoiceDef`, `RaceDef`, `ParallelDef`, `RepeatDef`) for the chain JSON schema and shows a trivial example. It does not address how to express arbitrarily nested combinator trees, which is required for any real chain. `Sequence(Choice(...), Race(...))` requires recursive Pydantic models with forward references and a discriminated union on the `oracle` field — a pattern that works in Pydantic v2 but requires explicit handling. Without this, the schema cannot express the combinator trees the plan depends on.  
*Mitigation:* Use Pydantic v2 discriminated unions with a string `oracle` discriminator field. Define a `OracleDef` as a `Union` of all oracle types, each with a `Literal` type tag:

```python
from __future__ import annotations
from pydantic import BaseModel
from typing import Annotated, Literal, Union
from pydantic import Field

class SequenceDef(BaseModel):
    oracle: Literal["sequence"]
    steps: list[AnyOracleDef]

class ChoiceDef(BaseModel):
    oracle: Literal["choice"]
    options: list[PatternOptionDef]
    max_buf: int = 1024 * 1024

class RaceDef(BaseModel):
    oracle: Literal["race"]
    branches: list[AnyOracleDef]

class SubChainDef(BaseModel):
    oracle: Literal["sub_chain"]
    chain_id: str

# ... other defs ...

AnyOracleDef = Annotated[
    Union[SequenceDef, ChoiceDef, RaceDef, ParallelDef, RepeatDef,
          TimeoutDef, PatternDef, CommandDef, SourceDef, SubChainDef, ...],
    Field(discriminator="oracle")
]

# Required for Pydantic v2 forward references:
SequenceDef.model_rebuild()
RaceDef.model_rebuild()
ParallelDef.model_rebuild()
RepeatDef.model_rebuild()
```

The `model_rebuild()` calls resolve forward references after all models are defined. This pattern is the standard Pydantic v2 approach for recursive schemas and must be established before any chain JSON is written — retrofitting it later is painful. Also: every oracle definition must include `schema_version` only at the top level (not on every node), and the loader validates it before calling `model_validate`.

**W15 — The daemon's request queue, event loop ownership, and result delivery are all unspecified.**  
`daemon.py` is listed in the module structure with the comment "Queue polling, lifecycle, tmux UI (thin layer)" but is otherwise empty of design. Three specific problems: (a) **Simultaneous submissions**: two chains submitted at once — are they queued and serialized, or run in parallel? On shared hardware, parallel execution of two chains targeting the same UART is physically impossible and would produce corrupted results. (b) **Event loop ownership**: the daemon runs an asyncio event loop; the MCP server likely runs its own (FastMCP or similar). Who owns the loop and how do cross-component calls work? (c) **Result delivery**: `client.py` submits a chain — how does it learn the result? The plan is silent on the IPC protocol between client and daemon.  
*Mitigation:*
- **Serialized queue**: the daemon maintains an `asyncio.Queue` of pending chain requests. A single chain executor coroutine drains this queue one at a time. Parallel hardware access is prevented by serialization — not by locks. This matches the current system's behaviour and is the correct model for hardware with exclusive UART/relay ownership. If parallel chains targeting *different* hardware platforms are needed in future, the daemon can maintain one queue per hardware target.
- **Event loop**: the daemon owns the single asyncio event loop. The MCP server runs as a coroutine within the same loop (`asyncio.create_task(mcp_server.run())`), not as a separate thread or process. FastMCP / `mcp[server]` supports `asyncio.run()` or being started as a task.
- **Result delivery**: use a Unix domain socket for client/daemon IPC. The protocol is line-delimited JSON: client sends `{"chain_id": "...", "overrides": {...}}`, daemon responds with a stream of status events (`{"event": "started", "run_id": "..."}`, `{"event": "verdict", "verdict": "matched", "label": "pass"}`, `{"event": "done"}`). The client blocks on the socket until `done`. This replaces the current implicit stdout/file coupling.

*Secondary problem:* The Unix socket IPC means `client.py` and `daemon.py` must agree on the socket path. Add `AUTOPILOT_SOCKET` to the layered config (Structural Problem 4 mitigation) with a default of `/tmp/autopilot.sock`. The daemon creates the socket at startup and removes it at shutdown (in the `try/finally`).

**W19 — VCMux Option B queue full drops bytes silently, unlike vcmuxer's OS-level backpressure.**  
In `VCMuxParser._enqueue()`, `put_nowait()` raises `asyncio.QueueFull` if the per-stream queue is at capacity (`maxsize=65536`). The current code silently drops those bytes. This differs from vcmuxer's behaviour: vcmuxer uses OS PTY buffering, which applies backpressure to the UART read rather than dropping. Silent byte drop means pattern oracles can miss expected text — `wait_pattern("login:")` might never match because the key bytes were dropped. In a hardware test, this appears as a spurious timeout, not a visible error.  
*Mitigation:* The pump loop must never silently drop bytes. Use `await queue.put(data)` instead of `put_nowait` in the pump task — this suspends the pump until the consumer drains the queue, applying backpressure to UART reads. If a slow consumer fills its queue, the pump suspends and the UART driver's kernel buffer absorbs the backpressure. Document the queue size (65536 bytes) as a tunable per-stream parameter in the StreamContext stream metadata, not a hardcoded constant. Add a structlog warning when a queue approaches capacity (>80% full) so slow-consumer bugs surface in logs before they cause silent drops.

**W20 — `Race` cancels losing branch contexts but not the branch tasks themselves.**  
The mitigation for W1 says `Race` calls `ctx.cleanup()` on every losing branch before discarding it. But `asyncio.wait(return_when=FIRST_COMPLETED)` leaves the losing branch tasks in the "pending" set — they are still running. `cleanup()` releases resources (closes connections, deregisters hooks) but does not cancel the asyncio tasks. The losing tasks will continue running until their oracle naturally returns, blocking on stream reads that may never produce data. They consume event loop resources and may produce spurious log output. In the worst case, a losing branch that has applied backpressure to a shared resource (like a UART buffer via W19's `await queue.put`) prevents other activity from proceeding.  
*Mitigation:* After `asyncio.wait(return_when=FIRST_COMPLETED)` returns, `Race` must explicitly cancel all tasks in the `pending` set and await their cancellation before returning:

```python
winner_task = next(iter(done))
for task in pending:
    task.cancel()
await asyncio.gather(*pending, return_exceptions=True)  # await cancellation
# then run cleanup on losing branch contexts
```

The `return_exceptions=True` on the gather prevents a cancelled task's CancelledError from propagating to Race's own caller. This is the standard asyncio task cleanup pattern. Document it as a required pattern in the combinators module.

**W21 — `Parallel` with `asyncio.gather()` default: one buggy branch kills all branches.**  
`asyncio.gather()` without `return_exceptions=True` cancels all other tasks if any one task raises an unhandled exception. In `Parallel(A, B, C)`, if branch B has a bug that raises `RuntimeError`, branches A and C are cancelled mid-execution. Their cleanup_hooks may or may not run (depending on whether the cancellation propagates cleanly through their code). The overall effect is: one oracle bug silently terminates the entire parallel group with no verdict from A or C.  
*Mitigation:* `Parallel` must use `asyncio.gather(*tasks, return_exceptions=True)`. Any result that is an exception (not a `(Verdict, StreamContext)` tuple) is converted to `(Verdict.error(f"unhandled: {exc}"), forked_ctx)` before the reducer sees it. This ensures the reducer always receives verdicts, not raw exceptions. The reducer can then route on `error` verdicts as needed (e.g. `all_pass` would fail if any branch has an error, which is correct). This also means unhandled exceptions in branches are surfaced as error verdicts in logs/recording rather than being silently swallowed or unexpectedly crashing the engine.

**W22 — Background asyncio tasks spawned inside oracle bodies hold stale `ctx` references after fork.**  
Some oracles spawn background tasks (`asyncio.create_task(pump())`) that hold a reference to `ctx` and may mutate it (e.g. `VCMuxSourceOracle`'s pump registers new streams). If the oracle is called, spawns a background task, and then the engine forks `ctx` for a `Parallel` branch — the background task holds a reference to the *pre-fork* ctx, not the branch's copy. Any mutations the background task makes (adding streams, registering hooks) affect the original ctx rather than the branch copy. This violates the branch isolation guarantee.  
*Mitigation:* The rule is: **oracle bodies must not retain a reference to `ctx` beyond their return.** Background tasks spawned inside an oracle must communicate via dedicated asyncio.Queue or Event objects stored in `ctx.streams` or `ctx.metadata` by the oracle *before* returning. The key invariant: **an oracle's background task must not hold a direct reference to the StreamContext dict** — only to the specific Queue/Event objects it needs. This prevents the stale-reference problem: the Queue/Event objects are in `ctx.streams` or `ctx.metadata`, and `ctx.fork()` creates a new dict pointing to the same Queue/Event objects (which is correct — the background task and the forked branch both see the same Queue, as they are operating on the same underlying stream).

Document this as a coding rule in the oracle implementation guide: "Oracles that spawn background tasks must store all cross-task communication objects in `ctx.streams` or `ctx.metadata` before returning. Never capture `ctx` itself in a background task closure."

**W23 — `asyncio.wait_for` cancellation has known bugs before Python 3.12.**  
`Timeout(oracle, t)` wraps the oracle with `asyncio.wait_for(coro, t)`. In Python 3.10 and 3.11, there is a well-documented bug where cancelling the outer task while `asyncio.wait_for` is active can cause the inner task's `CancelledError` to "escape" and be re-raised in the outer context, corrupting the exception chain and potentially causing hangs. The bug was fixed in Python 3.12 (bpo-46707). In a hardware orchestrator where SIGTERM handling (W10) cancels tasks, this bug can cause silent deadlocks during shutdown.  
*Mitigation:* Require Python 3.12+ as the minimum runtime. State this explicitly in `pyproject.toml`: `requires-python = ">=3.12"`. This is not an unusual requirement — Python 3.12 was released October 2023, and pyserial-asyncio-fast already requires it. All target deployment environments (Ubuntu 24.04, macOS 14+) ship Python 3.12 or later by default. Document the version requirement in the README with rationale: the asyncio.wait_for cancellation fix is a correctness requirement for the Timeout combinator.

**W24 — Oracle return type `(Verdict, StreamContext)` is ambiguous with mutate-in-place semantics.**  
With the W11 "mutate in place, return same object" model, the oracle signature still returns `StreamContext`. Callers MUST use the returned context (not the input reference) — this is the convention that enables the pure-functional framing in tests. But nothing enforces this. An oracle that returns a *different* ctx object would silently break the chain — the caller uses the returned new object while the original (with all its mutations) is discarded.  
*Mitigation:* Define the oracle protocol with a `Protocol` class in `engine/oracle.py`:

```python
from typing import Protocol

class Oracle(Protocol):
    async def __call__(self, ctx: StreamContext, timeout: float) -> tuple[Verdict, StreamContext]:
        ...
```

The engine asserts at each oracle call site: `assert result_ctx is ctx, "oracle must return the same StreamContext object"`. This assertion (disabled by `python -O` or at production) catches the most common mistake — returning a different object. Oracles that legitimately need to return a different context must do so via an explicit documented exception to this rule. In the `fork()` model, oracles within a branch always hold the branch's ctx and must return it.

**W16 — Parallel branch "merge on join" contradicts "branches reference disjoint streams."**  
W1's mitigation says "union of newly created streams across branches (new streams from any branch are visible after join)." But W11's mitigation (fork semantics) says branches each get their own forked context. These two interact: if branch A creates a new stream `vm0_console` and branch B creates a new stream `vm1_console`, the merge collects both. But if branch A *also* registers a cleanup_hook for its `vm0_console`, and the merge includes `vm0_console` in the joined context, then the cleanup_hook that was registered in branch A's local context must also be merged into the parent context — otherwise `vm0_console` will be leaked when the parent context is eventually cleaned up.  
W1's mitigation says `cleanup_hooks` are on branch contexts only; but branch contexts are discarded after join. The cleanup_hooks for resources that ARE merged into the parent must migrate to the parent's `cleanup_hooks`. Resources for branches that are NOT merged (Parallel reducer rejects a branch) must be cleaned up immediately.  
*Mitigation:* `ctx.fork()` creates a child context. At join, `ctx.merge(child_ctx, include_streams: set[str])` does: (a) copies all streams named in `include_streams` from `child_ctx` into the parent; (b) migrates the `cleanup_hooks` for those streams into the parent's `cleanup_hooks`; (c) calls `child_ctx.cleanup_hooks_for(exclude_streams)` to clean up any resources in the child context that were NOT included. The Parallel combinator decides `include_streams` via its reducer (e.g. `all_pass` includes all streams from all branches; a custom reducer may exclude losing branches). This requires `cleanup_hooks` to be associated with named streams (not just a flat list), so the engine knows which hooks correspond to which streams. Alternatively: each Source oracle registers its cleanup_hook as `(stream_name, hook_fn)` tuples, and `ctx.cleanup_for(stream_name)` runs only the hooks for that stream.

*Secondary problem:* This changes the `cleanup_hooks` data structure from `list[Callable]` to `list[tuple[str | None, Callable]]` — hooks associated with a stream name or `None` for hooks not tied to a specific stream. The API becomes `ctx.cleanup(stream_name: str | None = None)` where `None` cleans all hooks. This is a more complex API but is required for correct Parallel join semantics. All oracle implementations that register cleanup_hooks must be updated to pass the associated stream name.

**W17 — The recorder is a module stub with no design.**  
`recorder.py` is listed as `ChainRecorder (extracted from chain_runtime.py)` but no design is given: what events it records, what format, how it is wired into oracle execution, whether it runs as a hook, a wrapper combinator, or a side-channel. In the current system, recording is deeply entangled with chain execution. Without designing the recorder before implementing the engine, recording will be retrofitted as a side-channel and will miss events or duplicate the engine's state.  
*Mitigation:* Define `ChainRecorder` as an event sink with a fixed event vocabulary, wired in via the engine's execution hooks rather than ad-hoc. The event vocabulary:

```python
@dataclass
class OracleStarted:
    oracle_type: str; stream_name: str | None; timestamp: float

@dataclass
class OracleVerdict:
    oracle_type: str; verdict: Verdict; elapsed: float

@dataclass
class StreamBytesRead:
    stream_name: str; byte_count: int; offset: int  # for cursor tracking

@dataclass
class CleanupHookRan:
    stream_name: str | None; hook_name: str
```

The engine calls `recorder.emit(event)` at fixed points: before calling each oracle, after receiving a verdict, and after each cleanup_hook runs. The recorder writes events to `results/<run_id>/events.jsonl`. The `recorder` is passed to the engine at chain start and threaded via the StreamContext metadata (not as a global), so unit tests can pass a `NullRecorder` or `ListRecorder` for assertions. This design also provides the event sequence for the W4 regression baselines — the golden files are `events.jsonl` from real hardware runs, replayed against mock StreamContexts.

**W18 — Daemon/client IPC protocol is undefined (see W15), but a second gap: how does `mcp_server.py` retrieve run results and logs?**  
The MCP server currently provides tools like `check_test` and `get_logs`. In the rewrite, results are written to `results/<run_id>/` as artifacts. But the MCP server needs to know the `run_id` for the most recent chain, and needs to read structured verdict data (for `check_test`) and log data (for `get_logs`). The plan says "the MCP server or CI consumer retrieves artifacts after the chain completes" but gives no mechanism.  
*Mitigation:* The daemon maintains a small in-memory registry of recent runs: `{run_id: RunRecord(chain_id, status, verdict, artifact_dir)}`. The MCP server (running in the same event loop as the daemon) reads this registry directly — no IPC needed since they share the same process. MCP tools:
- `submit_chain(chain_id, overrides)` → `run_id` (submits to daemon queue, returns immediately)
- `check_test(run_id)` → reads `results/<run_id>/robot/verdict.json` or equivalent artifact
- `get_logs(run_id, stream_name)` → reads the recorder's `events.jsonl` filtered to a stream
- `list_runs()` → returns the in-memory registry as a list

The run registry must be bounded (keep last N runs) to prevent unbounded memory growth. A limit of 100 runs is sufficient for a two-author system.

---

## Tactical Improvements (Pre-Rewrite, Low Risk)

### T1 — Centralize `DEFAULT_BAUD = 115200`
Move from `console_sessions.py` to `config.py`. Import in `chain_runtime.py` and `BootHarness.py`. Currently hardcoded 6 times.

### T2 — Deduplicate Filter Scripts
6 filter scripts share identical structure (stdin loop, ANSI strip, marker-based filtering). Extract `filter_common.py` with `filter_from_marker(marker: bytes)`. Each filter becomes ~5 lines.

Affected: `filter_capdl_start.py`, `filter_vm_console.py`, `filter_kernel_start.py`, `filter_mb1_start.py`, `filter_sel4_start.py`, `filter_hyp_output.py`

### T3 — Named Constants in `filter_smmu_faults.py`
```python
GFSR_MULTI_FAULT_BIT    = 0x80000000
GFSR_EXT_ABORT_BIT      = 0x00000002
GFSYNR1_STREAM_ID_MASK  = 0xFFFF
```

### T4 — Remove Unused Imports
- `sel4_client.py` — `import os` unused
- `BootHarness.py` — `import BoardControl` unused (refactor artifact)

### T5 — Translate Finnish Comments to English
- `BootHarness.py:164–167`
- `filter_hyp_output.py:15`

### T6 — Add `requirements.txt`
Four undeclared external packages: `pyserial`, `pexpect`, `usbrelay-py`, `lz4`.

---

## Verification

**Tactical changes:** `python -m py_compile <file>` + pipe sample `.raw` logs through modified filter scripts, diff against known-good output.

**Rewrite order (incremental — hardware validation as early as possible):**
0. **Pre-rewrite baseline (W4 mitigation):** On real hardware, run the 5–6 most complex chains and record their verdict sequences and key match events (which pattern matched on which stream, producing which verdict). Keep raw bytes as debug artifacts but do not use them as the regression target — raw UART output is not reproducible across boots. Do this before touching any code.
1. Design new chain JSON schema (with `schema_version: 1`) and StreamContext merge semantics; document all oracle combinator types and parameters including `Timeout` relationship, `Repeat` stop conditions, and `cleanup_hooks` protocol
2. Read `chain_runtime.py` implementation of `setup_demo` and `check_verdict` to finalise Verdict oracle class before coding begins
3. Implement core engine: `BiStream`, `StreamContext` (with `cleanup_hooks`), oracle base type, combinators (`Sequence`, `Choice`, `Race`, `Parallel`, `Timeout`, `Repeat`) — with asyncio throughout. Set up structlog with `contextvars` context binding (`clear_contextvars()` + `bind_contextvars()` at each chain entry) from the start.
4. Build oracle unit test suite with mock StreamContexts using pytest-asyncio (`asyncio_mode = "auto"`): feed byte sequences, assert `(verdict, updated_context)` — no hardware needed
5. **Adapters first (W3 mitigation):** Implement `uart.py`, `ssh.py`, `process.py` as thin wrappers over new `BiStream`/`StreamContext` types. Keep old chain engine temporarily. Run against real hardware to validate the transport layer before touching the engine.
6. Implement remaining adapters (`docker.py`, `vcmux.py`, `interactive.py`); verify each against oracle unit tests. Design and implement `ConsoleBridge` and session registry before `interactive.py`.
7. Migrate existing chains to new schema, simplest first (`boot_stock_linux`, `wait_for_elfloader`) then complex (`vm-qemu-virtio`); each migrated chain is a behavioural regression test against the golden files from step 0
8. Swap old engine for new engine; live hardware smoke test: Orin AGX → seL4test chain → pass verdict
9. Add signal handling to `daemon.py`: SIGINT/SIGTERM cancel oracle tasks, `cleanup_hooks` release hardware (relay, serial fd, SSH). Hardware-critical cleanup runs synchronously in a `try/finally` outside the event loop.
10. Migrate daemon, client, MCP server last (thin wrappers over the new engine)
