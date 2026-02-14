# Parallel Chains Plan

Date: 2026-02-13
Status: Implemented (Phase 1)
Scope: `/home/hlyytine/autopilot`

## Goal

Define and implement a structured parallel-chain execution model that replaces ad-hoc fail-unaware `fork` usage for critical monitoring paths (notably ftrace overflow detection after ELF-loader start).

Terminology:
- staged migration naming is mandatory:
- use `parallel_split` + `parallel_join` until legacy `fork`/`join` semantics are fully eliminated,
- only then rename to canonical `split` + `join`.

## Problem Statement

Current `fork` behavior is fire-and-forget:
- parent chain does not observe fork terminal state in real time,
- `join` does not route based on fork result,
- a monitor-style branch cannot force immediate failure of the main chain.

This makes it unsuitable for fail-fast watchdog scenarios such as `FTRACE: Storage full` detection while VM boot flow continues.

## Proposed Model

Add explicit parallel execution semantics via two pseudosteps:

1. `parallel_split` (renamed to `split` in final cutover)
- starts N branch chains concurrently under a named group,
- each branch gets its own cancellation token,
- branch statuses are tracked in group state,
- execution continues to normal steps in each branch.

2. `parallel_join` (renamed to `join` in final cutover)
- joins one or more named parallel groups,
- applies an explicit reducer to joined branch results,
- routes according to reducer output (`pass` / `fail`),
- optionally cancels non-completed branches only when reducer policy requires it.

Reducer policy:
- `any_pass`: return `pass` as soon as any joined branch passes; return `fail` if all joined branches fail.
- `all_pass`: return `fail` as soon as any joined branch fails; return `pass` only when all joined branches pass.

### Canonical Step Schema (Implementation Contract)

Current migration-stage schema (`parallel_split` / `parallel_join`) example:

```json
{
  "type": "parallel_split",
  "group": "vm_boot_and_ftrace_watch",
  "branches": [
    { "name": "main_vm_boot", "chain": "vm_wait_boot_qemu_virtio" },
    { "name": "ftrace_watchdog", "chain": "monitor_ftrace_storage_full", "monitor": true }
  ],
  "outcomes": [
    { "label": "ok", "next": "parallel_join_vm_boot" }
  ],
  "on_timeout": "fail"
}
```

```json
{
  "type": "parallel_join",
  "join_groups": ["vm_boot_and_ftrace_watch"],
  "reduce": "any_pass",
  "timeout_s": 300,
  "outcomes": [
    { "label": "pass", "next": "filter_logs_pass" },
    { "label": "fail", "next": "filter_logs_fail" }
  ],
  "on_timeout": "filter_logs_fail"
}
```

Final target schema after legacy `fork`/`join` removal (`split` / `join`) example:

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
1. New coordinated parallel step types: `parallel_split`, `parallel_join` (final rename target: `split`, `join`).
2. Group runtime state in context (`parallel_groups`).
3. Reducer-based join semantics (`any_pass`, `all_pass`).
4. Persistent task/group registry (daemon-level bookkeeping, not request-local only).
5. Signal/event primitives for inter-thread orchestration.
6. Test verdict state separated from workflow terminal state.
7. Recorder extensions in `chain.json`:
- group id,
- branch statuses,
- cancel reason,
- reducer used,
- reducer decision evidence.
8. Validation rules for monitor branches (fail-only) and strict join-target existence.

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
- Confirm `parallel_join reduce=any_pass` and `reduce=all_pass` behavior against fixtures.
- Confirm verdict and workflow state are emitted independently.
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

### Mandatory Obvious-Bug Escalation Gate (Per Step)

For every migration step:
1. Keep active watch for obvious bugs/problems in changed or directly related paths.
2. If a likely bug/problem is suspected, stop the step and ask human for explicit direction:
- fix immediately in-scope,
- drop/skip from current scope,
- add a TODO/follow-up item to the plan,
- or proceed with a human-selected alternative approach.
3. Record the suspected issue and human decision in the migration step log.

A migration step is not complete until suspected obvious bugs/problems are either
resolved in-step or dispositioned by explicit human instruction.

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
- during migration, coordinated/winner-based behavior must use `parallel_split`/`parallel_join`,
- do not introduce new coordinated `fork`/legacy `join` flow dependencies,
- final rename to `split`/`join` occurs only after legacy `fork`/`join` semantics are fully removed.
4. Create a migration step log that captures:
- pre-step DRY/SSOT gate result,
- post-step DRY/SSOT gate result,
- evidence references.

