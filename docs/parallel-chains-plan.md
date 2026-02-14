# Parallel Chains Plan

Date: 2026-02-13
Status: Implemented (Phase 1)
Scope: `/home/hlyytine/autopilot`

## Goal

Define and implement a structured parallel-chain execution model that replaces ad-hoc fail-unaware `fork` usage for critical monitoring paths (notably ftrace overflow detection after ELF-loader start).

Terminology:
- canonical step names are `split` and `join`,
- no compatibility aliases are maintained.

## Problem Statement

Current `fork` behavior is fire-and-forget:
- parent chain does not observe fork terminal state in real time,
- `join` does not route based on fork result,
- a monitor-style branch cannot force immediate failure of the main chain.

This makes it unsuitable for fail-fast watchdog scenarios such as `FTRACE: Storage full` detection while VM boot flow continues.

## Proposed Model

Add explicit parallel execution semantics via two pseudosteps:

1. `split`
- starts N branch chains concurrently under a named group,
- each branch gets its own cancellation token,
- branch statuses are tracked in group state,
- execution continues to normal steps in each branch.

2. `join`
- consumes the first terminal outcome (`pass` or `fail`) produced by any branch,
- immediately cancels remaining branches,
- routes according to join outcomes (`pass` / `fail`).

First terminal result wins (latched).

### Canonical Step Schema (Implementation Contract)

`split` example:

```json
{
  "type": "split",
  "group": "vm_boot_and_ftrace_watch",
  "branches": [
    { "name": "main_vm_boot", "chain": "vm_wait_boot_qemu_virtio" },
    { "name": "ftrace_watchdog", "chain": "monitor_ftrace_storage_full", "monitor": true }
  ],
  "outcomes": [
    { "label": "ok", "next": "join_vm_boot" }
  ],
  "on_timeout": "fail"
}
```

`join` example:

```json
{
  "type": "join",
  "group": "vm_boot_and_ftrace_watch",
  "timeout_s": 300,
  "outcomes": [
    { "label": "pass", "next": "filter_logs_pass" },
    { "label": "fail", "next": "filter_logs_fail" }
  ],
  "on_timeout": "filter_logs_fail"
}
```

## Monitor Branch Policy (Mandatory)

Monitor branches are fail-only by design.

Rules:
1. Monitor branches must never contain terminal `pass` steps.
2. Monitor branches should loop/watch and terminate only on `fail` condition.
3. Main functional branch is the only source of normal `pass` for the group.
4. Runtime validation must reject monitor branch declarations that can reach `pass`.

## Initial Use Case: Orin VM ftrace overflow

After `ELF-loader started on CPU` is observed on `tty0`:
1. Start a parallel group with:
- main VM progression branch,
- ftrace monitor branch (`pattern: FTRACE: Storage full`, fail-only).
2. `join` returns:
- `fail` immediately if monitor fails first,
- `pass` if main branch reaches pass first and monitor has not failed.

## Chain Naming and Timing Cleanup

In `vm_common`:
1. Rename `wait_capdl` -> `elfloader_started`.
2. Success signal: `ELF-loader started on CPU` on `tty0`.
3. Netboot tolerance: timeout 180s for this stage.

## Runtime Changes (Planned)

Files:
- `chain_runtime.py`
- `docs/chain-spec.md`
- `docs/runbook.md`
- `docs/overview.md`
- `docs/architecture.md`

Runtime additions:
1. New step types: `split`, `join`.
2. Group runtime state in context (`parallel_groups`).
3. Winner latching with timestamp + branch name.
4. Branch cancellation propagation.
5. Recorder extensions in `chain.json`:
- group id,
- branch statuses,
- winner branch,
- winner result,
- winner timestamp.
6. Validation rules for monitor branches (fail-only).

## DRY and SSOT Check (Current)

Checked against:
- `AGENTS.md`
- `docs/README.md`
- `docs/overview.md`
- `docs/runbook.md`
- `docs/chain-spec.md`
- `chain_runtime.py`
- `chains/*.json`

### SSOT/DRY Result

1. Chain semantics drift: resolved in this phase.
- Added `split` / `join` semantics to `docs/chain-spec.md`.
- Updated consumer docs (`overview`, `runbook`, `architecture`, `README`) and `AGENTS.md`.

2. Outcome semantics duplication risk: reduced.
- `docs/chain-spec.md` now defines canonical parallel semantics and monitor fail-only policy.
- Other docs now reference these semantics at operational level.

3. Existing chains duplicated for similar fork-recovery patterns (open follow-up)
- multiple profiles repeat `fork_recovery_pass` / `fork_recovery_fail` blocks.
- optional follow-up refactor: extract common recovery tail chain to reduce duplication.

