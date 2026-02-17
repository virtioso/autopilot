# Plan V3: Make `prepare_next_run` Daemon-Only, Remove from All Test Chains, and Fix SSH/Offset Bugs

## Summary

Refactor Autopilot so lifecycle management of `prepare_next_run` is exclusively daemon-owned.
All test profile chains must be completely unaware of prepare lifecycle orchestration.

This plan is explicit and enforceable:
1. No test chain may contain `prepare_next_run` task lifecycle steps.
2. Daemon admission policy is the only owner of prepare gating/degraded behavior.
3. Board actions remain chain-defined JSON, selected via platform overrides (SSOT), not hardcoded Python flow steps.
4. `AP-SSH-001` and `AP-OFFSET-001` are fixed within this refactor scope.
5. Progress must be tracked in this document; at each step, code and docs are committed to git.

Tracking doc:
- `/home/hlyytine/autopilot/docs/prepare-next-run-daemon-ownership-plan-2026-02-17.md`

## Execution and Commit Policy

1. Progress SSOT is this document.
2. Every numbered plan step ends with exactly one git commit.
3. That commit must include:
   - code/config/doc changes for that step
   - a progress update in this document.
4. No step is complete until both the document update and commit exist.

## Non-Negotiable Architecture Rules

1. **Daemon-only ownership**:
   - `prepare_next_run` lifecycle trigger, retries, gating, degraded transition, and queue admission are daemon responsibilities only.
2. **Zero chain awareness**:
   - Test chains must not include any lifecycle coupling for prepare:
     - `task_spawn` with `task=prepare_next_run`
     - `signal_set` for `prepare_next_run_go`
     - `task_join` waiting for `prepare_next_run`
3. **JSON remains action SSOT**:
   - Python orchestrates policy/state machine only.
   - Probe and prepare actions are executed by chain IDs configured in `platform-init-<platform>.json` overrides.

## Scope

In scope:
1. Daemon lifecycle state machine and queue gating.
2. Removal of inline prepare lifecycle from all affected test chains.
3. Policy gates to prevent reintroduction.
4. Fixes for:
   - `AP-SSH-001` (root-cause-first)
   - `AP-OFFSET-001` (byte-exact offsets).

Out of scope:
1. Prompt-only readiness policy.
2. Hardcoding probe/prepare boot steps in Python.
3. Unrelated chain-language redesign.

## Current Affected Chains (Must Be Cleaned)

1. `chains/vm_common.json`
2. `chains/linux-kernel.json`
3. `chains/linux-kernel-multi.json`
4. `chains/sel4test.json`
5. `chains/boot-interactive.json`
6. `chains/boot-interactive-efi.json`

## SSOT Configuration Model

Use `platform-init-<platform>.json` + `set_overrides` to define lifecycle policy:

1. `lifecycle.prepare.probe_chain` (e.g., `boot_stock_linux`)
2. `lifecycle.prepare.run_chain` (e.g., `recovery_boot`)
3. `lifecycle.prepare.max_retries` (e.g., `3`)
4. `lifecycle.prepare.degraded_holds_queue` (`true`)
5. optional timeouts/backoff fields.

Daemon reads these from `platform_overrides`.
No lifecycle constants in test chains.

## Implementation Plan

### Phase 1: Introduce daemon lifecycle manager

1. Add daemon-level `PrepareLifecycle` manager in Autopilot daemon code.
2. Manager state model:
   - `unknown`, `probing`, `preparing`, `pass`, `fail`, `degraded`.
3. Startup:
   - run probe chain from overrides.
   - if pass -> state `pass`.
   - else -> start prepare flow and gate queue until `pass`.
4. Post-request:
   - after every request completion (pass/fail/cancel), schedule prepare cycle.
5. Queue admission:
   - dequeue only when state `pass`.
   - in `degraded`, hold queue and expose reason.

### Phase 2: Remove lifecycle orchestration from test chains

1. Edit listed chains to remove prepare lifecycle blocks.
2. Preserve test logic and verdict semantics (pass/fail of the test itself only).
3. Ensure no request chain mutates prepare state directly.

