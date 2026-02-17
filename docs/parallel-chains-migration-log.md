# Parallel Chains Migration Log

Date started: 2026-02-14
Status: Archived historical record (frozen)
Scope: `/home/hlyytine/autopilot` and `/home/hlyytine/tii-sel4/projects/virtioso-camkes-vm`

Note:
- This file is a chronological implementation log and intentionally preserves
  historical terminology from each step (including pre-cutover op names).
- Historical entries include legacy chain-level `prepare_next_run` orchestration
  references and should not be used as current runtime policy.
- Do not rewrite historical entries except for append-only corrections.

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

## Step 2026-02-14-14

Summary:
- Adopt explicit verdict-setting in `vm_common` before recovery tail steps.

Pre-step DRY/SSOT gate:
- Canonical behavior source: `docs/chain-spec.md` (`set_test_verdict`).
- Chain migration target: `chains/vm_common.json`.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Added `set_verdict_pass` and `set_verdict_fail` using `set_test_verdict`.
- Routed log-analysis pass/fail transitions through verdict steps before recovery forks.

Verification:
- `validate_chain(vm_common)` returns `ok`.

Post-step DRY/SSOT gate:
- Chain semantics now use explicit verdict-setting and remain behaviorally equivalent for current terminal routing.

Post-step commit gate:
- Commit: `af9acba`
- Message: `chains: set explicit test verdict before vm_common recovery`

## Step 2026-02-14-15

Summary:
- Add daemon-scoped task registry and signal primitives to runtime.

Pre-step DRY/SSOT gate:
- Canonical direction source: `docs/parallel-chains-plan.md` (persistent tasks/signals).
- Runtime targets: `chain_runtime.py`, `orin_kernel_autopilot.py`.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Added chain ops:
  - `task_spawn`
  - `task_join`
  - `signal_set`
  - `signal_wait`
- Added strict validation for new ops (required fields/reducer labels).
- Added daemon-scoped `task_registry` and `signals` storage in orchestrator and
  injected into request/bootstrap contexts.

Verification:
- `python3 -m py_compile chain_runtime.py orin_kernel_autopilot.py`
- `validate_chain(...)` smoke test for new op schema returned `ok`.

Post-step DRY/SSOT gate:
- Runtime now includes persistent bookkeeping/signaling primitives required by plan.

Post-step commit gate:
- Commit: `f9bef48`
- Message: `runtime: add persistent task registry and signal chain ops`

## Step 2026-02-14-16

Summary:
- Update visualization tooling to expose reducer join/task/signal step metadata.

Pre-step DRY/SSOT gate:
- Canonical step schema source: `docs/chain-spec.md`.
- Tool target: `tools/autopilot_chain_viz.py`.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Added verbose label fields for:
  - `parallel_join` (`join_groups`, `reduce`)
  - `task_spawn` (`task`, `chain`, `chain_file`)
  - `task_join` (`tasks`, `reduce`)
  - `signal_set` / `signal_wait` (`signal`, `consume`)

Verification:
- `python3 -m py_compile tools/autopilot_chain_viz.py`
- Render sanity check confirmed reducer fields in `vm_common` diagram.

Post-step DRY/SSOT gate:
- Tool labels align with updated runtime/schema semantics.

Post-step commit gate:
- Commit: `2b70ad0`
- Message: `tools: visualize reducer joins and task/signal step fields`

## Step 2026-02-14-17

Summary:
- Prototype `prepare_for_next_run` flow using daemon-scoped task/signal primitives.

Pre-step DRY/SSOT gate:
- Canonical runtime/schema source: `chain_runtime.py` + `docs/chain-spec.md`.
- Chain migration targets: `chains/vm_common.json`, `chains/prepare_next_run_task.json`.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Added `chains/prepare_next_run_task.json`:
  - waits on `signal_wait(prepare_next_run_go)`,
  - executes `recovery_boot`,
  - returns `pass`/`fail`.
