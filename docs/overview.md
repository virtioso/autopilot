# Autopilot System Overview

**Last Updated**: 2026-02-11

## Purpose

Autopilot is a host-side orchestration service for automated boot testing on
the NVIDIA Jetson AGX Orin (Tegra234). It executes chain-based test flows,
handles UART/SSH interactions, and produces structured results for humans and
AI tools.

## Key Features

- **Chain-Based Execution**: All test logic is defined in chain files.
- **Branching Outcomes**: Regex-driven outcomes route to next steps.
- **Reusable Chains**: Chains can call other chains via `call_chain`, `task_spawn`, and parallel groups.
- **Parallel Groups**: Concurrent branch execution with reducer-based join semantics (`any_pass` / `all_pass`).
- **UART Source Mapping**: Dynamic `map_source` ties tty devices to logical sources.
- **tmux-Native Operator UI**: Window switching, status, and abort control are handled by tmux.
- **Structured Results**: `chain.json` captures step-by-step outcomes/errors plus parallel group winner/cancellation metadata.
- **Decoupled Status**: `test_verdict` is tracked independently from `workflow_state` for housekeeping tails.

Defaults for `AUTOPILOT_DIR`, TTYs, and queue names are defined in `config.py` (SSOT).
For Orin AGX EFI workflows, set
`AUTOPILOT_PLATFORM=orin-agx-uefi-netboot` so Autopilot runs
`chains/platform-init-<platform>.json` at daemon startup to set overrides.
`WORKSPACE` must be set before running Autopilot.

## Quick Start

### Start Autopilot

```bash
cd <code_root>
export WORKSPACE=<workspace>
AUTOPILOT_PLATFORM=orin-agx-uefi-netboot \
AUTOPILOT_DIR="${WORKSPACE}/autopilot" \
python3 orin_kernel_autopilot.py
```

### Start Autopilot via MCP (Headless + tmux UI)

If you want Autopilot running headless while preserving the operator UI, start it via
MCP in a tmux session:

```json
{
  "tool": "autopilot_start",
  "autopilot_dir": "${WORKSPACE}/autopilot"
}
```

Attach to the tmux session:

```bash
tmux attach -t autopilot
```

**Orin AGX note**: The MCP start path sets `AUTOPILOT_TTY0=/dev/ttyACM0` and
`AUTOPILOT_TTY1=/dev/ttyACM1` by default. These are Orin AGX-specific and must
be replaced for other platforms (e.g. Raspberry Pi 4 uses `/dev/ttyUSB*`).

### Submit a Request

Requests are JSON files that specify a root chain by name in `profile`:

```bash
cd "${WORKSPACE}/autopilot"
TS=$(date +%Y%m%d-%H%M%S)
cat > requests/pending/${TS}.request <<'EOF_REQ'
{
  "profile": "linux-kernel",
  "description": "single-run kernel test"
}
EOF_REQ
```

Results appear in `results/<timestamp>/`.

## Chain Model (Summary)

Executable chain files live in:

- `<code_root>/chains/*.json`

Each chain file defines:
- `entry`: starting step label
- `steps`: dictionary of steps

There is no `subchains` schema. Reuse is done via:
- `task_spawn`/`task_join` for persistent asynchronous/background tasks
- `call_chain` for synchronous inline chain execution
- `split`/`join` for concurrent branch groups with fail-fast monitoring

Console login/prompt profiles remain in:

- `<code_root>/profiles/linux-yocto.json`
- `<code_root>/profiles/ubuntu-22.json`

## tmux Controls

Autopilot uses tmux-native controls:

- `Ctrl-B` then `0..9`: switch window
- `Ctrl-B` then `r`: abort run and start recovery boot
- `Ctrl-B` then `d`: detach from session
- type directly in a source window to send raw input to that mapped console

## Output Files

Each test produces:

- `chain.json`: structured step results and errors
- `console/<source>.jsonl`: UART transcripts for mapped sources
- Additional log files under `console/` as defined by chain steps (for example, filtered outputs created by `analyze_logs`)
- `device-trees/*.dtb` and `device-trees/*.dts`: guest DTB dumps decoded from logs
- `device-trees/summary.json`: extraction/conversion summary

## Dependencies

Host:
- Python 3, `pexpect`, `pyserial`, `usbrelay_py`
- SSH access to target (default IP `192.168.101.112`)

Target:
- UEFI + extlinux boot menu
- Deployment target required by active profile/platform (for Orin EFI flow:
  SCP upload to `/efiboot/{target_binary_name}` followed by UEFI `boot_efi` command modes)

## See Also

- [Runbook](runbook.md) - Operational procedures and troubleshooting
- [Architecture](architecture.md) - System diagrams and component details
- [Chain Specification](chain-spec.md) - JSON schema and step definitions
