# Autopilot Rewrite: Progress Tracker

> Update this document at the start and end of every session.  
> For session continuity and quick onboarding, see `context-recovery.md`.

## Current Step

**Step 7 — Chain migration: infrastructure complete, hardware chains pending**  
Worktree: `~/autopilot-rewrite/` on orphan branch `rewrite`  
Chain schema (Pydantic discriminated union, 19 oracle types), OracleFactory (hydrate), engine/runtime.py, and 3 migrated chains are implemented. 100/100 tests pass. Remaining: migrate chains that require hardware (seL4test full flow, boot_stock_linux, vm chains). Needs step 0 baselines first.

---

## Step Status

| # | Step | Status | Notes |
|---|---|---|---|
| 0 | Pre-rewrite baselines on real hardware | BLOCKED | Needs hardware; do before chain migration (step 7) |
| 1 | Chain JSON schema design + StreamContext merge semantics | COMPLETE | Documented in index.md (W1–W29 mitigations) |
| 2 | Read `setup_demo` / `check_verdict` — finalise Verdict oracle | COMPLETE | See findings below |
| 3 | Core engine: BiStream, StreamContext, Verdict, combinators | COMPLETE | `engine/oracle.py`, `engine/combinators.py`, `engine/recorder.py` — 34/34 tests pass |
| 4 | Oracle unit test suite (mock StreamContexts, pytest-asyncio) | COMPLETE | `tests/test_engine.py` — built alongside step 3; all combinators covered |
| 5 | Adapters: `uart.py`, `ssh.py`, `process.py` (+ `engine/primitives.py`) | SW DONE / HW PENDING | 50/50 software tests pass; 4 hardware tests skip-marked (UART, SSH to target) |
| 6 | Remaining adapters: `docker.py`, `vcmux.py`, `robot.py`, `interactive.py` | SW DONE | 73/73 tests pass (6 skip: 4 HW, 2 RF not installed) |
| 7 | Chain migration (simple → complex) | IN PROGRESS | Schema+runtime+factory complete; 3 chains migrated; full HW migration needs step 0 baselines |
| 8 | Swap engine; hardware smoke test (Orin AGX → seL4test) | NOT STARTED | |
| 9 | Signal handling: SIGINT/SIGTERM → cancel → cleanup | NOT STARTED | |
| 10 | Migrate daemon, client, MCP server | NOT STARTED | |

---

## Step 2 Findings (W6 Resolution)

Read `chain_runtime.py` `_step_setup_demo`, `_step_check_verdict`, `_step_set_test_verdict`:

**`setup_demo`** — Compound step: (1) calls `map_vcmux_source` (VCMux BiStream creation); (2) splits tmux windows and runs `tail -F` on vcmuxer log files for display. In the rewrite: VCMux source creation = `VCMuxSourceOracle` with display as a side-effect via `pipe-pane -I` tee. The `tail -F` mechanism is replaced. No special oracle class needed for setup_demo — it decomposes into existing oracle types.

**`check_verdict`** — Two sub-modes:
- `artifact_grep`: SSH to target, poll `grep -q <pattern> <file>` with 2s retry. Maps to `Poll(SSHCommandOracle(...))`.
- `tmux_capture`: Poll `tmux capture-pane` output for a regex. **Wrong model for rewrite** — tmux capture is lossy (ANSI-rendered). Replace with Pattern oracle on the BiStream directly.
- Default: "fail". Handle with a `VerdictOracle(label="fail")`.

**`set_test_verdict`** — Trivial: write verdict to `ctx["test_verdict"]` and call `recorder.set_test_verdict()`. Maps to `VerdictOracle`: immediate `Matched(label)` verdict + recorder side-effect. The Verdict oracle class stays simple.

**Conclusion (W6 resolved):** No hidden complexity blocks the Verdict oracle class design. `setup_demo` decomposes naturally; `check_verdict.tmux_capture` is deprecated in favour of stream-native pattern matching.

---

## Completed Steps Detail

### Step 1 — Schema and architecture design
All documented in `~/autopilot/docs/rewrite/index.md`:
- Oracle combinator model (W1–W29, all mitigated)
- StreamContext: copy-on-fork, mutate-in-place, cleanup_hooks as `(stream_name|None, Callable)` tuples
- Combinators: Sequence, Choice, Race, Timeout, Parallel, Repeat.monitor/Repeat.poll
- Verdict: frozen dataclasses Matched/TimeoutVerdict/Error + structural pattern matching
- Module structure, library selections, version control strategy (orphan branch + worktree)
- Prior art studies in `prior-art-*.md`

---

## Tactical Improvements (Pre-rewrite, can do anytime)

| # | Task | Status |
|---|---|---|
| T1 | Centralize `DEFAULT_BAUD = 115200` in config.py | NOT STARTED |
| T2 | Deduplicate 6 filter scripts → filter_common.py | NOT STARTED |
| T3 | Named constants in filter_smmu_faults.py | NOT STARTED |
| T4 | Remove unused imports (sel4_client.py, BootHarness.py) | NOT STARTED |
| T5 | Translate Finnish comments to English | NOT STARTED |
| T6 | Add requirements.txt | NOT STARTED |
