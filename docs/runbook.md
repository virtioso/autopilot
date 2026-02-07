# Autopilot Runbook

**Last Updated**: 2026-02-07

This runbook describes how to operate the Autopilot service and how the new
chain-based execution model works, including startup mappings, recovery
behavior, and the built-in TUI controls.

## Quick Glossary

- **Chain**: A labeled set of steps executed by the chain runner.
- **Step**: A unit of work (boot menu, wait for prompt, upload, etc).
- **Outcome**: A regex match (or action result) that routes to another step.
- **Source**: Logical name for a UART or log stream (for matching and logging).
- **Window**: A TUI view bound to a logical source.

## Prerequisites

1. Target board reachable over SSH at `192.168.101.112`.
2. UART devices present on host:
   - `/dev/ttyACM0` (primary UART)
   - `/dev/ttyACM1` (secondary UART)
3. USB relay accessible for power/reset control (`usbrelay_py`).
4. Python dependencies installed: `pexpect`, `pyserial`, `usbrelay_py`.
5. Autopilot working directory exists (requests/results/runtime).

## Directory Layout

- Code: `/home/hlyytine/autopilot`
- Working dir: `${AUTOPILOT_DIR:-/home/hlyytine/tii-sel4/autopilot}`
- Requests: `${AUTOPILOT_DIR}/requests`
- Results: `${AUTOPILOT_DIR}/results`
- Runtime state: `${AUTOPILOT_DIR}/runtime`
- Profiles: `/home/hlyytine/autopilot/profiles` (code repo, single source of truth)

## Start the Autopilot Daemon

```bash
cd /home/hlyytine/autopilot
AUTOPILOT_DIR=/home/hlyytine/tii-sel4/autopilot python3 orin_kernel_autopilot.py
```

Confirm it prints:
- `Watching: .../requests/pending`
- `Results:  .../results`

### Start via MCP (Headless + tmux TUI)

Use the MCP tools to start Autopilot in a detached tmux session:

```json
{
  "tool": "autopilot_start",
  "autopilot_dir": "/home/hlyytine/tii-sel4/autopilot"
}
```

The response includes an `attach_hint`, typically:

```bash
tmux attach -t autopilot
```

## Stop the Autopilot Daemon

1. Press `Ctrl+C` in the running terminal.
2. Autopilot will move any `processing` requests back to `pending`.

### Stop via MCP

```json
{
  "tool": "autopilot_stop",
  "autopilot_dir": "/home/hlyytine/tii-sel4/autopilot"
}
```

### Restart via MCP

```json
{
  "tool": "autopilot_restart",
  "autopilot_dir": "/home/hlyytine/tii-sel4/autopilot"
}
```

### Codex MCP Examples

When calling from Codex, use the fully qualified MCP tool names:

```python
mcp__sel4-autopilot__autopilot_start(
    autopilot_dir="/home/hlyytine/tii-sel4/autopilot"
)

mcp__sel4-autopilot__autopilot_restart(
    autopilot_dir="/home/hlyytine/tii-sel4/autopilot"
)

mcp__sel4-autopilot__autopilot_status(
    autopilot_dir="/home/hlyytine/tii-sel4/autopilot"
)

mcp__sel4-autopilot__autopilot_stop(
    autopilot_dir="/home/hlyytine/tii-sel4/autopilot"
)
```

## Chain Model Overview

Each request is executed by a chain definition stored in a profile JSON.
Chains are the **only** source of test logic.

- Steps route to other steps by label.
- Outcomes match regex patterns on a **source** (logical UART/log stream).
- Timeouts are modeled per step with `on_timeout`.
- Terminal steps are explicit: `pass` and `fail`.

### Startup Chain (Runs Once at Launch)

On daemon startup, a special chain runs to establish default mappings. It
typically binds:

- `/dev/ttyACM0` -> `tty0`
- `/dev/ttyACM1` -> `tty1`
- Window 1 -> `tty0`
- Window 2 -> `tty1`

This ensures the TUI has windows immediately.

### Forked Recovery Boot

During a test, Autopilot may start a **forked** recovery boot chain to return
the board to stock Linux while log parsing continues. The main chain reports
results immediately; the recovery runs in the background unless a join is
explicitly requested.

