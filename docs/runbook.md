# Autopilot Runbook

**Last Updated**: 2026-02-11

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

- Code: `<code_root>`
- Working dir: `${AUTOPILOT_DIR:-/home/hlyytine/tii-sel4/autopilot}`
- Requests: `${AUTOPILOT_DIR}/requests`
- Results: `${AUTOPILOT_DIR}/results`
- Runtime state: `${AUTOPILOT_DIR}/runtime`
- Profiles: `<code_root>/profiles` (code repo, single source of truth)

Defaults for `AUTOPILOT_DIR`, TTYs, and queue names are defined in `config.py` (SSOT).

## Start the Autopilot Daemon

```bash
cd <code_root>
AUTOPILOT_PLATFORM=orin-agx-uefi-netboot \
AUTOPILOT_DIR=/home/hlyytine/tii-sel4/autopilot \
python3 orin_kernel_autopilot.py
```

Confirm it prints:
- `Watching: .../requests/pending`
- `Results:  .../results`

For Orin AGX, this platform setting is mandatory. Autopilot runs
`chains/platform-init-<platform>.json` during startup. This chain can set
runtime overrides (for example chain aliases).

### Start via MCP (Headless + tmux UI)

Use the MCP tools to start Autopilot in a detached tmux session:

```json
{
  "tool": "autopilot_start",
  "autopilot_dir": "/home/hlyytine/tii-sel4/autopilot",
  "tty0": "/dev/ttyACM0",
  "tty1": "/dev/ttyACM1"
}
```

The response includes an `attach_hint`, typically:

```bash
tmux attach -t autopilot
```

When submitting tests via MCP, the server will auto-start Autopilot if it is
not running, so you usually do not need to start it manually.

**Orin AGX note**: The MCP start path sets `AUTOPILOT_TTY0=/dev/ttyACM0` and
`AUTOPILOT_TTY1=/dev/ttyACM1` by default, and defaults
`AUTOPILOT_PLATFORM=orin-agx-uefi-netboot` when not already set. These Orin
defaults must be replaced for other platforms (e.g. Raspberry Pi 4 uses
`/dev/ttyUSB*` and a different platform override chain).
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
  "autopilot_dir": "/home/hlyytine/tii-sel4/autopilot",
  "tty0": "/dev/ttyACM0",
  "tty1": "/dev/ttyACM1"
}
```

### Codex MCP Examples

When calling from Codex, use the fully qualified MCP tool names:

```python
mcp__sel4-autopilot__autopilot_start(
    autopilot_dir="/home/hlyytine/tii-sel4/autopilot",
    tty0="/dev/ttyACM0",
    tty1="/dev/ttyACM1"
)

