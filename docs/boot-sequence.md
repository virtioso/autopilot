# Boot Sequence and UEFI Entry

**Last Updated**: 2026-02-20

## Overview

Autopilot uses chain-driven boot orchestration. For Orin AGX EFI workflows,
`boot_efi` is authoritative and performs reset-line + UART-driven shell entry.

## Orin AGX UEFI Shell Entry Contract

Active when:
- `AUTOPILOT_PLATFORM=orin-agx-uefi-netboot`
- chain step type is `boot_efi`

Sequence:
1. Assert reset line.
2. Wait for all mapped UART sources to be quiet for `1.0s`.
3. Wait an additional fixed `0.5s`.
4. Deassert reset line.
5. Wait on `tty0` for startup marker (`startup.nsh`) and/or `Shell>`.
6. Send Enter once startup marker appears.
7. Require `Shell>` within `60s` from reset assertion.
8. Dispatch EFI command by mode:
   - `extlinux`: `fs3:\EFI\BOOT\BOOTAA64.EFI`
   - `test_efi`: `fs2:\efiboot\{target_binary_name}`

If `Shell>` appears without a startup marker, shell is accepted.

## Canonical Chains

- `boot_stock_linux`:
  - `boot_efi(mode=extlinux)`
  - `wait_pattern` for stock login/shell prompt
  - `ssh_wait_ready`
- `deploy_and_boot_test_efi`:
  - upload EFI (and profile-specific prereqs)
  - reboot
  - `boot_efi(mode=test_efi)`

## Operational Notes

- Platform init chain: `platform-init-orin-agx-uefi-netboot`
- TTY mapping is defined by startup chain (`tty0`, `tty1`) and request chains.
- Primary evidence for UEFI entry issues is in `results/<id>/console/tty0*`.

## Troubleshooting

1. Missing shell prompt:
   - check `startup.nsh`/`Shell>` markers in `tty0` logs
   - verify UART mappings and physical device presence
2. Reset path failures:
   - verify relay/reset wiring and board control availability
3. Post-dispatch failures:
   - verify command path (`fs2:`/`fs3:`) and target binary name/path

## See Also

- `docs/runbook.md`
- `docs/chain-spec.md`
- `docs/overview.md`
