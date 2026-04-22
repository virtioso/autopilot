# Orin AGX UEFI Shell Reset Hardening Plan (2026-02-20)

## Goal
Replace fragile Orin AGX UEFI-entry behavior with deterministic reset-line control:

1. assert reset line
2. wait until all mapped UART input is quiet for 1.0s
3. wait additional 0.5s
4. deassert reset line
5. wait for `startup.nsh` text on `tty0` (fallback accept direct `Shell>`)
6. send Enter when startup marker seen
7. wait for `Shell>` on `tty0`
8. dispatch EFI command and continue existing success checks

Shell acquisition budget from reset assert: <= 60s.

## Locked decisions
- Scope: Orin-only behavior change (`AUTOPILOT_PLATFORM=orin-agx-uefi-netboot`)
- Existing Orin flow is replaced (not opt-in)
- Quiescence rule: 1.0s + fixed 0.5s delay
- `Shell>` fallback is accepted when `startup.nsh` line is absent
- Mirror docs in `<workspace>/projects/virtioso-camkes-vm` are updated in same rollout

## Global guardrails (run before each step)
- SSOT/DRY lint:
  - `bash <workspace>/projects/virtioso-camkes-vm/docs/agents/lint-ssot.sh`
- Python syntax:
  - `python3 -m py_compile chain_runtime.py orin_kernel_autopilot.py BoardControl.py`
- Stale token guard (active docs/chains):
  - `rg -n "bootefi_common|bootefi_orin_netboot|parallel_split|parallel_join" chains docs`

## Progress table
| ID | Scope | Files | Preflight | Validation | Commit message | Status | Commit SHA |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | Create tracking artifact | `docs/orin-agx-uefi-shell-reset-hardening-plan-2026-02-20.md` | global guardrails | `git diff -- docs/orin-agx-uefi-shell-reset-hardening-plan-2026-02-20.md` | `docs: add Orin UEFI shell reset hardening progress plan` | done | `c8c1dae` |
| S2 | Add explicit reset-line API | `BoardControl.py` | global guardrails | `python3 -m py_compile BoardControl.py` | `board: add explicit reset line control methods` | done | `b363fe6` |
| S3 | Implement Orin reset-to-shell in `boot_efi` | `chain_runtime.py` | global guardrails | `python3 -m py_compile chain_runtime.py` | `runtime: implement Orin reset-to-UEFI-shell boot_efi flow` | done | `8e382ba` |
| S4 | Fix chain entry drift | `chains/boot_stock_linux.json` | global guardrails | chain validation via daemon startup path | `chains: fix boot_stock_linux entry flow` | done | `e0653e0` |
| S5 | Autopilot docs sync | `docs/chain-spec.md`, `docs/runbook.md`, `docs/README.md`, `docs/architecture.md`, `docs/boot-sequence.md` | global guardrails | targeted `rg` checks for stale behavior text | `docs: align autopilot boot flow docs with Orin shell sequence` | done | `cc6af51` |
| S6 | Diagram sync | `diagrams/*.mmd`, `diagrams/README.md` | global guardrails | verify changed diagrams reference current chain names | `diagrams: sync chain visuals with current boot flow` | done | `493e799`, `02e2737` |
| S7 | Mirror docs sync in workspace repo | `<workspace>/projects/virtioso-camkes-vm/docs/agents/autopilot-testing-policy.md`, `<workspace>/projects/virtioso-camkes-vm/docs/reference/autopilot-chain-diagrams.md` | repo-local SSOT lint in workspace | repo-local grep + lint | `docs: sync Orin autopilot boot policy with reset-shell flow` | done | `5bebff8` |
| S8 | Final closure and verification stamp | tracking file + any final doc notes | global guardrails | py_compile + lint + summary check | `docs: finalize progress tracking with validation results` | done | - |

## Notes
- This repo already had unrelated staged/unstaged work at start of execution. Work proceeds on top per explicit user instruction.
- Commits are step-atomic and non-amended.
- S7 completed in mirror repo (`<workspace>/projects/virtioso-camkes-vm`) as commit `5bebff8`.
- Stale-token guard (`bootefi_common|bootefi_orin_netboot|parallel_split|parallel_join`) still matches historical tracking docs by design; active chains/runtime were validated separately.

## Final validation summary (S8)
- `bash <workspace>/projects/virtioso-camkes-vm/docs/agents/lint-ssot.sh`: pass
- `python3 -m py_compile chain_runtime.py orin_kernel_autopilot.py BoardControl.py`: pass
- `validate_all_chains()`: pass
