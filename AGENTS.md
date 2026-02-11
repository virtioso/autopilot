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
path. Profiles are static data and live in the code repo at
`/home/hlyytine/autopilot/profiles` (single source of truth). The code can live
anywhere (for example `~/autopilot`), while each project uses its own
`AUTOPILOT_DIR`.

## MCP-Controlled Autopilot Daemon

Autopilot can be started/stopped/restarted via MCP tools:
- `autopilot_start`
- `autopilot_stop`
- `autopilot_restart`
- `autopilot_status`

These tools run the daemon headless by default in a tmux session and return an
attach hint (`tmux attach -t autopilot`) to access the TUI.

Orin AGX note: MCP start uses default UARTs `/dev/ttyACM0` and `/dev/ttyACM1`.
Replace these for other platforms (e.g. Raspberry Pi 4 uses `/dev/ttyUSB*`).

## When Working From Another Project

If you are running Autopilot from a different project repo, ensure that project
has its own `AGENTS.md` referencing the Autopilot docs above and that it defines
`AUTOPILOT_DIR` for that project.

## Guest DTB Artifacts (Mandatory Workflow)

After every test run, Autopilot extracts guest DTB dumps from logs into
`results/<request_id>/device-trees/` and converts them to `.dts` when possible.

If guest behavior is abnormal, always inspect the generated `.dts` files as part
of first-pass triage.
