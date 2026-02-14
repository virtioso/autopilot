# Parallel Chains Inventory

Date: 2026-02-14
Scope: `/home/hlyytine/autopilot/chains/*.json`

## Coordinated Parallel Flows

These are winner-based concurrent flows using canonical
`split`/`join` semantics.

1. `chains/vm_common.json`
- `parallel_vm_boot_split` (`type=split`, `group=vm_boot_and_ftrace_watch`)
- `parallel_vm_boot_join` (`type=join`, `join_groups=["vm_boot_and_ftrace_watch"]`)
- Classification: coordinated parallel control (already migrated).

## Legacy `fork`/`join` Usage

Current `join` usage count: `0`.
Current `fork` usage count: `0`.

## Final-Rename Blockers

Final rename cutover status (prefixed names -> canonical names):

Current status:
- legacy `fork` in chain JSON: eliminated.
- legacy `join` in chain JSON: eliminated.
- runtime rejects legacy prefixed split/join op names.
- active chains and tooling use canonical `split`/`join`.
