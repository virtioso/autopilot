# Autopilot Rewrite: Oracle Combinator Architecture

## What Autopilot Actually Is

Autopilot is a **chain-based orchestration runtime** for heterogeneous embedded and virtualization testing. It started as an Orin AGX kernel test harness but now orchestrates:

- Physical hardware (Orin AGX with UEFI reset-line handshakes, USB relay power control)
- seL4 hypervisor VMs (vcmuxer/VCMux multiplexing, dynamic VM console discovery)
- QEMU (ARM64/x86-64, router-based console discovery)
- Docker containers (Isengard vehicle-control + autonomy ROS 2 stacks)
- Pub/sub middleware (Zenoh bridge, virtioso-muxd)
- AI-driven interactive debugging (MCP server exposing console sessions to Claude)

The underlying ideas — structured workflow composition, parallel fork/join, async task coordination, signal-based synchronization — are right. The problems are that the flat JSON graph encoding is the wrong shape for expressing oracle composition, and that a single file has accreted all of the implementation.

---

## The Central Architectural Problem

### Autopilot Is Not Quite an Oracle — But It Keeps Becoming One

The system has an unresolved identity: it was designed as a **transport and orchestration layer** but pattern matching on streams has gradually made it a de-facto oracle — it decides whether tests passed by watching for regex patterns.

With Robot Framework (or DTC) in the picture, RF *is* the oracle. Autopilot's role should be to carry RF to the hardware and carry results back — not to re-interpret RF's output via regex.

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
| **Source** | `map_source`, `map_command_source`, `map_vcmux_source` | Add/replace named streams in StreamContext; PTY display attachment is a side effect of stream creation, not a separate step. **Transition atomicity:** the in-process VCMux parser (Option B) ensures no bytes are dropped during the UART→multiplexed-streams transition — the pump loop runs continuously before and after stream registration, buffering bytes per-stream from the moment they arrive. |
| **Upload** | `upload_efi`, `upload_file`, `upload_kernel` | SCP side effect; verdict on completion or checksum match |
| **Process** | `analyze_logs` | Run subprocess; verdict on exit code; no stream consumed |
| **Interactive** | `interactive_console` | Expose named `BiStream` to human/AI; yield on completion signal |
| **Sync** | `signal_wait`, `wait_router_session`, `relay`, `purge_sources` | Synchronization primitives; advance StreamContext state |
| **Meta** | `set_overrides` | Modify StreamContext config (chain aliases, lifecycle); immediate verdict |
| **Verdict** | `check_verdict`, `setup_demo`, `set_test_verdict` | Record test-level outcome as side effect; immediate verdict — collapses the current two-step `set_test_verdict` + terminal pattern |
| **Terminal** | `pass`, `fail` | Propagate verdict to enclosing combinator (Sequence, Parallel, etc.) |

**`map_window` / `map_router_session_panes` disappear** as explicit step types: in the rewrite, attaching a PTY to a tmux pane is a side effect of Source oracle creation (the stream is created with a display target), not a separate oracle. This matches the PTY tee architecture: Python creates the BiStream and simultaneously attaches a display copy to tmux via libtmux.

All oracles are the same type; their verdict semantics differ by intent:
- **Navigation oracles** (`wait_pattern` for "Shell>") — verdict means "we reached this hardware state"
- **Lightweight verdict oracles** (`wait_pattern` for "Tests passed") — verdict means "we saw the pass/fail banner"
- **Proper verdict oracles** (RF oracle, exit code oracle) — verdict carries structured test results

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
| **docker SDK (docker-py 7.1.0)** | Ad-hoc subprocess docker calls | Container lifecycle + log stream tailing for Isengard Docker adapter. Log stream framing uses an 8-byte Docker multiplexing header (stdout/stderr + length). Use docker-py; aiodocker is inactive (last release 12+ months ago). |
| **zenoh-python** | Ad-hoc Zenoh calls in vcmuxer integration | Pub/sub for the Isengard/Docker path only — NOT applicable to the CAmkES/seL4 UART path (seL4 has no networking). For seL4: in-process VCMux parser (see below). |
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

`sel4_client.py`, `sel4_mcp_server.py`, `orin_kernel_autopilot.py`, MCP tools `test_sel4_efi` / `check_sel4_test` — all reflect what the project was. They now serve Docker, Zenoh, QEMU, and Isengard workloads.

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
│   │                      #   Zenoh bridge possible for Isengard path once virtioso-muxd gains publisher
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
