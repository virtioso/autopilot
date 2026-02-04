# Autopilot System Architecture

**Last Updated**: 2026-02-04

## Purpose

Autopilot is a host-side orchestration service for automated boot testing on an
NVIDIA Orin AGX target. It supports:

- Linux kernel boot tests (single-run and multi-run)
- seL4 EFI binary tests (single-run and multi-run)
- vm_minimal tests with dual-UART capture

It also provides a client library and an MCP server to make it easy for AI
coding tools to submit tests and retrieve results.

## System Diagram (Current)

```
Host PC (192.168.101.100)
┌─────────────────────────────────────────────────────────────────────────┐
│ Autopilot daemon (orin_kernel_autopilot.py)                              │
│ - Watches requests/pending/*.request                                    │
│ - Processes one request at a time                                       │
│ - Writes results/<timestamp>/                                           │
│                                                                         │
│ BootHarness + seL4BootHarness                                            │
│ - UEFI/extlinux navigation via UART                                     │
│ - Log capture and fault detection                                       │
│                                                                         │
│ BoardControl                                                            │
│ - USB relay control via usbrelay_py                                     │
│                                                                         │
│ UART devices                                                            │
│ - /dev/ttyACM0 (main console, ttyTCU0)                                   │
│ - /dev/ttyACM1 (secondary console, UARTI or VM console)                  │
│                                                                         │
│ Client + MCP                                                            │
│ - sel4_client.py                                                        │
│ - sel4_mcp_server.py                                                    │
└─────────────────────────────────────────────────────────────────────────┘
                               │
                         UART + SSH
                               │
                               ▼
Target Orin AGX (192.168.101.112)
┌─────────────────────────────────────────────────────────────────────────┐
│ UEFI firmware + extlinux boot menu                                       │
│ Stock Jetson Linux (SSH target)                                          │
│ Test kernels and seL4 EFI binaries                                       │
└─────────────────────────────────────────────────────────────────────────┘
```

## Core Components

### 1) Orchestrator: `orin_kernel_autopilot.py`

**Responsibilities**
- Polls `requests/pending/` for new requests.
- Moves requests through: pending -> processing -> completed/failed.
- Selects a flow based on request type:
  - `linux` (single/multi-run)
  - `sel4` (single/multi-run)
  - `vm_minimal` (single-run)
- Writes logs and summary artifacts into `results/<timestamp>/`.

**Key Paths**
- Requests:
  - `requests/pending/`
  - `requests/processing/`
  - `requests/completed/`
  - `requests/failed/`
- Results:
  - `results/<timestamp>/`
  - For multi-run: `results/<timestamp>/run_<n>/`

### 2) Boot Harnesses

**`BootHarness.py`**
- Common UART handling, status line, and pexpect-based pattern matching.
- `ReadyBootHarness`: boot to stock Linux shell prompt.
- `UpdateBootHarness`: SCP kernel image to target, then reboot.
- `PanicBootHarness`: boot test kernel and detect panic/SMMU faults/success.

**`seL4BootHarness.py`**
- `SeL4UploadHarness`: boot to stock Linux, SCP EFI binary to `/boot/efi/`.
- `SeL4UploadOnlyHarness`: SCP EFI binary without rebooting first.
- `SeL4RunHarness`: UEFI menu navigation -> EFI Shell -> run binary.
- `VMMinimalRunHarness`: dual UART capture for VM console output.

### 3) Board Control

**`BoardControl.py`**
- `BoardControlLocal` (default): uses `usbrelay_py` to toggle reset/recovery.
- `BoardControlRemote`: SSH to a boot server (optional, not default).

### 4) Client + MCP Integration

**`sel4_client.py`**
- API and CLI for submitting tests and retrieving logs.

**`sel4_mcp_server.py`**
- MCP server exposing test submission and log retrieval to AI tools.

## Data Flow (Current)

### Linux Test (Single-Run)
1. Upload kernel via SCP to target.
2. Reboot and boot test kernel.
3. Detect panic/SMMU fault/success.
4. Filter and store logs in `results/<ts>/`.

### Linux Test (Multi-Run)
1. Upload kernel once.
2. Reboot N times (SSH reboot if available, otherwise hardware reset).
3. Store logs per run in `results/<ts>/run_<n>/`.
4. Write `summary.json`.

### seL4 Test (Single-Run)
1. SCP EFI binary to `/boot/efi/`.
2. Reboot and navigate UEFI to EFI Shell.
3. Run binary, capture UART output until quiescent.
4. Filter logs and store in `results/<ts>/`.

### seL4 Test (Multi-Run)
1. Upload once for run 1.
2. Hardware reboot for runs 2..N.
3. Store per-run logs and write `summary.json`.

### vm_minimal
1. Upload EFI binary.
2. Run with `VMMinimalRunHarness`.
3. Capture:
   - `/dev/ttyACM0` (seL4/capdl output)
   - `/dev/ttyACM1` (VM console output)
4. Filter to `sel4.log` and `vm.log`.

## Results and Artifacts

Single-run results:
- `uart-raw.log` (raw UART)
- `sel4.log` / `kernel.log` (filtered)
- `hyp.log` (EL2 UARTI)
- `panic.log`, `smmu_faults.log`, `disassembly.log` (conditional)

Multi-run results:
- `results/<ts>/run_<n>/...`
- `summary.json`

## Planned: Interactive Console Sessions (AI-Driven)

We plan to add a generic interactive console layer for AI tools that:
- Opens UART sessions post-boot.
- Auto-logins based on profile JSON (prompt regex + steps).
- Allows line-based command execution with polling output.
- Writes transcripts to `results/<ts>/console/`.

The interactive phase will be available:
- As part of a test flow (post-boot interactive phase).
- As a dedicated `boot_interactive` request type.

## Interfaces and Request Schema

Current requests are JSON `.request` files in `requests/pending/`.

Planned extension:
```json
{
  "type": "boot_interactive",
  "boot_target": "stock_linux",
  "interactive": {
    "enabled": true,
    "phase": "post_boot",
    "sessions": [
      { "name": "vm0", "port": "/dev/ttyACM0", "profile": "linux-yocto" }
    ]
  }
}
```

## Dependencies

Host:
- Python 3
- `pexpect`, `pyserial`
- `usbrelay_py`
- SSH access to target

Target:
- UEFI boot menu
- SSH server on stock Linux
- `/boot/efi` writable for EFI uploads

