# Autopilot In A New Project

This guide explains how to use Autopilot as a standalone tool while keeping
project-specific state (queues, results, profiles) inside each project repo.

## Goal

- Keep Autopilot **code** in a dedicated location (example: `~/autopilot`).
- Keep Autopilot **working state** per project (example: `~/tii-sel4/autopilot`).
- Make it easy for AI tools (Codex/Claude Code) to use Autopilot in any repo.

## Recommended Layout

- Code location: `~/autopilot` (or any path you prefer)
- Project working directory: `<project>/autopilot`

Example:
- Code: `~/autopilot` (clone of this repo)
- Project: `~/tii-sel4`
- Working dir: `~/tii-sel4/autopilot`

## Step 1: Create a Project Working Directory

Create queues and results directories:

```bash
mkdir -p ~/tii-sel4/autopilot/{profiles,requests,results}
mkdir -p ~/tii-sel4/autopilot/requests/{pending,inflight,done,failed}
```

Sync profiles from the code repo:

```bash
cp -r ~/autopilot/profiles/* ~/tii-sel4/autopilot/profiles/
```

### Optional: One-command initializer

You can use the helper script to create the working directory and `.mcp.json`:

```bash
~/autopilot/scripts/new-project-init.sh ~/tii-sel4
```

## Step 2: Set Environment Variables

At minimum:

```bash
export AUTOPILOT_DIR=~/tii-sel4/autopilot
export AUTOPILOT_TTY0=/dev/ttyACM0
export AUTOPILOT_TTY1=/dev/ttyACM1
```

## Step 3: Start Autopilot

From the code repo:

```bash
python3 ~/autopilot/orin_kernel_autopilot.py
```

## Step 4: Add MCP Server (Optional but Recommended)

Create `<project>/.mcp.json`:

```json
{
  "mcpServers": {
    "sel4-autopilot": {
      "command": "python3",
      "args": ["~/autopilot/sel4_mcp_server.py"],
      "env": {
        "AUTOPILOT_DIR": "${AUTOPILOT_DIR:-~/tii-sel4/autopilot}"
      }
    }
  }
}
```

Some clients auto-load MCP servers from `.mcp.json`; some do not. If MCP is
unavailable, fall back to the request/result queues in `AUTOPILOT_DIR`.

## Step 5: Add AGENTS.md To The Project

Add an `AGENTS.md` in the project root with a mandatory preflight section that
references Autopilot docs, for example:

- `../autopilot/docs/README.md`
- `../autopilot/docs/overview.md`
- `../autopilot/docs/runbook.md`
- `../autopilot/docs/chain-spec.md`
- `../autopilot/docs/ai-interactive-console.md`

## Suggested Prompt For AI Tools

Use a prompt like this when starting a session:

```
You are working in <project>. Autopilot is installed at ~/autopilot, and
AUTOPILOT_DIR is ~/tii-sel4/autopilot. Prefer MCP tools if available; otherwise
use the request/result queues under AUTOPILOT_DIR. Read the Autopilot docs in
~/autopilot/docs before making changes.
```

### Prompt Templates

**Codex CLI (short)**

```
You are working in <project>. Autopilot code is in ~/autopilot and the working
directory is AUTOPILOT_DIR=<project>/autopilot. Read ~/autopilot/docs/README.md
and ~/autopilot/docs/runbook.md before changes. Prefer MCP; otherwise use the
request/result queues under AUTOPILOT_DIR.
```

**Claude Code (short)**

```
Read ~/autopilot/AGENTS.md and ~/autopilot/docs/README.md first. Autopilot code
is in ~/autopilot; AUTOPILOT_DIR=<project>/autopilot. Use MCP if available, else
operate via the request/result queues.
```

## Notes

- Autopilot code and project working directories are decoupled on purpose.
- Profiles are project-local; update them per project needs.