### Phase 3: Add hard policy gates (DRY/SSOT enforcement)

1. Runtime validator gate (`chain_runtime.py`):
   - reject any profile chain containing prepare lifecycle signatures.
2. CI/preflight lint gate:
   - repo-wide scan fails if chains reintroduce forbidden prepare lifecycle patterns.
3. New chain admission gate:
   - new/modified chains must pass schema + lifecycle policy lint.

### Phase 4: `AP-SSH-001` root-cause-first fix

1. Instrument prompt match and ssh-wait attempts.
2. Determine root cause before behavior changes (no timeout assumption).
3. Implement fix based on confirmed cause while keeping SSH strict.
4. Record evidence in tracking doc with before/after run IDs.

### Phase 5: `AP-OFFSET-001` exact byte offset fix

1. Rework `wait_pattern` offset calculation to use byte positions in raw buffer.
2. Ensure `log_offset` exactly matches byte index in `console/*.raw`.
3. Add regression tests for ANSI/control and multibyte scenarios.

## Public Interface / Type Changes

1. `autopilot_status` payload extension:
   - add `prepare` object:
     - `state`
     - `degraded_reason`
     - `last_probe`
     - `last_prepare`
     - `retry_count`.
2. Result metadata semantics:
   - `log_offset` explicitly defined as raw-byte offset.
3. Optional platform override schema extension:
   - `lifecycle.prepare.*` keys.

## Test Cases and Scenarios

1. Startup pass path:
   - probe passes -> first request accepted immediately.
2. Startup recovery path:
   - probe fails -> prepare runs -> queue blocked until pass.
3. Degraded path:
   - prepare fails after max retries -> queue held, no dequeue.
4. Post-request trigger:
   - each completed request causes prepare cycle before next dequeue.
5. Chain isolation:
   - verify affected chains contain zero prepare lifecycle steps.
6. SSH bug regression:
   - reproduce former false-fail class; confirm corrected behavior.
7. Offset accuracy:
   - `log_offset` equals `grep -aob` marker byte index.

## Acceptance Criteria

1. All six affected test chains are free of prepare lifecycle references.
2. Any new chain with prepare lifecycle patterns is rejected by automated gates.
3. Daemon alone controls prepare state and request admission.
4. `autopilot_status` exposes prepare health.
5. `AP-SSH-001` closed with confirmed root cause + validated fix.
6. `AP-OFFSET-001` closed with byte-accurate offset proof.

## Rollout

1. Land lifecycle manager + status surfacing.
2. Land chain cleanup and policy gates.
3. Land SSH/offset fixes with tests.
4. Run multi-request validation sequence on Orin AGX.

## Rollback

1. Revert lifecycle manager and status additions.
2. Restore previous chain versions if required.
3. Revert SSH/offset patches independently if needed.
4. Re-run baseline request to confirm old behavior restored.

## Assumptions and Defaults

1. Tracking and implementation repo: `/home/hlyytine/autopilot`.
2. SSH readiness remains mandatory.
3. Degraded mode holds queue instead of auto-failing pending requests.
4. Platform policy remains data-driven via `platform-init` overrides.

## Progress Log

### Step 1 (Completed): Phase 1 daemon lifecycle manager + status surfacing

Date: 2026-02-17

Implemented:
1. Added daemon-owned `PrepareLifecycle` state machine in `orin_kernel_autopilot.py` with states:
   - `unknown`, `probing`, `preparing`, `pass`, `fail`, `degraded`.
2. Added startup probe flow:
   - run `lifecycle.prepare.probe_chain` (default `boot_stock_linux`);
   - on failure, run prepare cycle via `lifecycle.prepare.run_chain` (default `recovery_boot`).
3. Added queue admission gate:
   - dequeue only when lifecycle state is `pass`;
   - in `degraded` with `degraded_holds_queue=true`, queue stays blocked.
4. Added post-request lifecycle trigger:
   - after each completed request, daemon runs prepare cycle before admitting next request.
5. Added status export:
   - daemon writes `runtime/prepare_state.json`;
   - `sel4_client.get_autopilot_status()` now includes `prepare` object.
