# Autopilot In A New Project

This guide explains how to use Autopilot as a standalone tool while keeping
project-specific state (queues, results, runtime) inside each project repo.

## Goal

- Keep Autopilot **code** in a dedicated location inside the workspace (example: `<workspace>/tools/autopilot`).
- Keep Autopilot **working state** per project (example: `<workspace>/autopilot`).
- Make it easy for AI tools (Codex/Claude Code) to use Autopilot in any repo.

Defaults for `AUTOPILOT_DIR`, TTYs, and queue names are defined in `config.py` (SSOT).
`WORKSPACE` is required.

## Recommended Layout

- Code location: `<workspace>/tools/autopilot`
- Project working directory: `<project>/autopilot`

Example:
- Code: `<workspace>/tools/autopilot` (clone of this repo)
- Project: `<workspace>`
- Working dir: `<workspace>/autopilot`

## Step 1: Create a Project Working Directory

Create queues and results directories:

```bash
mkdir -p <workspace>/autopilot/{requests,results}
mkdir -p <workspace>/autopilot/requests/{pending,processing,completed,failed}
```

### Optional: One-command initializer

You can use the helper script to create the working directory:

```bash
<workspace>/tools/autopilot/scripts/new-project-init.sh <workspace>
```

## Step 2: Set Environment Variables

At minimum:

```bash
export WORKSPACE=<workspace>
export AUTOPILOT_DIR="$WORKSPACE/autopilot"
export AUTOPILOT_TTY0=/dev/ttyACM0
export AUTOPILOT_TTY1=/dev/ttyACM1
```

## Step 3: Start Autopilot

From the code repo:

```bash
python3 <workspace>/tools/autopilot/orin_kernel_autopilot.py
```

## Step 4: Add Autopilot Command To PATH

Expose the command API used by agents:

```bash
ln -s <workspace>/tools/autopilot/bin/autopilot ~/.local/bin/autopilot
```

Agents must use `autopilot ... --json` for lifecycle, submit, status, and logs.
Do not use MCP tools, direct queue files, or Python internals as fallback paths.

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
You are working in <project>. Autopilot is installed at <workspace>/tools/autopilot, and
AUTOPILOT_DIR is <workspace>/autopilot. Use only `autopilot ... --json`; if it
fails or is ambiguous, stop and ask the human owner. Read the Autopilot docs in
<workspace>/tools/autopilot/docs before making changes.
```

### Prompt Templates

**Codex CLI (short)**

```
You are working in <project>. Autopilot code is in <workspace>/tools/autopilot
and the working directory is AUTOPILOT_DIR=<workspace>/autopilot. Read
<workspace>/tools/autopilot/docs/README.md and
<workspace>/tools/autopilot/docs/runbook.md before changes. Use only
`autopilot ... --json`.
```

**Claude Code (short)**

```
Read <workspace>/tools/autopilot/AGENTS.md and
<workspace>/tools/autopilot/docs/README.md first. Autopilot code is in
<workspace>/tools/autopilot; AUTOPILOT_DIR=<workspace>/autopilot. Use only
`autopilot ... --json`.
```

## Notes

- Autopilot code and project working directories are decoupled on purpose.
- Profiles are **static data** and live in `<workspace>/tools/autopilot/profiles` (single source of truth).
- Do not copy profiles into `AUTOPILOT_DIR`; edits must be made in the code repo.