## TUI Controls (Screen-Like)

Autopilot enables a built-in TUI if it is attached to a TTY.

- `Ctrl-A` then `X`: exit the TUI (return to normal output or stop session).
- `Ctrl-A` then `1..9`: switch to window N.
- `Ctrl-A` then `W`: show window list (window number -> source).
- `Ctrl-A` then `R`: abort the current test (user abort) and start recovery boot.
- `Ctrl-A` then `I`: toggle interactive input mode for the active window.

When interactive input is enabled, keystrokes are sent to the **source**
associated with the currently visible window.

### Ending Interactive Sessions

Interactive sessions end when the login prompt appears again (e.g. after
typing `exit`). The chain only ends **after** a shell prompt has been seen,
so the initial login banner does not terminate the session.

On success, the interactive chains **fork a recovery boot** to return the
board to stock Linux in the background while the run reports `pass`.

If `hold_open` is set to `false`, the interactive step returns immediately
while leaving the session active for humans or AI tools.

Stock Linux boot phases in other chains now invoke a **non-blocking**
interactive step so a console is always available without slowing the flow.

### Console Profiles

- `ubuntu-22` is used for stock Ubuntu consoles.
- `linux-yocto` is reserved for seL4 guest/Yocto consoles.

### Status Line

Autopilot renders a status line showing:
- Current step name
- Request ID and profile
- Chain/subchain name and elapsed time
- Active window and source
- Interactive input state (on/off)

The status line is rendered at the bottom of the terminal when possible.

If Autopilot is not running in a TTY, the TUI is disabled and keybindings are
ignored.

## Error Codes in chain.json

`chain.json` includes a standard error code for each step failure:

- `timeout`
- `regex_miss`
- `user_abort`
- `exception`
- `canceled`
- `validation_error`

## Submit a Request (Example)

Requests reference a profile that contains a chain definition.

```bash
cd /home/hlyytine/tii-sel4/autopilot
TS=$(date +%Y%m%d-%H%M%S)
cat > requests/pending/${TS}.request <<'EOF'
{
  "profile": "linux-yocto",
  "description": "single-run kernel test"
}
EOF
```

Results appear in `results/<timestamp>/`.

## Interactive EFI Sessions

For EFI-based interactive sessions, use the `boot-interactive-efi` profile and
provide `binary_path` and `binary_name` in the request:

```bash
TS=$(date +%Y%m%d-%H%M%S)
cat > requests/pending/${TS}.request <<'EOF'
{
  "profile": "boot-interactive-efi",
  "type": "boot_interactive",
  "binary_path": "/absolute/path/to/sel4test.efi",
  "binary_name": "sel4test.efi",
  "description": "EFI interactive session"
}
EOF
```

## Profile Chains (Where to Edit)

Profiles live in `/home/hlyytine/autopilot/profiles`. Each profile defines:

- `chain.entry`
- `chain.steps`
- optional `chain.subchains`

Profiles are static data. Edit them only in the code repo and do not copy them into
`AUTOPILOT_DIR`.

Example step types:
- `relay`
- `boot_menu`
- `wait_pattern`
- `upload_kernel`
- `upload_efi`
- `reboot`
- `map_source`
- `map_window`
- `send_cmd`
- `interactive_console`
- `fork`, `join`
- `pass`, `fail`

## Troubleshooting

### Chain Validation Fails
1. Check `chain.json` or stderr for validation errors.
2. Verify all step labels referenced by `next`, `on_timeout`, or `on_error`.

### Boot Menu Not Detected
1. Inspect UART logs in `results/<timestamp>/console/` (profile-defined log names).
2. Verify the regex in the `boot_menu` step matches actual output.

### SSH Upload Fails
1. Check SSH auth: `ssh root@192.168.101.112 hostname`.
2. Confirm network connectivity and correct target IP.

### TTY Missing
1. `ls /dev/ttyACM*`
2. Replug USB serial adapters if needed.

### Recovery Boot Not Completing
1. Verify recovery chain definition in the profile.
2. Check `results/<timestamp>/chain.json` fork status.
