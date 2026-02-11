# Chain Specification

**Last Updated**: 2026-02-11

This document defines the JSON schema used for chain-based execution.

## Chain Files

Executable chains are stored as one file per chain under:

- `/home/hlyytine/autopilot/chains/<name>.json`

The request field `profile` selects the root chain name.
For example, `"profile": "vm-qemu-virtio"` loads:
`/home/hlyytine/autopilot/chains/vm-qemu-virtio.json`.

## Chain Object

Required fields:
- `entry`: step label to start with.
- `steps`: dictionary of step definitions.

There is no `subchains` object. Reuse is done by referencing other chain files
with `fork` (async) or `call_chain` (sync).

## Step Definition

Common fields:
- `type`: step type string.
- `timeout_s`: integer timeout (seconds).
- `on_timeout`: label to transition on timeout.
- `on_error`: label to transition on exception (optional).
- `outcomes`: list of outcomes (regex or action results).

Interactive console fields:
- `hold_open`: if false, create sessions and return immediately.
- `exit_after_shell`: if true, only exit after a shell prompt was seen.

Step-specific parameters vary by type.

## Outcome Definition

Required:
- `label`: outcome name.
- `next`: target step label.

If regex-based:
- `pattern`: regex to match.
- `source`: logical source name (e.g., `tty0`, `vm0`).

## Step Types (v2)

Action steps:
- `relay`
- `boot_menu`
- `uefi_shell_run`
- `wait_pattern`
- `upload_kernel`
- `upload_efi`
- `reboot`
- `ssh_cmd`
- `map_source`
- `map_window`
- `send_cmd`
- `interactive_console`
- `analyze_logs`
- `fork`
- `call_chain`
- `join`

Terminal steps:
- `pass`
- `fail`

## Chain Reference Rules

For steps that reference another chain (`fork`, `call_chain`):
- `chain` must be a bare chain name (no path, no `.json`).
- Resolution path is fixed: `/home/hlyytine/autopilot/chains/<name>.json`.
- Names must match `[A-Za-z0-9._-]+`.

## Example: fork

```json
{
  "type": "fork",
  "chain": "recovery_boot",
  "outcomes": [
    { "label": "started", "next": "parse_results" }
  ],
  "on_timeout": "parse_results"
}
```

## Example: call_chain

```json
{
  "type": "call_chain",
  "chain": "bootefi_common",
  "outcomes": [
    { "label": "pass", "next": "wait_sel4" },
    { "label": "fail", "next": "fail" }
  ],
  "on_timeout": "fail"
}
```

`call_chain` runs the target chain synchronously in the same request context.
The calling step must define outcomes for `pass` and `fail` labels.
