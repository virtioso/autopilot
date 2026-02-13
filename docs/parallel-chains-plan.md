# Parallel Chains Plan

Date: 2026-02-13
Status: Implemented (Phase 1)
Scope: `/home/hlyytine/autopilot`

## Goal

Define and implement a structured parallel-chain execution model that replaces ad-hoc fail-unaware `fork` usage for critical monitoring paths (notably ftrace overflow detection after ELF-loader start).

## Problem Statement

Current `fork` behavior is fire-and-forget:
- parent chain does not observe fork terminal state in real time,
- `join` does not route based on fork result,
- a monitor-style branch cannot force immediate failure of the main chain.

This makes it unsuitable for fail-fast watchdog scenarios such as `FTRACE: Storage full` detection while VM boot flow continues.

## Proposed Model

Add explicit parallel execution semantics via two pseudosteps:

1. `parallel_split`
- starts N branch chains concurrently under a named group,
- each branch gets its own cancellation token,
- branch statuses are tracked in group state,
- execution continues to normal steps in each branch.

2. `parallel_join`
- consumes the first terminal outcome (`pass` or `fail`) produced by any branch,
- immediately cancels remaining branches,
- routes according to join outcomes (`pass` / `fail`).

First terminal result wins (latched).

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
2. `parallel_join` returns:
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
1. New step types: `parallel_split`, `parallel_join`.
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
- Added `parallel_split` / `parallel_join` semantics to `docs/chain-spec.md`.
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

## Out of Scope (This Plan Draft)

- Migrating all existing fork-based flows to parallel groups in one change.
- Broad unrelated chain cleanup outside tracing-driven workflows.