Acceptance criteria:
- `docs/chain-spec.md` is updated first and is the canonical source for migration-stage naming and final rename criteria.
- no contradictory naming guidance remains in current-policy docs:
  `docs/README.md`, `docs/overview.md`, `docs/runbook.md`, `docs/architecture.md`, `AGENTS.md` (if applicable).
- baseline pass/fail run artifacts and step log template are committed.
- all affected repos are clean before phase work starts.

### Phase 1: Runtime Canonicalization (`~/autopilot`)

1. Keep coordinated parallel control on `parallel_split`/`parallel_join` during migration.
2. Ensure legacy fork-join semantics remain non-ambiguous while migration is in progress.
3. Enforce validation rules:
- `parallel_join` must reference only existing/known join targets.
- monitor branches must be fail-only (must not reach terminal `pass`).
4. Add reducer support to `parallel_join`:
- `reduce=any_pass` and `reduce=all_pass`.
5. Add persistent task/group registry with explicit lifecycle:
- `created`, `running`, `pass`, `fail`, `canceled`.
6. Add signal/event primitives:
- wait/set semantics for inter-thread coordination.
7. Introduce explicit test verdict state decoupled from workflow completion.
8. Extend recorder output in `chain.json` with stable group metadata:
- group id,
- branch status map,
- reducer mode and decision result,
- winner branch/result/timestamp when applicable,
- cancellation reason where applicable.
9. Add/extend runtime tests for:
- reducer correctness (`any_pass`, `all_pass`),
- branch cancellation propagation,
- deterministic decision reporting,
- signal wait/set behavior,
- verdict persistence independent of workflow tail steps.

Acceptance criteria:
- runtime accepts and enforces `parallel_split`/`parallel_join` semantics with no ambiguity against legacy `join`.
- runtime rejects missing/unknown join targets.
- validation rejects monitor branches that can reach terminal `pass`.
- recorder writes complete reducer/decision metadata in `chain.json` for parallel groups.
- automated tests cover reducer behavior, branch-cancel behavior, and signal semantics.
- all phase-generated changes are committed in atomic logical commits; no carried uncommitted changes remain.

### Phase 2: Chain Migration (`~/autopilot/chains`)

1. Inventory all `fork`/`join` usage.
2. Classify each usage:
- side-task fire-and-forget: migrate to explicit task-registry/signal-based pattern (no legacy `fork` dependency long-term),
- parent-outcome-dependent parallel logic: migrate to `parallel_split`/`parallel_join` with explicit reducer.
3. Migrate critical chains first (including `vm_common` / `vm-qemu-virtio` paths).
4. Enforce hard migration:
- no merged chains may use coordinated `fork`/legacy `join` where winner-based parallel behavior is required,
- all migrated coordinated flows use `parallel_split`/`parallel_join`.
5. Preserve behavior with incremental PRs:
- one chain family at a time,
- validated by dynamic run evidence.
6. Keep rollback simple:
- retain pre-migration chain snapshots/commits,
- avoid multi-chain bulk rewrites in single change.

Acceptance criteria:
- all migrated chains use `parallel_split`/`parallel_join` for coordinated parallel behavior.
- no coordinated migrated flow depends on legacy `fork`/`join` semantics.
- prep-for-next-run flow is modeled using registry/signal-aware parallel primitives.
- dynamic runs verify both pass-first and fail-first outcomes for critical chains.
- all affected repos pass pre-step cleanliness checks for each migration step and finish clean after commits.

### Phase 3: MCP Server and API Surfaces

1. Expose parallel winner metadata in status APIs/tools:
- group status,
- winner branch/result/time,
- cancellation of non-winner branches.
2. Expose reducer metadata and decision evidence:
- reducer mode,
- joined target list,
- decision reason.
3. Expose test verdict independently from workflow state:
- `test_verdict` (`pass`/`fail`),
- `workflow_state` (`running`/`housekeeping`/`completed`/`failed`).
4. Remove compatibility paths and require consumers to use migration-stage `parallel_split`/`parallel_join` fields (until final rename cutover).
5. Add integration validation:
- monitor-triggered fail-first run must surface winner=`fail` at MCP level.

