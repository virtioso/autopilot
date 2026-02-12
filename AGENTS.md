# AGENTS.md

This file provides guidance to Codex (and other coding agents) for working on
the Autopilot codebase.

## Mandatory Preflight (Before Any Planning or Implementation)

Always read the following documents before you start planning or making changes:

1. `docs/README.md`
2. `docs/overview.md`
3. `docs/runbook.md`
4. `docs/chain-spec.md`
5. `docs/ai-interactive-console.md`

## Working Directory vs Code

`AUTOPILOT_DIR` is the working directory (queues/results/runtime), not the code
path. Executable chains live in the code repo at
`/home/hlyytine/autopilot/chains`. Console login/prompt profiles live at
`/home/hlyytine/autopilot/profiles`. The code can live anywhere (for example
`~/autopilot`), while each project uses its own `AUTOPILOT_DIR`.

## Orin AGX Default Workflow (Mandatory)

For Orin AGX EFI testing, always run Autopilot with:

- `AUTOPILOT_PLATFORM=orin-agx-uefi-netboot`

This enforces the platform-init override chain and routes EFI deployment through
the netboot path (`/tftp/efi/bootimg.efi` + relay reset). Do not use legacy
SSH `/boot/efi` upload flow for Orin AGX unless explicitly requested.

## MCP-Controlled Autopilot Daemon

Autopilot can be started/stopped/restarted via MCP tools:
- `autopilot_start`
- `autopilot_stop`
- `autopilot_restart`
- `autopilot_status`

These tools run the daemon headless by default in a tmux session and return an
attach hint (`tmux attach -t autopilot`) to access the TUI.

Orin AGX note: always specify UARTs explicitly when starting/restarting via MCP:
`tty0="/dev/ttyACM0"` and `tty1="/dev/ttyACM1"`.
Defaults still exist, but callers must provide explicit values.
Replace these for other platforms (e.g. Raspberry Pi 4 uses `/dev/ttyUSB*` and
a different platform override chain).

## When Working From Another Project

If you are running Autopilot from a different project repo, ensure that project
has its own `AGENTS.md` referencing the Autopilot docs above and that it defines
`AUTOPILOT_DIR` for that project.

## Guest DTB Artifacts (Mandatory Workflow)

After every test run, Autopilot extracts guest DTB dumps from logs into
`results/<request_id>/device-trees/` and converts them to `.dts` when possible.

If guest behavior is abnormal, always inspect the generated `.dts` files as part
of first-pass triage.