- Updated `vm_common`:
  - spawn prep task at start (`task_spawn`),
  - set verdict after log analysis,
  - signal prep start (`signal_set`),
  - wait for prep completion (`task_join reduce=all_pass`),
  - fallback to relay reset on prep fail/timeout.

Obvious-bug escalation:
- Found lifecycle bug: `run_bootefi fail` path skipped signal/join, leaving prep task blocked and causing next-run spawn failure.
- Human direction: fix immediately.
- Fix applied: route `run_bootefi fail/timeout` to `set_verdict_fail` path (which signals and joins prep task).

Verification:
- `validate_chain(prepare_next_run_task)` returns `ok`.
- `validate_chain(vm_common)` returns `ok`.

Post-step DRY/SSOT gate:
- Prototype chain flow is aligned with task/signal runtime primitives and explicit verdict semantics.

Post-step commit gate:
- Commit: `3e164dd`
- Message: `chains: prototype prepare-next-run task via task/signal ops`

## Step 2026-02-14-18

Summary:
- Migrate remaining `fork_recovery_*` chain flows to `task_spawn` + `signal_set` + `task_join`.

Pre-step DRY/SSOT gate:
- Canonical migration direction: `docs/parallel-chains-plan.md` (task registry/signals and explicit verdict model).
- Canonical runtime semantics: `chain_runtime.py` + `docs/chain-spec.md`.
- Migration targets:
  - `chains/linux-kernel.json`
  - `chains/linux-kernel-multi.json`
  - `chains/sel4test.json`
  - `chains/boot-interactive.json`
  - `chains/boot-interactive-efi.json`

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Removed legacy `fork_recovery_pass` / `fork_recovery_fail` flow from target chains.
- Added per-chain prep-task lifecycle:
  - `task_spawn` for `prepare_next_run` using `prepare_next_run_task`,
  - `set_test_verdict` (`pass`/`fail`) before housekeeping join,
  - `signal_set(prepare_next_run_go)`,
  - `task_join(tasks=[prepare_next_run], reduce=all_pass)`,
  - relay fallback on join fail/timeout before terminal step.
- Routed post-spawn failure and timeout paths to verdict+signal+join path so spawned prep tasks are not leaked.
- Updated `run_bootefi` fail paths in EFI-based chains to go through verdict+join flow.

Verification:
- JSON validity:
  - `python3 -m json.tool chains/linux-kernel.json`
  - `python3 -m json.tool chains/linux-kernel-multi.json`
  - `python3 -m json.tool chains/boot-interactive.json`
  - `python3 -m json.tool chains/boot-interactive-efi.json`
  - `python3 -m json.tool chains/sel4test.json`
- Runtime validation:
  - `validate_chain(linux-kernel)` returns `ok`
  - `validate_chain(linux-kernel-multi)` returns `ok`
  - `validate_chain(boot-interactive)` returns `ok`
  - `validate_chain(boot-interactive-efi)` returns `ok`
  - `validate_chain(sel4test)` returns `ok`
- Legacy-removal check:
  - `rg "fork_recovery|\"type\": \"fork\"|\"type\": \"join\"" chains` returned no matches.

Post-step DRY/SSOT gate:
- Remaining critical chains now follow the same task/signal prep-for-next-run model and explicit verdict/workflow split.
- Coordinated parallel progression remains on `parallel_split`/`parallel_join` (no rename cutover yet).

Post-step commit gate:
- Commit: `d666040`
- Message: `chains: migrate remaining recovery fork flows to task/signal joins`

## Step 2026-02-14-19

Summary:
- Add trace metadata so visualization can map parallel branches to split/join steps.

Pre-step DRY/SSOT gate:
- Canonical schema/runtime source: `chain_runtime.py` + `docs/chain-spec.md`.
- Tooling requirement source: `docs/parallel-chains-plan.md` Phase 5 fan-out/fan-in rendering acceptance.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- `chain_runtime.py` now records:
  - `parallel_groups.<group>.split_step`
  - `parallel_groups.<group>.split_chain`
  - `parallel_groups.<group>.join.step`
  - `parallel_groups.<group>.join.chain`
