# Parallel Chains Inventory

Date: 2026-02-14
Scope: `/home/hlyytine/autopilot/chains/*.json`

## Coordinated Parallel Flows

These are winner-based concurrent flows and must remain on
`parallel_split`/`parallel_join` during current migration stage.

1. `chains/vm_common.json`
- `parallel_vm_boot_split` (`type=parallel_split`, `group=vm_boot_and_ftrace_watch`)
- `parallel_vm_boot_join` (`type=parallel_join`, `group=vm_boot_and_ftrace_watch`)
- Classification: coordinated parallel control (already migrated).

## Legacy `fork`/`join` Usage

Current `join` usage count: `0`.

Current `fork` usage:

1. `chains/vm_common.json`
- `fork_recovery_pass` -> `recovery_boot`
- `fork_recovery_fail` -> `recovery_boot`
- Classification: side-task async recovery (non-coordinated).

2. `chains/linux-kernel.json`
- `fork_recovery_pass` -> `recovery_boot`
- `fork_recovery_fail` -> `recovery_boot`
- Classification: side-task async recovery (non-coordinated).

3. `chains/linux-kernel-multi.json`
- `fork_recovery_pass` -> `recovery_boot`
- `fork_recovery_fail` -> `recovery_boot`
- Classification: side-task async recovery (non-coordinated).

4. `chains/sel4test.json`
- `fork_recovery_pass` -> `recovery_boot`
- `fork_recovery_fail` -> `recovery_boot`
- Classification: side-task async recovery (non-coordinated).

5. `chains/boot-interactive.json`
- `fork_recovery` -> `recovery_boot`
- Classification: side-task async recovery (non-coordinated).

6. `chains/boot-interactive-efi.json`
- `fork_recovery` -> `recovery_boot`
- Classification: side-task async recovery (non-coordinated).

## Final-Rename Blockers

Per migration policy, rename `parallel_split` -> `split` and `parallel_join` -> `join`
only after all legacy `fork`/`join` references are eliminated.

Current blockers:
- legacy `fork` still present in six chain files (all async recovery paths).
- legacy `join` no longer present in chain JSON files.
