# Parallel Chains Migration Log

Date started: 2026-02-14
Scope: `/home/hlyytine/autopilot` and `/home/hlyytine/tii-sel4/projects/virtioso-camkes-vm`

## Step 2026-02-14-01

Summary:
- Fix runtime deadlock in `parallel_join` state snapshot path.

Pre-step DRY/SSOT gate:
- Canonical behavior source: `chain_runtime.py` and `docs/chain-spec.md`.
- Derived surfaces checked: trace visualization consumer (`tools/autopilot_chain_viz.py`).
- Drift found: lock re-entry bug in runtime implementation.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Obvious-bug escalation:
- Issue: deadlock risk due non-reentrant lock acquisition in `parallel_join`.
- Human direction: fix immediately and commit separately.

Implementation:
- Added unlocked snapshot helper and used it from `parallel_join` locked section.

Post-step DRY/SSOT gate:
- Runtime behavior corrected in canonical source.
- No contradictory docs introduced by this step.

Post-step commit gate:
- Commit: `164ad71`
- Message: `runtime: fix parallel_join lock reentry deadlock`

## Step 2026-02-14-02

Summary:
- Enforce non-ambiguous step semantics between legacy `join` and `parallel_join`.

Pre-step DRY/SSOT gate:
- Canonical behavior source: `chain_runtime.py`.
- Derived surfaces: chain JSON authorship rules in docs.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Validation rejects `parallel_join` with `chain` field.
- Validation rejects legacy `join` with `group` field.

Post-step DRY/SSOT gate:
- Runtime semantics are explicit and non-overlapping.

Post-step commit gate:
- Commit: `b30f91c`
- Message: `runtime: reject ambiguous join/parallel_join fields`

## Step 2026-02-14-03

Summary:
- Fix static visualization of parallel groups to show branch fan-out/fan-in.

Pre-step DRY/SSOT gate:
- Canonical visualization behavior source: `tools/autopilot_chain_viz.py`.
- Drift found: collapsed single-edge rendering for `parallel_split`/`parallel_join`.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Added explicit per-branch nodes for `parallel_split`.
- Rendered one edge per branch from split node.
- Rendered per-branch edges into downstream target(s), including `parallel_join`.

Post-step DRY/SSOT gate:
- Tool output now reflects configured branch topology.

Post-step commit gate:
- Commit: `623a55e`
- Message: `tools: render parallel_split fan-out and parallel_join fan-in`

## Step 2026-02-14-04

Summary:
- Extend runtime `parallel_groups` metadata with branch cancellation reason.

Pre-step DRY/SSOT gate:
- Canonical runtime trace source: `chain_runtime.py`.
- Derived consumer: trace visualization.
- Drift found: no explicit cancellation reason in branch state.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Added `cancel_reason` to recorded branch metadata.
- Set `cancel_reason=winner:<branch>` when non-winner branches are canceled.

Post-step DRY/SSOT gate:
- Canonical trace metadata now distinguishes cancellation from generic failure paths.

Post-step commit gate:
- Commit: `1616f21`
- Message: `runtime: record parallel branch cancel reason metadata`

## Step 2026-02-14-05

Summary:
- Extend trace visualization to show parallel group winner and canceled branches.

Pre-step DRY/SSOT gate:
- Canonical visualization source: `tools/autopilot_chain_viz.py`.
- Canonical runtime metadata source: `chain_runtime.py`.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Added `parallel_groups` trace subgraph rendering.
- Added winner/canceled visual classes and labels using `cancel_reason`.

Post-step DRY/SSOT gate:
- Visualization consumes canonical runtime metadata fields.

Post-step commit gate:
- Commit: `91c4b16`
- Message: `tools: show parallel group winner and canceled branches in traces`

## Step 2026-02-14-06

Summary:
- Document parallel cancellation metadata in canonical docs.

Pre-step DRY/SSOT gate:
- Canonical doc source: `docs/chain-spec.md`.
- Derived docs: `docs/runbook.md`, `docs/overview.md`.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Added `parallel_groups`/`cancel_reason` metadata definitions and runbook notes.

Post-step DRY/SSOT gate:
- Canonical and derived docs aligned to runtime metadata.

Post-step commit gate:
- Commit: `9af56a1`
- Message: `docs: document parallel branch cancel metadata in chain.json`