- Added execution-context tracking of current step name for precise metadata capture.

Verification:
- `python3 -m py_compile chain_runtime.py`

Post-step DRY/SSOT gate:
- Runtime now emits the join/split provenance metadata needed by visualization tools to render true parallel topology.

Post-step commit gate:
- `~/autopilot` commit: `d215a10`
- Message: `runtime: record split/join step metadata for parallel groups`

## Step 2026-02-14-20

Summary:
- Fix trace diagram topology so parallel_split and parallel_join are rendered as real fan-out/fan-in, not single-path sequence only.

Pre-step DRY/SSOT gate:
- Canonical metadata source: `chain_runtime.py` `parallel_groups` fields.
- Tool target: `projects/virtioso-camkes-vm/tools/autopilot_chain_viz.py`.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Trace rendering now maps step names to rendered trace nodes.
- Parallel-group subgraph now links:
  - split step node -> each branch node (fan-out),
  - each branch node -> join step node (fan-in).
- Group labels now include split/join step names when present.

Verification:
- `python3 -m py_compile tools/autopilot_chain_viz.py`
- Synthetic trace fixture check confirms explicit fan-out/fan-in edges:
  - split -> branch edges for all configured branches
  - branch -> join edges for all configured branches

Post-step DRY/SSOT gate:
- Trace visualization now reflects actual parallel topology and reducer context instead of only chronological adjacency.

Post-step commit gate:
- `~/tii-sel4/projects/virtioso-camkes-vm` commit: `fe55f56`
- Message: `tools: render parallel split/join fanout in trace diagrams`

## Step 2026-02-14-21

Summary:
- Remove stale fork-recovery wording from docs and regenerate chain reference diagrams from current chains.

Pre-step DRY/SSOT gate:
- Canonical runtime semantics: `chains/*.json` + `chain_runtime.py`.
- Docs must not claim legacy `fork_recovery_*` flow after migration completion.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- Updated `~/autopilot/docs` wording to current task/signal model:
  - `README.md`
  - `overview.md`
  - `runbook.md`
  - `architecture.md`
  - `parallel-chains-inventory.md` (fork/join chain usage now zero; blockers adjusted)
- Regenerated chain diagrams from live chain JSON using:
  - `python3 tools/autopilot_chain_viz.py --all --docs --profiles-dir /home/hlyytine/autopilot/chains`
- Kept generated `~/autopilot/diagrams/*.mmd` by explicit human direction.
- Updated generated reference page:
  - `~/tii-sel4/projects/virtioso-camkes-vm/docs/reference/autopilot-chain-diagrams.md`

Verification:
- `rg "fork_recovery|type=fork|fork: recovery_boot" docs/reference/autopilot-chain-diagrams.md` -> no matches.
- Generated diagram set includes migrated chains and task/signal-based flows.

Post-step DRY/SSOT gate:
- Autopilot docs and generated diagrams are now aligned with migrated chain behavior and no longer describe removed fork-recovery paths.

Post-step commit gate:
- `~/autopilot` commit: `dd895f6`
- Message: `docs: sync runtime semantics and regenerate chain diagrams`
- `~/tii-sel4/projects/virtioso-camkes-vm` commit: `24dbc81`
- Message: `docs: refresh autopilot chain reference diagrams`

## Step 2026-02-14-22

Summary:
- Remove remaining legacy `fork`/`join` runtime plumbing and corresponding trace fields.

Pre-step DRY/SSOT gate:
- Chains already migrated off legacy fork/join usage.
- Runtime still contained legacy handlers and output fields (`forks`, `chain.fork.*` paths), violating no-transition direction.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- `chain_runtime.py`:
  - removed legacy `fork`/`join` dispatch handlers and implementation paths,
  - validation now rejects `type=fork` and `type=join`,
  - removed `forks` recording from `chain.json`,
  - replaced abort recovery `fork` path with detached chain runner (`chain.abort_recovery.*.json`).
- `orin_kernel_autopilot.py`:
  - removed legacy `forks`/`fork_recorders` context fields.