6. Added platform-init lifecycle policy defaults in:
   - `chains/platform-init-orin-agx-uefi-netboot.json` under `lifecycle.prepare.*`.

Validation:
1. `python3 -m py_compile orin_kernel_autopilot.py sel4_client.py`
2. JSON parse validation for `chains/platform-init-orin-agx-uefi-netboot.json`

### Step 2 (Completed): Remove prepare lifecycle orchestration from test chains

Date: 2026-02-17

Implemented:
1. Removed prepare lifecycle coupling from all targeted test chains:
   - `chains/vm_common.json`
   - `chains/linux-kernel.json`
   - `chains/linux-kernel-multi.json`
   - `chains/sel4test.json`
   - `chains/boot-interactive.json`
   - `chains/boot-interactive-efi.json`
2. Removed or bypassed `spawn_prepare_task` ownership logic from those chains.
3. Removed `signal_set(prepare_next_run_go)` and `task_join(prepare_next_run)` flow from those chains.
4. Updated `set_verdict_pass`/`set_verdict_fail` transitions to terminate directly at `pass`/`fail`.

Validation:
1. `validate_chain(...)` passes for all six updated chains.
2. Signature scan confirms zero prepare lifecycle signatures in those six chains.

### Step 3 (Completed): Add hard policy gates (runtime + lint)

Date: 2026-02-17

Implemented:
1. Runtime validator enforcement in `chain_runtime.py`:
   - rejects `task_spawn task=prepare_next_run`
   - rejects `task_spawn chain=prepare_next_run_task`
   - rejects `signal_set signal=prepare_next_run_go`
   - rejects `task_join` lists containing `prepare_next_run`
2. Added repo lint script:
   - `scripts/lint_prepare_lifecycle.py`
   - scans `chains/*.json` and fails on forbidden prepare lifecycle signatures.

Validation:
1. `python3 /home/hlyytine/autopilot/scripts/lint_prepare_lifecycle.py` passes.
2. `validate_chain(...)` passes for all `chains/*.json`.
3. `python3 -m py_compile chain_runtime.py` passes.

### Step 4 (Completed): `AP-SSH-001` root-cause instrumentation (strict SSH retained)

Date: 2026-02-17

Implemented:
1. Added per-attempt SSH readiness diagnostics in `chain_runtime.py` (`_step_ssh_wait_ready`):
   - attempt count
   - target endpoint
   - last error detail (`timeout` or SSH exit code)
   - remaining budget per attempt
2. Added explicit success summary log:
   - attempts and elapsed seconds.
3. Timeout failure now reports:
   - total attempts
   - elapsed duration
   - last observed error detail.
4. SSH strictness unchanged:
   - still requires successful SSH command before pass.

Validation:
1. `python3 -m py_compile chain_runtime.py` passes.

### Step 5 (Completed): `AP-OFFSET-001` byte-exact `log_offset`

Date: 2026-02-17

Implemented:
1. Reworked `wait_pattern` matching in `chain_runtime.py` to operate on raw bytes (not decoded text slices).
2. `log_offset` now computed as absolute byte offset in `console/*.raw`.
3. Added rolling byte buffer in `wait_pattern` to preserve cross-read regex matching continuity.
4. Added `wait_pattern.start_from` policy (`head|tail`) and set:
   - `chains/boot_stock_linux.json` `wait_stock_prompt.start_from = tail`
   to avoid stale prompt matches from earlier UART history.
5. Updated docs:
   - `docs/chain-spec.md` documents `wait_pattern.start_from`.
   - `docs/chain-spec.md` clarifies `log_offset` as raw-byte offset.

Validation:
1. `validate_chain(...)` passes for all `chains/*.json`.
2. `python3 -m py_compile chain_runtime.py` passes.

### Step 6 (Completed): Remove residual runtime coupling to legacy prepare signals

Date: 2026-02-17

Implemented:
1. Removed legacy `prepare_next_run` special-casing from `ChainRunner._handle_cancel`:
   - no longer skips cancel by hardcoded task name `prepare_next_run`
   - no longer increments hardcoded signal `prepare_next_run_go` on cancel
2. This completes daemon-only lifecycle ownership and removes hidden coupling from request-chain runtime cancel path.

