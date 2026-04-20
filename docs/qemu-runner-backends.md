# QEMU Runner Backends

Autopilot supports QEMU-backed targets by invoking the workspace-owned manual
runner instead of embedding separate QEMU launch knowledge.

Manual runner SSOT:

- `/home/hlyytine/tii-sel4/projects/virtioso-camkes-vm/tools/qemu_runner.py`

## Backend Model

- `orinagx`
  - hardware UART-backed sources
  - stock-Linux prepare-next-run lifecycle
- `qemu_arm64_defconfig`
  - local subprocess-backed source
  - launches the manual runner with `run-local`
  - no relay/UART recovery path
- `qemu_x86_64_defconfig`
  - SSH subprocess-backed source
  - launches the manual runner with `run-remote`
  - no relay/UART recovery path

## Runtime Seams

The runtime now supports two source mapping styles:

- `map_source`
  - serial-backed source using a tty path
- `map_command_source`
  - process-backed source using a command list
  - stdout and stderr are merged into the source log
  - non-zero process exit emits `AUTOPILOT_FAIL: PROCESS_EXIT_NONZERO`

This keeps chain-level verdict logic the same across hardware and QEMU
backends: chains still wait for pass/fail markers in a named source.

## Daemon Startup

QEMU-backed profiles start autopilot in `qemu-generic` mode.

- platform init chain: `platform-init-qemu-generic.json`
- startup probe chain: `post_test_fallback_noop`
- prepare chain: `post_test_fallback_noop`

This avoids the Orin-specific stock-Linux probe and relay-reset lifecycle when
the daemon is only being used for QEMU-backed runs.
