# Parallel Chains Inventory

Date: 2026-02-14
Scope: `/home/hlyytine/autopilot/chains/*.json`

## Coordinated Parallel Flows

These are winner-based concurrent flows and must remain on
`parallel_split`/`parallel_join` during current migration stage.

1. `chains/vm_common.json`
- `parallel_vm_boot_split` (`type=parallel_split`, `group=vm_boot_and_ftrace_watch`)
- `parallel_vm_boot_join` (`type=parallel_join`, `join_groups=["vm_boot_and_ftrace_watch"]`)
- Classification: coordinated parallel control (already migrated).

## Legacy `fork`/`join` Usage

Current `join` usage count: `0`.
Current `fork` usage count: `0`.

## Final-Rename Blockers

Per migration policy, rename `parallel_split` -> `split` and `parallel_join` -> `join`
only after all legacy `fork`/`join` references are eliminated.

Current blockers:
- legacy `fork` in chain JSON: eliminated.
- legacy `join` in chain JSON: eliminated.
- runtime and tooling still expose migration-stage names (`parallel_split`/`parallel_join`);
  final rename cutover remains pending.