Acceptance criteria:
- MCP status outputs include group status, winner branch/result/timestamp, canceled branches.
- MCP status outputs include reducer mode and decision reason.
- MCP status outputs include `test_verdict` decoupled from `workflow_state`.
- MCP consumers used in runbooks can read required fields for the current migration stage without compatibility shims.
- integration test confirms winner metadata is visible end-to-end.
- step outputs are committed in one or more logically grouped commits with no leftover working-tree noise.

### Phase 4: Docs, Runbooks, and AGENTS

1. `~/autopilot/docs/chain-spec.md` stays canonical for semantics.
2. Update `~/autopilot/docs/{README,overview,runbook,architecture}.md`:
- remove coordinated-monitor guidance based on `fork`,
- during migration, reference `parallel_split`/`parallel_join` semantics;
- after final rename cutover, update docs to canonical `split`/`join`.
3. Update `~/tii-sel4/projects/virtioso-camkes-vm/AGENTS.md`:
- explicit policy: use `parallel_split`/`parallel_join` for coordinated parallel control during migration;
- after final rename cutover, use `split`/`join`,
- `fork` only for non-blocking side tasks.
4. Update `~/tii-sel4/projects/virtioso-camkes-vm/docs/agents/*` runbooks:
- examples, expected outcomes, and troubleshooting aligned to winner metadata.

Acceptance criteria:
- terminology is consistent for the current migration stage
  (`parallel_split`/`parallel_join` before final rename, `split`/`join` after cutover).
- runbook examples and troubleshooting steps match current runtime behavior.
- no contradictory guidance remains for coordinated `fork` usage.
- affected repos are clean at step start and clean at step end after commits.

### Phase 5: `~/tii-sel4` Side Tooling

1. Update visualization/parsing tools to consume `parallel_groups` metadata.
2. Model coordinated parallel topology correctly in diagrams:
- `parallel_split` must fan out to all configured branch entry paths (not a single edge),
- `parallel_join` must fan in from all participating branch terminal paths (not a single edge),
- branch identity/group identity must be visible in node or edge labels.
3. Show winner branch/result and canceled branches by default where applicable.
4. Update tooling to assume `parallel_split`/`parallel_join` metadata during migration,
   then switch to `split`/`join` after final rename cutover.
5. Add visualization acceptance checks:
- static fixture for a 2+ branch parallel_split/parallel_join group must render multi-edge fan-out/fan-in,
- runtime trace fixture must render winner and canceled branches distinctly,
- regression check fails if any parallel group collapses to single-edge representation.
- add fixtures for reducer modes (`any_pass`, `all_pass`) with explicit decision visualization.

Acceptance criteria:
- graph for each parallel_split node has one outgoing edge per configured branch.
- graph for each parallel_join node has one incoming edge per participating branch terminal path.
- winner/canceled/decision states are visually distinguishable in trace output.
- tooling/doc changes from each migration step are fully committed in logical units.

### Phase 6: Validation, Rollout, and Deprecation

1. Static checks:
- schema/validation/tests pass,
- no stale docs claiming `fork` is sufficient for fail-fast coordinated control.
2. Dynamic checks:
- pass path validation,
- monitor fail-fast validation,
- confirm `chain.json` and MCP outputs are consistent.
 - confirm reducer semantics for `any_pass` and `all_pass`.
 - confirm test verdict remains stable while housekeeping workflow continues.
3. Rollout rule:
- runtime + chain behavior lands before broad docs/consumer updates finalize.
4. Enforcement rule:
- while legacy fork/join exists: fail validation on coordinated logic implemented via legacy `fork`/`join`,
- after legacy fork/join elimination: rename `parallel_split` -> `split` and `parallel_join` -> `join`,
- after rename: fail validation on `parallel_split`/`parallel_join` usage,
- fail validation on coordinated logic implemented via legacy `fork`.

5. Final rename safety verification (mandatory before/after rename):
- inventory all code/docs/chains/tooling/MCP consumers for expected `join` semantics,
- confirm no remaining consumer expects legacy fork-join `join` behavior,
- run end-to-end tests and visualization checks on renamed schema,
- block release if any old-join semantic assumption remains.

Acceptance criteria:
- static validation fails on forbidden legacy names/usages.
- dynamic validation reproduces expected pass/fail-fast behavior on target flows.
- DRY/SSOT pre/post gates pass for every completed migration step.
- repo hygiene and post-step commit gates pass for every completed migration step.
- suspected obvious bugs/problems are explicitly dispositioned with recorded human direction.
- final rename to `split`/`join` is complete and verified safe against legacy `join` semantic expectations.
