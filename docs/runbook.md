# Autopilot Runbook

**Last Updated**: 2026-02-08

This runbook describes how to operate the Autopilot service and how the new
chain-based execution model works, including startup mappings, recovery
behavior, and tmux-native operator controls.

## Quick Glossary

- **Chain**: A labeled set of steps executed by the chain runner.
- **Step**: A unit of work (boot menu, wait for prompt, upload, etc).
- **Outcome**: A regex match (or action result) that routes to another step.
- **Source**: Logical name for a UART or log stream (for matching and logging).
- **Window**: A tmux window bound to a logical source.

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

Defaults for `AUTOPILOT_DIR`, TTYs, and queue names are defined in `config.py` (SSOT).

## Start the Autopilot Daemon

```bash
cd /home/hlyytine/autopilot
AUTOPILOT_DIR=/home/hlyytine/tii-sel4/autopilot python3 orin_kernel_autopilot.py
```

Confirm it prints:
- `Watching: .../requests/pending`
- `Results:  .../results`

### Start via MCP (Headless + tmux UI)

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

When submitting tests via MCP, the server will auto-start Autopilot if it is
not running, so you usually do not need to start it manually.

**Orin AGX note**: The MCP start path sets `AUTOPILOT_TTY0=/dev/ttyACM0` and
`AUTOPILOT_TTY1=/dev/ttyACM1` by default. These are Orin AGX-specific and must
be replaced for other platforms (e.g. Raspberry Pi 4 uses `/dev/ttyUSB*`).
These defaults are injected into the tmux session environment.

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

This ensures tmux source windows can be created immediately.

### Forked Recovery Boot

During a test, Autopilot may start a **forked** recovery boot chain to return
the board to stock Linux while log parsing continues. The main chain reports
results immediately; the recovery runs in the background unless a join is
explicitly requested.

## tmux Controls

Autopilot is controlled through tmux keybindings and pane clients.

- `Ctrl-B` then `0..9`: switch to tmux window N.
- `Ctrl-B` then `r`: abort the current test (user abort) and start recovery boot.
- `Ctrl-B` then `d`: detach from the running session.
- Type directly in a source window to send raw input to that source.

`map_window` steps remain in chains and are converted at runtime to tmux window
bindings for backward compatibility.

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

Autopilot publishes runtime state and tmux renders the status line. It shows:
- Current step name
- Request ID and profile
- Chain/subchain name and elapsed time
- Active source/window mapping

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

## Guest Device Tree Artifacts (Automatic)

After every processed request, Autopilot scans captured logs for `DTB_DUMP_START`
and `DTB_DUMP_END` markers and writes decoded artifacts to:

- `results/<timestamp>/device-trees/*.dtb`
- `results/<timestamp>/device-trees/*.dts` (via `dtc`)
- `results/<timestamp>/device-trees/summary.json`

If no DTB dump markers are found, `summary.json` still exists and reports zero
extracted dumps.

When a run has guest behavior problems (boot failure, missing device, guest
panic, unexpected timeout), **always inspect the generated `.dts` files** as a
first-line debugging step.

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
3. If guest boot behavior is wrong, inspect `results/<timestamp>/device-trees/*.dts`.

### SSH Upload Fails
1. Check SSH auth: `ssh root@192.168.101.112 hostname`.
2. Confirm network connectivity and correct target IP.

### TTY Missing
1. `ls /dev/ttyACM*`
2. Replug USB serial adapters if needed.

### Recovery Boot Not Completing
1. Verify recovery chain definition in the profile.
2. Check `results/<timestamp>/chain.json` fork status.
