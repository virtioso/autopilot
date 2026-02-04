# Autopilot Runbook

**Last Updated**: 2026-02-04

This runbook provides step-by-step operational and developer procedures for
running the Autopilot service, submitting tests, collecting results, and
troubleshooting. It also documents the planned interactive console workflow
for AI tools.

## Prerequisites

1. Target board reachable over SSH at `192.168.101.112`.
2. UART devices present on host:
   - `/dev/ttyACM0` (main console)
   - `/dev/ttyACM1` (secondary console)
3. USB relay accessible for power/reset control (`usbrelay_py`).
4. Python dependencies installed: `pexpect`, `pyserial`, `usbrelay_py`.

## Start the Autopilot Daemon

1. Open a terminal on the host.
2. Start the service:

```bash
cd /home/hlyytine/pkvm/jetson-pkvm/autopilot
python3 orin_kernel_autopilot.py
```

3. Confirm it prints:
   - `Watching: .../requests/pending`
   - `Results:  .../results`

## Stop the Autopilot Daemon

1. Press `Ctrl+C` in the running terminal.
2. Autopilot will move any `processing` requests back to `pending`.

## Submit a Linux Kernel Test (Single-Run)

1. Ensure the kernel image is built:
   - `KERNEL_IMAGE` comes from `WORKSPACE/Linux_for_Tegra/source/kernel/linux/.../Image`.
2. Create a request file:

```bash
cd /home/hlyytine/pkvm/jetson-pkvm/autopilot
TS=$(date +%Y%m%d-%H%M%S)
cat > requests/pending/${TS}.request <<'EOF'
{
  "type": "linux",
  "description": "single-run kernel test"
}
EOF
```

3. Wait for completion. Results are in `results/<timestamp>/`.

## Submit a Linux Kernel Test (Multi-Run)

```bash
cd /home/hlyytine/pkvm/jetson-pkvm/autopilot
TS=$(date +%Y%m%d-%H%M%S)
cat > requests/pending/${TS}.request <<'EOF'
{
  "type": "linux",
  "multi_run": true,
  "run_count": 5,
  "description": "multi-run kernel test"
}
EOF
```

Results will be in `results/<timestamp>/run_<n>/` plus `summary.json`.

## Submit a seL4 EFI Test (Single-Run)

Using the client library/CLI:

```bash
cd /home/hlyytine/pkvm/jetson-pkvm/autopilot
./sel4_client.py submit /path/to/sel4test.efi --name sel4test.efi --arm-hyp --wait
```

Or with a manual request file:

```bash
TS=$(date +%Y%m%d-%H%M%S)
cat > requests/pending/${TS}.request <<'EOF'
{
  "type": "sel4",
  "binary_path": "/absolute/path/to/sel4test.efi",
  "binary_name": "sel4test.efi",
  "description": "seL4 EFI test"
}
EOF
```

## Submit a seL4 EFI Test (Multi-Run)

```bash
./sel4_client.py submit-multi /path/to/sel4test.efi --runs 5 --type sel4 --arm-hyp --wait
```

## Submit a vm_minimal Test

```bash
./sel4_client.py submit-vm /path/to/capdl-vm_minimal.efi --wait
```

Results include:
- `sel4.log` (capdl loader output)
- `vm.log` (VM console output)

## Check Status and Fetch Logs

```bash
# List requests
./sel4_client.py list --pending --completed --failed

# Check status
./sel4_client.py status 20251212-143022

# Fetch logs
./sel4_client.py log 20251212-143022
./sel4_client.py log 20251212-143022 --raw
```

## Planned: Interactive Console Sessions (AI Tools)

This section documents planned behavior for interactive shell sessions. It is
not implemented yet.

### Concept
- Autopilot boots a target (stock Linux or VM).
- It opens one or more UART sessions.
- AI tools (via MCP) send commands and poll output.
- Transcripts are persisted in `results/<timestamp>/console/`.

### Planned Request Format
```json
{
  "type": "boot_interactive",
  "boot_target": "stock_linux",
  "interactive": {
    "enabled": true,
    "phase": "post_boot",
    "sessions": [
      { "name": "vm0", "port": "/dev/ttyACM0", "profile": "linux-yocto" },
      { "name": "vm1", "port": "/dev/ttyACM1", "profile": "linux-yocto" }
    ],
    "idle_timeout_s": 900
  }
}
```

### Planned MCP Operations
- `open_console_session`
- `send_console_command`
- `read_console_output`
- `close_console_session`
- `list_console_sessions`

## Troubleshooting

### UEFI Navigation Fails
1. Inspect `uart-raw.log` for actual menu output.
2. Confirm the expected UEFI prompts are unchanged.

### SSH Upload Fails
1. Check SSH key auth: `ssh root@192.168.101.112 hostname`.
2. Ensure network connectivity to target.

### UART Device Missing
1. Check `ls /dev/ttyACM*`.
2. Replug USB serial adapters if needed.

### Board Stuck or Unresponsive
1. Power cycle using the USB relay.
2. If still stuck, manual reset and re-run.

## Recovery Procedure

1. Stop autopilot.
2. Power-cycle the board.
3. Start autopilot again.
4. Re-submit the failed request.

