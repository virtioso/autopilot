# Autopilot Rewrite: Progress Tracker

> Update this document at the start and end of every session.  
> For session continuity and quick onboarding, see `context-recovery.md`.

## Current Step

**Step 13 — Hardware smoke test: COMPLETE (chain validated; sel4test binary has unrelated bug)**  
Worktree: `~/autopilot-rewrite/` on orphan branch `rewrite`  
230/230 tests pass (6 hardware/RF skipped).

All 33 production chains migrated (Tier 0–3). Tier 4 deferred: `prepare_next_run_task.json` (needs `signal_wait`) and `isengard-linux-orin-zenoh-demo.json` (needs Zenoh-specific oracles).

**Hardware validation (2026-05-24):** `sel4test.json` ran end-to-end on Orin AGX. Chain correctly:
- Opened UART, cycled relay, navigated UEFI shell to stock Linux boot
- SSH-polled with retry until Linux network was up (60×2s, `retry_errors=True`)
- Uploaded rootfs.ext4 + test.efi via SFTP, ran `sync`, power-cycled board
- Navigated UEFI shell, launched test.efi, matched "ELF-loader started on CPU"

Verdict was `TimeoutVerdict` — not a chain bug. sel4test binary hit a `vm fault on data` during BIND0001 and stalled. The binary was freshly built with a stub fix for missing `orin_proof_*` symbols (commit `4079715` in `projects/sel4test`). The fault is in the test suite itself, not in chain infrastructure.

**New features added this session:**
- `RecordedOracle` — emits `OracleStarted`/`OracleVerdict` events for every oracle node
- `run.log` — per-run structured log file (structlog → stdlib logging → FileHandler)
- `TeeStream` + `ctx.add_stream()` — universal raw stream capture to `results/<run_id>/streams/<name>.raw`
- `repeat_poll` `retry_errors` flag — retries on Error verdicts (SSH polling during board boot)
- `sync` step in `deploy_and_boot_test_efi.json` — flushes page cache before relay power-cut
- `skip_menu: true` in `uefi_shell_run` — bypasses UEFI menu navigation on current Orin AGX
- SSH upload symlink fix: `.resolve()` on src path before SFTP put
- Relay fix: call `usbrelay_py.board_count()` before `board_details()` to init HID library

**Rewrite is feature-complete and hardware-validated.** Tier 4 chains remain deferred. Next step is production cutover: swap the `autopilot.py` entrypoint and update `PATH`.

Hardware smoke test command (run on Orin AGX with board connected):
```bash
cd ~/autopilot-rewrite
# Set required env vars first (from platform YAML defaults):
export AUTOPILOT_TTY0=/dev/ttyACM0
export AUTOPILOT_TARGET_IP=192.168.1.100
# For sel4test also set:
export AUTOPILOT_ROOTFS_PATH=/path/to/rootfs.ext4
export AUTOPILOT_LOCAL_EFI_PATH=/path/to/test.efi
export AUTOPILOT_REMOTE_EFI_PATH=/efiboot/test.efi
export AUTOPILOT_EFI_BINARY='efiboot\test.efi'

python autopilot.py run --chain chains/sel4test.json --platform orin-agx --timeout 1200
```

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
| 8 | Swap engine; hardware smoke test (Orin AGX → seL4test) | SW DONE / HW PENDING | ChainRunner, Config, CLI+signal handling built; 188 tests pass; `python autopilot.py run --chain chains/sel4test.json --platform orin-agx` |
| 9 | Signal handling: SIGINT/SIGTERM → cancel → cleanup | COMPLETE | Integrated in step 8: CLI wires SIGINT/SIGTERM → task.cancel(); cleanup tested |
| 10 | Migrate daemon, client, MCP server | COMPLETE | daemon.py + client.py + mcp_server.py + 40 tests; was 161 tests |
| 11 | FilterBiStream + new oracle types + chain migration (Tier 1+2) | COMPLETE | 216 tests pass; relay/uefi_shell_run/extlinux_boot implemented; 21 chains migrated; test_chains_mock.py added |
| 12 | Chain migration (Tier 3): vm_common_setup split, vm_common, vm-minimal, vm-qemu-virtio, linux-kernel*, qemu_x86_64_vm_* | COMPLETE | 230 tests pass; 33 chains total; CommandOracle write_stream added |
| 13 | Hardware smoke test: sel4test.json on Orin AGX | COMPLETE | Chain infrastructure validated end-to-end; sel4test binary has unrelated BIND0001 vm fault bug (not a chain issue) |

---

## Step 2 Findings (W6 Resolution)

Read `chain_runtime.py` `_step_setup_demo`, `_step_check_verdict`, `_step_set_test_verdict`:

**`setup_demo`** — Compound step: (1) calls `map_virtioso_mux_source` (VCMux BiStream creation); (2) splits tmux windows and runs `tail -F` on virtioso-mux log files for display. In the rewrite: VCMux source creation = `VCMuxSourceOracle` with display as a side-effect via `pipe-pane -I` tee. The `tail -F` mechanism is replaced. No special oracle class needed for setup_demo — it decomposes into existing oracle types.

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