- `sel4_client.py`:
  - canceled result writer no longer emits `forks` metadata.

Verification:
- `python3 -m py_compile chain_runtime.py orin_kernel_autopilot.py sel4_client.py`
- `validate_chain` passes for all chain JSON files under `chains/`.
- grep check confirms no runtime code references to legacy fork/join handlers remain.

Post-step DRY/SSOT gate:
- Runtime semantics now match migrated chain model (task/signal + parallel groups) without legacy fork/join compatibility paths.

## Step 2026-02-14-23

Summary:
- Remove legacy fork-trace rendering from visualization tooling.

Pre-step DRY/SSOT gate:
- Runtime no longer emits `chain.fork.*.json`; diagrams should not expose obsolete include-fork mode.

Pre-step repo hygiene gate:
- `~/autopilot`: clean before edits.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean before edits.

Implementation:
- `tools/autopilot_chain_viz.py`:
  - removed `--include-forks` option and related trace rendering path,
  - removed fork-specific static graph edge rendering logic.

Verification:
- `python3 -m py_compile tools/autopilot_chain_viz.py`
- `python3 tools/autopilot_chain_viz.py --all --docs --profiles-dir /home/hlyytine/autopilot/chains`

Post-step DRY/SSOT gate:
- Visualization now tracks only active chain semantics and artifacts.

## Step 2026-02-14-24

Summary:
- Execute final op-name cutover from `parallel_split`/`parallel_join` to canonical `split`/`join`.

Pre-step DRY/SSOT gate:
- Legacy `fork`/legacy `join` runtime paths were removed in previous step.
- Cutover precondition satisfied: no chain JSON relies on old fork-join semantics.

Pre-step repo hygiene gate:
- `~/autopilot`: clean.
- `~/tii-sel4/projects/virtioso-camkes-vm`: clean.

Implementation:
- `chain_runtime.py`:
  - dispatch/validation now treat `split` and `join` as canonical ops,
  - validation rejects deprecated `parallel_split`/`parallel_join`,
  - internal step handlers renamed accordingly.
- `chains/vm_common.json`:
  - switched op types to `split` and `join`.
- `tools/autopilot_chain_viz.py`:
  - static parser/renderer updated to detect `split`/`join`.

Verification:
- `python3 -m py_compile chain_runtime.py orin_kernel_autopilot.py sel4_client.py`
- `python3 -m py_compile tools/autopilot_chain_viz.py`
- `validate_chain(...)` passes for all chain JSON under `chains/`.
- Regenerated docs diagrams from live chains.

Post-step DRY/SSOT gate:
- Runtime, chains, and tooling now agree on canonical `split`/`join` semantics.

## Step 2026-02-14-25

Summary:
- Align active docs to post-cutover terminology and remove stale migration-stage wording.

Implementation:
- Updated active docs (`README`, `overview`, `architecture`, `runbook`, `chain-spec`,
  `parallel-chains-inventory`, `dry-ssot-tracking`) to canonical naming where applicable.
- Regenerated reference chain diagrams in `tii-sel4` docs.

Post-step DRY/SSOT gate:
- Active operator/developer docs now describe canonical `split`/`join` semantics.

## Step 2026-02-14-26

Summary:
- Close migration artifacts: freeze historical records and complete final terminology sweep.

Implementation:
- Updated active guidance:
  - `AGENTS.md` now uses canonical `split`/`join` naming.
  - `docs/parallel-chains-inventory.md` wording updated to avoid migration-stage naming.
- Marked historical docs as archived/frozen:
  - `docs/parallel-chains-migration-log.md`
  - `docs/parallel-chains-plan.md`

Final grep sweep:
- `rg "parallel_split|parallel_join"` across both repos now matches only:
  - historical docs (`parallel-chains-migration-log.md`, `parallel-chains-plan.md`),
  - runtime deprecation guard in `chain_runtime.py`.

Disposition:
- Historical docs intentionally retain old terms for auditability.
- Runtime guard intentionally retains old terms to produce explicit validation errors on deprecated schema.