## SSOT Update Plan

1. `docs/chain-spec.md` is canonical for:
- parallel step schema,
- winner semantics,
- monitor fail-only policy.

2. `docs/runbook.md`, `docs/overview.md`, `docs/architecture.md`, and `AGENTS.md`:
- reference chain-spec for behavioral rules,
- keep only operational guidance.

3. Add/update changelog entries in `docs/dry-ssot-tracking.md` for future drift remediations.

## Validation Plan

1. Static
- JSON schema/chain validation for new steps.
- Python syntax checks.
- grep checks for stale wording claiming only `fork/call_chain` concurrency.

2. Dynamic
- Run `vm-qemu-virtio` with induced/known ftrace overflow path.
- Confirm immediate group fail on monitor trigger.
- Confirm pass path when monitor does not fail.
- Confirm `chain.json` includes parallel winner metadata.
3. DRY/SSOT gate cadence
- run DRY/SSOT checks before and after every migration step,
- block progression when any post-step DRY/SSOT check fails.

## Out of Scope (This Plan Draft)

- Migrating all existing fork-based flows to parallel groups in one change.
- Broad unrelated chain cleanup outside tracing-driven workflows.

## End-to-End Migration Plan

This section defines the concrete migration sequence across runtime, chains,
MCP surfaces, and downstream docs/tooling.

### Mandatory DRY/SSOT Gates (Per Step)

For every migration step in every phase, execute two gates:
1. Pre-step DRY/SSOT gate:
- identify canonical source of truth for the behavior being changed,
- identify all derived/duplicate locations that must remain aligned,
- record existing drift and planned correction scope.
2. Post-step DRY/SSOT gate:
- verify canonical source was updated and remains authoritative,
- verify all derived docs/runbooks/tooling are synchronized,
- verify no contradictory wording/semantics remain,
- record evidence (commands/findings/paths) in the step log.

A migration step is not complete until both gates pass.

### Mandatory Repo Hygiene and Commit Gates (Per Step)

For every migration step:
1. Pre-step repo hygiene gate (all affected repos):
- verify clean git state (`no staged`, `no unstaged`, `no untracked`),
- if any affected repo is dirty, stop and ask human for explicit instruction per repo
  (`commit`, `stash`, or `discard`),
- do not start implementation for the step until all affected repos are clean.
2. Post-step commit gate:
- commit all code/doc changes produced by the step,
- if the step contains multiple logical changes, create multiple atomic commits
  grouped by related purpose,
- do not carry uncommitted or unrelated changes into the next migration step.

A migration step is not complete until repo hygiene and commit gates also pass.

### Phase 0: Preflight and Baseline

1. Confirm target trees and owners:
- `~/autopilot` (`chain_runtime.py`, `chains/*.json`, `docs/*`, MCP server code)
- `~/tii-sel4/projects/virtioso-camkes-vm` (`AGENTS.md`, `docs/agents/*`, tooling)
2. Capture baseline evidence from current behavior:
- one normal pass run,
- one fail-fast monitor-triggered run,
- preserve `results/<id>/chain.json` artifacts.
3. Immediate cutover policy:
- no compatibility window,
- coordinated/winner-based behavior must use `split/join` only,
- legacy `parallel_split`/`parallel_join` naming and compatibility paths are removed.
4. Create a migration step log that captures:
- pre-step DRY/SSOT gate result,
- post-step DRY/SSOT gate result,
- evidence references.

Acceptance criteria:
- `docs/chain-spec.md` is updated first and is the canonical source for `split/join`.
- no stale `parallel_split`/`parallel_join` references remain in current-policy docs:
  `docs/README.md`, `docs/overview.md`, `docs/runbook.md`, `docs/architecture.md`, `AGENTS.md` (if applicable).
- baseline pass/fail run artifacts and step log template are committed.
- all affected repos are clean before phase work starts.

### Phase 1: Runtime Canonicalization (`~/autopilot`)

1. Remove `parallel_split`/`parallel_join` compatibility handling.
2. Make coordinated parallel control use `split`/`join` only.
3. Enforce validation rules:
- `join` must reference a valid active group.
- monitor branches must be fail-only (must not reach terminal `pass`).
4. Extend recorder output in `chain.json` with stable group metadata:
- group id,
- branch status map,
- winner branch/result/timestamp,
- cancellation reason where applicable.
5. Add/extend runtime tests for:
- first-terminal-wins latching,
- branch cancellation propagation,
- deterministic winner reporting.

Acceptance criteria:
- runtime accepts `split/join` and rejects legacy parallel step names.
- validation rejects monitor branches that can reach terminal `pass`.
- recorder writes complete winner metadata in `chain.json` for split/join groups.
- automated tests cover winner-latch and branch-cancel behavior.
- all phase-generated changes are committed in atomic logical commits; no carried uncommitted changes remain.

