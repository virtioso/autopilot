# Autopilot System Overview

**Last Updated**: 2026-02-07

## Purpose

Autopilot is a host-side orchestration service for automated boot testing on
the NVIDIA Jetson AGX Orin (Tegra234). It executes **chain-based test flows**
defined in profile JSON files, handles UART/SSH interactions, and produces
structured results for humans and AI tools.

## Key Features

- **Chain-Based Execution**: All test logic is defined in profile chains.
- **Branching Outcomes**: Regex-driven outcomes route to next steps.
- **Parallel Recovery**: Recovery boot can run in parallel with log parsing.
- **UART Source Mapping**: Dynamic `map_source` ties tty devices to logical sources.
- **Built-in TUI**: Screen-like hotkeys to switch windows and abort runs.
- **Structured Results**: `chain.json` captures step-by-step outcomes and errors.

Defaults for `AUTOPILOT_DIR`, TTYs, and queue names are defined in `config.py` (SSOT).

## Quick Start

### Start Autopilot

```bash
cd /home/hlyytine/autopilot
AUTOPILOT_DIR=/home/hlyytine/tii-sel4/autopilot python3 orin_kernel_autopilot.py
```

### Start Autopilot via MCP (Headless + tmux TUI)

If you want Autopilot running headless while preserving the TUI, start it via
MCP in a tmux session:

```json
{
  "tool": "autopilot_start",
  "autopilot_dir": "/home/hlyytine/tii-sel4/autopilot"
}
```

Attach to the TUI:

```bash
tmux attach -t autopilot
```

**Orin AGX note**: The MCP start path sets `AUTOPILOT_TTY0=/dev/ttyACM0` and
`AUTOPILOT_TTY1=/dev/ttyACM1` by default. These are Orin AGX-specific and must
be replaced for other platforms (e.g. Raspberry Pi 4 uses `/dev/ttyUSB*`).

### Submit a Request

Requests are JSON files that specify a **profile**:

```bash
cd /home/hlyytine/tii-sel4/autopilot
TS=$(date +%Y%m%d-%H%M%S)
cat > requests/pending/${TS}.request <<'EOF'
{
  "profile": "linux-kernel",
  "description": "single-run kernel test"
}
EOF
```

Results appear in `results/<timestamp>/`.

Profiles are static data and live in `/home/hlyytine/autopilot/profiles` (single source of truth).

## Chain Model (Summary)

Each profile defines:
- `chain.entry`: starting step label
- `chain.steps`: dictionary of steps
- `chain.subchains`: named subchains for `fork`

Steps include `relay`, `boot_menu`, `wait_pattern`, `upload_*`, `reboot`,
`map_source`, `map_window`, `fork`, and `join`. Terminal steps are explicit
`pass` and `fail`.

## TUI Hotkeys

Autopilot enables a built-in TUI when attached to a TTY:

- `Ctrl-A` then `1..9`: switch window
- `Ctrl-A` then `W`: list windows
- `Ctrl-A` then `X`: exit UI
- `Ctrl-A` then `R`: abort run and start recovery boot

## Output Files

Each test produces:

- `chain.json`: structured step results and errors
- `console/<source>.jsonl`: UART transcripts for mapped sources
- Additional log files under `console/` as defined by the profile chain (for example, filtered outputs created by `analyze_logs`)

## Dependencies

Host:
- Python 3, `pexpect`, `pyserial`, `usbrelay_py`
- SSH access to target (default IP `192.168.101.112`)

Target:
- UEFI + extlinux boot menu
- `/boot/efi` writable for EFI uploads

## See Also

- [Runbook](runbook.md) - Operational procedures and troubleshooting
- [Architecture](architecture.md) - System diagrams and component details
- [Chain Specification](chain-spec.md) - JSON schema and step definitions
