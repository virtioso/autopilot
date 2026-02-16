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
| S05 | Add canonical `boot_stock_linux.json` | Done | `chains/boot_stock_linux.json` | chain validates | this commit | Includes `boot_efi` + prompt wait + `ssh_wait_ready` |
| S06 | Rename/refactor deploy chain to `deploy_and_boot_test_efi` | Done | `chains/bootefi_common.json` -> `chains/deploy_and_boot_test_efi.json` | chain validates | this commit | Switched to `/efiboot/{target_binary_name}` + reboot + `boot_efi(test_efi)` |
| S07 | Migrate chain callers to canonical chains | Done | `chains/*.json` | no duplicated stock boot/deploy flows | this commit | Rewired callers to `boot_stock_linux` and `deploy_and_boot_test_efi` |
| S08 | Remove old aliases/references (`bootefi_common`) | Done | `chains/*`, docs | zero live refs | this commit | Removed platform alias and active chain-spec old-name reference |
| S09 | Handle legacy harness alignment/deprecation | Done | `seL4BootHarness.py`, docs | explicit status | this commit | Marked module as legacy and aligned upload cleanup path to `/efiboot` |
| S10 | Docs/diagrams sync in autopilot | Done | `docs/*.md`, `diagrams/*.mmd` | references consistent | this commit | Synced autopilot docs/diagrams and removed obsolete netboot chain artifact |
| S11 | Validation sweep and evidence log | Done | tracking file + outputs | checks green | this commit | py_compile + chain validation + stale-ref scans completed |
| S12 | Final closure summary and completion commit | Planned | tracking file | all steps done | - |  |

## Verification Log
- S01: tracker file created and branch set to `efi-boot-refactor`.
- S02: added runtime dispatch + `_step_boot_efi` implementation and documented `boot_efi` in chain spec.
- S03: added runtime dispatch + `_step_ssh_wait_ready` and documented usage in chain spec.
- S04: naming centralized in `submit_sel4_efi_test`; MCP now reports request-derived `target_binary_name` instead of generating its own.
- S05: added canonical `boot_stock_linux` chain with `uname -a` readiness retry window (1s per try, 10s total).
- S06: renamed `bootefi_common` chain file to `deploy_and_boot_test_efi` and refactored behavior to SCP+SSH reboot+`boot_efi`.
- S07: migrated stock-boot callers (`sel4test`, `boot-interactive*`, `linux-kernel*`, `vm_common`, `recovery_boot`) to canonical chains.
- S08: removed old `bootefi_common` alias wiring from platform-init and updated active chain-spec references.
- S09: documented `seL4BootHarness.py` as legacy/non-authoritative and aligned its cleanup/upload path naming to `/efiboot`.
- S10: updated autopilot docs/diagrams to current chain names and flow; removed obsolete `bootefi_orin_netboot` chain file.
- S11 checks:
  - `python3 -m py_compile chain_runtime.py sel4_client.py sel4_mcp_server.py seL4BootHarness.py` -> `py_compile_ok`
  - `validate_chain` over all chain JSON files -> `chain_validate_ok 19`
  - `rg` stale-ref scan excluding tracker file -> no matches for `bootefi_common|bootefi_orin_netboot|/boot/efi|/tftp/efi/bootimg.efi`
  - canonical caller scan confirms `boot_stock_linux` and `deploy_and_boot_test_efi` call sites in target profiles.

## Closure Checklist
- [ ] All steps S01-S12 complete.
- [ ] No live `bootefi_common` references in active code/docs.
- [ ] Chain validation passes for modified chains.
- [ ] End-to-end behavior documented.