### Phase 2: Chain Migration (`~/autopilot/chains`)

1. Inventory all `fork`/`join` usage.
2. Classify each usage:
- side-task fire-and-forget: keep as `fork`,
- parent-outcome-dependent parallel logic: migrate to `split/join`.
3. Migrate critical chains first (including `vm_common` / `vm-qemu-virtio` paths).
4. Enforce hard migration:
- no merged chains may retain `parallel_split`/`parallel_join` names,
- no merged chains may depend on compatibility alias behavior.
5. Preserve behavior with incremental PRs:
- one chain family at a time,
- validated by dynamic run evidence.
6. Keep rollback simple:
- retain pre-migration chain snapshots/commits,
- avoid multi-chain bulk rewrites in single change.

Acceptance criteria:
- all migrated chains use `split/join` for coordinated parallel behavior.
- no migrated chain contains `parallel_split`/`parallel_join`.
- dynamic runs verify both pass-first and fail-first outcomes for critical chains.
- all affected repos pass pre-step cleanliness checks for each migration step and finish clean after commits.

### Phase 3: MCP Server and API Surfaces

1. Expose parallel winner metadata in status APIs/tools:
- group status,
- winner branch/result/time,
- cancellation of non-winner branches.
2. Remove compatibility paths and require consumers to use canonical `split/join`-era fields.
3. Add integration validation:
- monitor-triggered fail-first run must surface winner=`fail` at MCP level.

Acceptance criteria:
- MCP status outputs include group status, winner branch/result/timestamp, canceled branches.
- MCP consumers used in runbooks can read canonical fields without compatibility shims.
- integration test confirms winner metadata is visible end-to-end.
- step outputs are committed in one or more logically grouped commits with no leftover working-tree noise.

### Phase 4: Docs, Runbooks, and AGENTS

1. `~/autopilot/docs/chain-spec.md` stays canonical for semantics.
2. Update `~/autopilot/docs/{README,overview,runbook,architecture}.md`:
- remove coordinated-monitor guidance based on `fork`,
- reference canonical `split/join` semantics.
3. Update `~/tii-sel4/projects/virtioso-camkes-vm/AGENTS.md`:
- explicit policy: use `split/join` for coordinated parallel control,
- `fork` only for non-blocking side tasks.
4. Update `~/tii-sel4/projects/virtioso-camkes-vm/docs/agents/*` runbooks:
- examples, expected outcomes, and troubleshooting aligned to winner metadata.

Acceptance criteria:
- terminology is consistent (`split/join`) across all updated docs.
- runbook examples and troubleshooting steps match current runtime behavior.
- no contradictory guidance remains for coordinated `fork` usage.
- affected repos are clean at step start and clean at step end after commits.

### Phase 5: `~/tii-sel4` Side Tooling

1. Update visualization/parsing tools to consume `parallel_groups` metadata.
2. Model `split/join` topology correctly in diagrams:
- `split` must fan out to all configured branch entry paths (not a single edge),
- `join` must fan in from all participating branch terminal paths (not a single edge),
- branch identity/group identity must be visible in node or edge labels.
3. Show winner branch/result and canceled branches by default where applicable.
4. Update tooling to assume canonical `split/join` metadata for current/future runs.
5. Add visualization acceptance checks:
- static fixture for a 2+ branch split/join group must render multi-edge fan-out/fan-in,
- runtime trace fixture must render winner and canceled branches distinctly,
- regression check fails if any split/join group collapses to single-edge representation.

Acceptance criteria:
- graph for each split node has one outgoing edge per configured branch.
- graph for each join node has one incoming edge per participating branch terminal path.
- winner/canceled branch states are visually distinguishable in trace output.
- tooling/doc changes from each migration step are fully committed in logical units.

### Phase 6: Validation, Rollout, and Deprecation

1. Static checks:
- schema/validation/tests pass,
- no stale docs claiming `fork` is sufficient for fail-fast coordinated control.
2. Dynamic checks:
- pass path validation,
- monitor fail-fast validation,
- confirm `chain.json` and MCP outputs are consistent.
3. Rollout rule:
- runtime + chain behavior lands before broad docs/consumer updates finalize.
4. Enforcement rule:
- fail validation on `parallel_split`/`parallel_join` usage,
- fail validation on coordinated logic implemented via legacy `fork`.

Acceptance criteria:
- static validation fails on forbidden legacy names/usages.
- dynamic validation reproduces expected pass/fail-fast behavior on target flows.
- DRY/SSOT pre/post gates pass for every completed migration step.
- repo hygiene and post-step commit gates pass for every completed migration step.
