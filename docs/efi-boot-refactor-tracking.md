# EFI Boot Refactor Tracking

## Metadata
- Owner: Codex + hlyytine
- Branch: `efi-boot-refactor`
- Started: 2026-02-16
- Scope: Orin EFI flow refactor with strict DRY/SSOT enforcement
- Policy:
  - No compatibility aliases retained.
  - One tracked step per commit.
  - This file is the progress SSOT for this effort.

## Locked Decisions
- Fixed UEFI mapping:
  - extlinux: `fs3:\EFI\BOOT\BOOTAA64.EFI`
  - test EFI: `fs2:\efiboot\{target_binary_name}`
- Upload method: SCP over SSH.
- Upload target path: `/efiboot`.
- No pre-upload cleanup in `/efiboot`.
- Target naming: `<sanitized binary stem>-<request_id>.EFI`.
- Rename `bootefi_common` -> `deploy_and_boot_test_efi` as final naming state.

## Step Tracker
| Step | Description | Status | Files | Validation | Commit | Notes |
|---|---|---|---|---|---|---|
| S01 | Create tracker file and execution policy | Done | `docs/efi-boot-refactor-tracking.md` | file created | this commit | Initial tracker |
| S02 | Add `boot_efi` runtime step + spec docs | Done | `chain_runtime.py`, `docs/chain-spec.md` | step dispatch + spec entry | this commit | Added `boot_efi` mode mapping and spec example |
| S03 | Add `ssh_wait_ready` runtime step + spec docs | Done | `chain_runtime.py`, `docs/chain-spec.md` | retry/timeout behavior | this commit | Added retry-based SSH readiness probe step |
| S04 | Centralize target naming in submit path and MCP output | Done | `sel4_client.py`, `sel4_mcp_server.py` | naming generated once | this commit | Added `test_name` + `target_binary_name` SSOT in submit path |
| S05 | Add canonical `boot_stock_linux.json` | Planned | `chains/boot_stock_linux.json` | chain validates | - |  |
| S06 | Rename/refactor deploy chain to `deploy_and_boot_test_efi` | Planned | `chains/bootefi_common.json` -> `chains/deploy_and_boot_test_efi.json` | chain validates | - |  |
| S07 | Migrate chain callers to canonical chains | Planned | `chains/*.json` | no duplicated stock boot/deploy flows | - |  |
| S08 | Remove old aliases/references (`bootefi_common`) | Planned | `chains/*`, docs | zero live refs | - | no compat alias |
| S09 | Handle legacy harness alignment/deprecation | Planned | `seL4BootHarness.py`, docs | explicit status | - |  |
| S10 | Docs/diagrams sync in autopilot and tii-sel4 refs | Planned | `docs/*.md`, `diagrams/*.mmd` | references consistent | - |  |
| S11 | Validation sweep and evidence log | Planned | tracking file + outputs | checks green | - |  |
| S12 | Final closure summary and completion commit | Planned | tracking file | all steps done | - |  |

## Verification Log
- S01: tracker file created and branch set to `efi-boot-refactor`.
- S02: added runtime dispatch + `_step_boot_efi` implementation and documented `boot_efi` in chain spec.
- S03: added runtime dispatch + `_step_ssh_wait_ready` and documented usage in chain spec.
- S04: naming centralized in `submit_sel4_efi_test`; MCP now reports request-derived `target_binary_name` instead of generating its own.

## Closure Checklist
- [ ] All steps S01-S12 complete.
- [ ] No live `bootefi_common` references in active code/docs.
- [ ] Chain validation passes for modified chains.
- [ ] End-to-end behavior documented.
