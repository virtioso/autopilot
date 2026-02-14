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

## Step 2026-02-14-07

Summary:
- Add migration-stage semantic guards for `join` vs `parallel_join` field usage.

Pre-step DRY/SSOT gate:
- Canonical behavior source: `chain_runtime.py`.
- Derived docs impacted: none (runtime-only validation tightening).

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Reject `parallel_join` steps that define `chain`.
- Reject legacy `join` steps that define `group`.

Post-step DRY/SSOT gate:
- Legacy and coordinated join semantics are explicitly separated by validation.

Post-step commit gate:
- Commit: `b30f91c`
- Message: `runtime: reject ambiguous join/parallel_join fields`

## Step 2026-02-14-08

Summary:
- Add runtime `cancel_reason` metadata for canceled non-winner parallel branches.

Pre-step DRY/SSOT gate:
- Canonical runtime trace source: `chain_runtime.py`.
- Derived consumer target: trace visualization.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Added `cancel_reason` field to recorded parallel branch state.
- Populate `winner:<branch>` when cancellation is due to winner latch.

Post-step DRY/SSOT gate:
- Runtime metadata now carries explicit cancellation provenance.

Post-step commit gate:
- Commit: `1616f21`
- Message: `runtime: record parallel branch cancel reason metadata`

## Step 2026-02-14-09

Summary:
- Render `parallel_groups` winner/canceled state in trace diagrams.

Pre-step DRY/SSOT gate:
- Canonical visualization source: `tools/autopilot_chain_viz.py`.
- Runtime metadata source: `chain_runtime.py` (`parallel_groups` + `cancel_reason`).

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Added dedicated trace subgraph for parallel groups.
- Styled winner and canceled branches distinctly.

Post-step DRY/SSOT gate:
- Visualization and runtime metadata are aligned.

Post-step commit gate:
- Commit: `91c4b16`
- Message: `tools: show parallel group winner and canceled branches in traces`

## Step 2026-02-14-10

Summary:
- Inventory and classify all `fork`/`join`/parallel usage in chain files.

Pre-step DRY/SSOT gate:
- Canonical source: `chains/*.json`.
- Derived output: migration inventory doc.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Created `docs/parallel-chains-inventory.md` with:
  - coordinated parallel flows,
  - legacy fork/join usage classification,
  - final-rename blockers.

Post-step DRY/SSOT gate:
- Inventory establishes current baseline and blockers for rename gate.

Post-step commit gate:
- Commit: `8ec5175`
- Message: `docs: inventory fork/join and parallel group usage`

## Step 2026-02-14-11

Summary:
- Redefine parallel model toward reducer-based joins and decoupled verdict/workflow state.

Pre-step DRY/SSOT gate:
- Canonical semantic source: `docs/chain-spec.md`.
- Derived docs to align: `docs/parallel-chains-plan.md`, `docs/runbook.md`,
  `docs/architecture.md`, `docs/overview.md`.
- Drift found: older first-result-wins-only wording and no explicit verdict/workflow split.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Added reducer semantics for `parallel_join` (`any_pass`, `all_pass`).
- Added strict join-target validation requirement (`join_groups` exists and is known).
- Added persistent task registry/signal model direction in plan.
- Added explicit `test_verdict` vs `workflow_state` separation to docs.

Post-step DRY/SSOT gate:
- Canonical and derived docs are aligned on reducer joins and verdict separation.

Post-step commit gate:
- Commit: `0ca4875`
- Message: `docs: define reducer-based parallel join and verdict separation`

## Step 2026-02-14-12

Summary:
- Implement reducer-based `parallel_join` runtime schema and migrate `vm_common` join step.

Pre-step DRY/SSOT gate:
- Canonical behavior source: `docs/chain-spec.md`.
- Runtime implementation target: `chain_runtime.py`.
- Chain migration target: `chains/vm_common.json`.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- `parallel_join` now requires:
  - `join_groups` (non-empty list),
  - `reduce` (`any_pass` or `all_pass`).
- Validation rejects legacy `group` on `parallel_join`.
- Validation enforces join targets exist in chain `parallel_split` groups.
- Runtime reducer decisions are recorded under parallel-group `join` metadata.
- Updated `vm_common` to use:
  - `join_groups: [\"vm_boot_and_ftrace_watch\"]`
  - `reduce: \"any_pass\"`

Verification:
- `python3 -m py_compile /home/hlyytine/autopilot/chain_runtime.py`
- `validate_chain(vm_common)` returns `ok`.

Post-step DRY/SSOT gate:
- Runtime and canonical schema now match reducer-based join direction.

Post-step commit gate:
- Commit: `c7a4f57`
- Message: `runtime: add parallel_join reducers and join_groups schema`

## Step 2026-02-14-13

Summary:
- Add initial verdict/workflow state separation to runtime recorder and status APIs.

Pre-step DRY/SSOT gate:
- Canonical semantic source: `docs/chain-spec.md` verdict/workflow model.
- Runtime/API targets: `chain_runtime.py`, `sel4_client.py`.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Recorder now persists:
  - `test_verdict`
  - `workflow_state`
- Added runtime step type:
  - `set_test_verdict` (`verdict=pass|fail`)
- Status APIs now include `chain_summary` with:
  - `overall_status`, `test_verdict`, `workflow_state`, `abort_reason`

Verification:
- `python3 -m py_compile chain_runtime.py sel4_client.py`
- Recorder smoke check confirmed serialized verdict/workflow fields.

Post-step DRY/SSOT gate:
- Runtime/API data model aligned with verdict/workflow separation direction.

Post-step commit gate:
- Commit: `2691cdd`
- Message: `runtime: add test verdict state and expose it in status APIs`