mcp__sel4-autopilot__autopilot_restart(
    autopilot_dir="/home/hlyytine/tii-sel4/autopilot",
    tty0="/dev/ttyACM0",
    tty1="/dev/ttyACM1"
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

### Canonical EFI/Stock Boot Chains

- `boot_stock_linux`: canonical stock Linux bring-up path
  (`boot_efi(mode=extlinux)` + prompt wait + `ssh_wait_ready`).
- `deploy_and_boot_test_efi`: canonical test EFI deployment/boot path
  (SCP to `/efiboot/{target_binary_name}` + SSH reboot + `boot_efi(mode=test_efi)`).

For Orin AGX (`AUTOPILOT_PLATFORM=orin-agx-uefi-netboot`), `boot_efi` includes
reset-line orchestration before command dispatch:
- assert reset line,
- wait for UART quiescence on mapped sources,
- wait an additional fixed delay,
- deassert reset line,
- wait for startup marker and shell prompt on `tty0`,
- send Enter at startup marker and then dispatch the EFI command.

### Prepare Lifecycle (Daemon-Owned)

Prepare lifecycle is daemon-managed, not request-chain managed.

- Request/test chains must not use prepare lifecycle coupling primitives:
  - `task_spawn task=prepare_next_run`
  - `signal_set signal=prepare_next_run_go`
  - `task_join` including `prepare_next_run`
- Startup probe and post-request prepare flow are selected from platform policy:
  - `lifecycle.prepare.probe_chain`
  - `lifecycle.prepare.run_chain`
- Queue admission is gated by daemon prepare state (`pass` required unless
  platform policy explicitly allows degraded admission).

### Parallel Groups

For fail-fast watchdog scenarios, use `split` + `join`:
- `split` starts named branch chains concurrently.
- `join` joins named groups and applies reducer policy:
  - `reduce=any_pass`: pass if any joined branch passes; fail if all fail.
  - `reduce=all_pass`: fail if any joined branch fails; pass only if all pass.
- join targets are strict: missing/unknown groups are validation/startup errors.
- cancellation of non-winning branches depends on reducer policy.

Monitor policy:
- monitor branches must be fail-only,
- monitor branches must never contain terminal `pass`,
- runtime validation rejects monitor branches that can reach `pass`.

For single-source ordered classification (for example, consuming leading
whitespace and then branching on the first non-whitespace prefix), prefer the
`case` step over parallel groups.

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

Interactive chains set verdict and terminate (`pass`/`fail`) without
prepare-lifecycle steps; daemon lifecycle handling runs outside the request
chain.

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
- Current chain name and elapsed time
- Active source/window mapping

## Error Codes in chain.json

`chain.json` includes a standard error code for each step failure:

- `timeout`
- `regex_miss`
- `user_abort`
- `exception`
- `canceled`
- `validation_error`

`console/autopilot.fail.log` also carries classification markers. For failed VM
boot starts, `AUTOPILOT_FAIL: BINARY_NOT_UPLOADED` means UEFI reported
`is not recognized as an internal or external command` when launching the EFI
binary.

For parallel groups, `chain.json` also includes `parallel_groups` with:
- split origin metadata (`split_step`, `split_chain`)
- winner metadata (`branch`, `status`, `finished_at`)
- branch state metadata (`chain`, `monitor`, `status`, `finished_at`, `cancel_reason`)
- join decision metadata (`step`, `chain`, `reduce`, `decision`, `joined_groups`)

When a non-winner branch is canceled after winner latch, `cancel_reason` is set
to `winner:<branch-name>`.

Autopilot status also distinguishes:
- `test_verdict`: authoritative test result
- `workflow_state`: execution lifecycle (can remain active for housekeeping)

Chains can set verdict before workflow completion via:
- `set_test_verdict` step (`verdict=pass|fail`)

For daemon-scoped background orchestration, chains can use:
- `task_spawn` / `task_join` for persistent named background tasks
- `signal_set` / `signal_wait` for inter-thread signaling

## Human-Readable Chain Artifacts

Use `scripts/chain_humanize.py` to generate chain-like JSON files with inline
decoded console snippets derived from recorded offsets.

Default batch mode (recommended):

```bash
scripts/chain_humanize.py --result-dir results/<timestamp>
```

This scans `results/<timestamp>/chain*.json` and writes sibling files:

- `chain.human.json`
- `chain.task.<name>.human.json`
- `chain.parallel.<group>.<branch>.human.json`

Single-file mode:

```bash
scripts/chain_humanize.py --chain-file results/<timestamp>/chain.json
```

If step-level `source_ranges` metadata exists, the script adds `source_snippets`
with decoded text for each source range. If only legacy `log_offset` metadata
is present, it adds `match_snippet` using a context window around that offset.

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

## Chain Files (Where to Edit)

Executable chains live in `<code_root>/chains` as one file per chain (`<name>.json`).

Each request field `profile` selects the root chain file by name (`chains/<profile>.json`).

Reusable flow is expressed by referencing other chain files via `task_spawn`,
`call_chain`, and parallel group branches.

Console login/prompt profiles remain in `<code_root>/profiles` (for example
`linux-yocto.json`, `ubuntu-22.json`).

Example step types:
- `relay`
- `boot_menu`
- `wait_pattern`
- `case`
- `upload_kernel`
- `upload_efi`
- `upload_file`
- `reboot`
- `map_source`
- `map_window`
- `send_cmd`
- `interactive_console`
- `task_spawn`, `task_join`, `signal_set`, `signal_wait`, `call_chain`
- `split`, `join`
- `set_overrides`
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
2. Check `results/<timestamp>/chain.json` task and parallel-group status.