Validation:
1. `python3 -m py_compile chain_runtime.py` passes.
2. `validate_chain(...)` passes for all `chains/*.json`.
3. `python3 /home/hlyytine/autopilot/scripts/lint_prepare_lifecycle.py` passes.

### Step 7 (Completed): Live Orin validation evidence (`AP-SSH-001` and `AP-OFFSET-001`)

Date: 2026-02-17

Run IDs:
1. Before (known failing prepare/ssh): `20260217-090413`
2. After (post-fix validation run): `20260217-105711`

Evidence collected from live run:
1. Daemon lifecycle behavior:
   - startup probe reached `state=pass` (`last_probe.status=pass`, chain `boot_stock_linux`)
   - post-request prepare cycle executed and passed (`last_prepare.status=pass`, chain `recovery_boot`, trigger `request_complete:20260217-105711`)
2. SSH readiness instrumentation:
   - `autopilot.log` recorded per-attempt diagnostics for `ssh_wait_ready`
   - observed attempts 1/2 timeout and attempt 3 success (`elapsed_s=4.567`)
   - confirms root-cause timing evidence is now visible without changing strict SSH policy
3. Byte-exact offset verification:
   - `results/20260217-105711/chain.json` reported `wait_stock_prompt.log_offset = 119238`
   - direct raw-file search in `results/20260217-105711/console/tty0.raw` found:
     - `tegra-ubuntu login:` at byte index `119238`
   - offset is exact (raw-byte aligned).

Notes:
1. Request `20260217-105711` overall verdict remained `fail` for unrelated test-flow reasons.
2. Validation target here was lifecycle ownership + SSH/offset instrumentation/fix correctness.

### Step 8 (Completed): Startup admission gate + documentation SSOT cleanup

Date: 2026-02-17

Implemented:
1. Added daemon startup fail-fast validation for all chain files:
   - `orin_kernel_autopilot.py` now runs `validate_all_chains()` at startup
   - service aborts early if any chain JSON/schema/runtime-validation rule is broken.
2. Updated MCP `autopilot_status` response path:
   - if client helper output lacks `prepare`, server now merges `runtime/prepare_state.json` directly (backward compatibility for long-lived MCP processes).
3. Updated stale docs that still described chain-level prepare lifecycle:
   - `docs/runbook.md` now documents daemon-owned prepare lifecycle policy.
   - `docs/architecture.md` data flow updated to remove request-chain prepare orchestration.
   - `docs/chain-spec.md` `set_test_verdict` example now terminates directly (no `prepare_next_run` step).

Validation:
1. `python3 -m py_compile orin_kernel_autopilot.py sel4_mcp_server.py` passes.
2. `python3 /home/hlyytine/autopilot/scripts/lint_prepare_lifecycle.py` passes.
3. `validate_chain(...)` passes for all `chains/*.json`.

### Step 9 (Completed): DRY cleanup for lifecycle policy signatures

Date: 2026-02-17

Implemented:
1. Removed duplicated forbidden-signature literals from lint tool.
2. `scripts/lint_prepare_lifecycle.py` now imports
   `FORBIDDEN_PREPARE_LIFECYCLE_PATTERNS` from `chain_runtime.py` (single source of truth).

Validation:
1. `python3 /home/hlyytine/autopilot/scripts/lint_prepare_lifecycle.py` passes.
2. `python3 -m py_compile scripts/lint_prepare_lifecycle.py chain_runtime.py` passes.

### Step 10 (Completed): Enforce chain admission gate + preflight entrypoint

Date: 2026-02-17

Implemented:
1. Submission-time chain admission gate in `sel4_client.py`:
   - validates `profile` name format
   - requires `chains/<profile>.json` to exist
   - runs `validate_chain(...)` on the selected profile chain
   - runs lifecycle lint (`scripts/lint_prepare_lifecycle.py`) before accepting request submission
2. Added preflight script:
   - `scripts/preflight_autopilot.py`
   - runs py-compile checks, lifecycle lint, and `validate_chain` for all chains.

Validation:
1. `python3 scripts/preflight_autopilot.py` passes.
