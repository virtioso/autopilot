# Chain Specification

**Last Updated**: 2026-02-05

This document defines the JSON schema used for chain-based execution.

## Top-Level Profile

```json
{
  "name": "linux-yocto",
  "chain": {
    "entry": "boot_test",
    "steps": {
      "boot_test": { "...": "..." }
    },
    "subchains": {
      "recovery_boot": { "...": "..." }
    }
  }
}
```

## Chain Object

Required fields:
- `entry`: step label to start with.
- `steps`: dictionary of step definitions.

Optional:
- `subchains`: named chain definitions referenced by `fork`.

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

## Step Types (v1)

Action steps:
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
- `analyze_logs`
- `fork`
- `join`

Terminal steps:
- `pass`
- `fail`

## Example: map_source

```json
{
  "type": "map_source",
  "tty": "/dev/ttyACM0",
  "source": "vm0",
  "log": "console/vm0.jsonl",
  "mode": "append",
  "outcomes": [
    { "label": "ok", "next": "wait_vm0_login" }
  ],
  "on_timeout": "fail"
}
```

## Example: map_window

```json
{
  "type": "map_window",
  "window": 1,
  "source": "vm0",
  "title": "VM0 Console",
  "outcomes": [
    { "label": "ok", "next": "wait_vm0_login" }
  ],
  "on_timeout": "fail"
}
```

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
