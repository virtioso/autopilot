# Chain Specification

**Last Updated**: 2026-02-11

This document defines the JSON schema used for chain-based execution.

## Chain Files

Executable chains are stored as one file per chain under:

- `<code_root>/chains/<name>.json`

The request field `profile` selects the root chain name.
For example, `"profile": "vm-qemu-virtio"` loads:
`<code_root>/chains/vm-qemu-virtio.json`.

## Chain Object

Required fields:
- `entry`: step label to start with.
- `steps`: dictionary of step definitions.

There is no `subchains` object. Reuse is done by referencing other chain files
with `task_spawn` (async), `call_chain` (sync), and parallel-group steps.

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
- `call_chain`
- `split`
- `join`
- `task_spawn`
- `task_join`
- `signal_set`
- `signal_wait`
- `set_overrides`
- `set_test_verdict`

Terminal steps:
- `pass`
- `fail`

## Chain Reference Rules

For steps that reference another chain (`task_spawn`, `call_chain`):
- `chain` must be a bare chain name (no path, no `.json`).
- Resolution path is fixed: `<code_root>/chains/<name>.json`.
- Names must match `[A-Za-z0-9._-]+`.

For `split` branch entries:
- each branch must include `name` and `chain`,
- branch names must be unique within the split step,
- monitor branches must be explicitly marked with `"monitor": true`.

Monitor branch policy:
- monitor branches are fail-only,
- monitor branches must not contain terminal `pass` steps (directly or through `call_chain`),
- validation rejects monitor branches that can reach `pass`.

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

## Example: split + join

```json
{
  "type": "split",
  "group": "vm_boot_and_ftrace_watch",
  "branches": [
    { "name": "main_vm_boot", "chain": "vm_wait_boot_qemu_virtio" },
    { "name": "ftrace_watchdog", "chain": "monitor_ftrace_storage_full", "monitor": true }
  ],
  "outcomes": [
    { "label": "ok", "next": "join_vm_boot" }
  ],
  "on_timeout": "fail"
}
```

```json
{
  "type": "join",
  "join_groups": ["vm_boot_and_ftrace_watch"],
  "reduce": "any_pass",
  "timeout_s": 300,
  "outcomes": [
    { "label": "pass", "next": "filter_logs_pass" },
    { "label": "fail", "next": "filter_logs_fail" }
  ],
  "on_timeout": "filter_logs_fail"
}
```

Parallel-group semantics:
- branch chains execute concurrently after `split`,
- `join` joins one or more named groups and applies a reducer,
- reducer `any_pass`: returns `pass` when any joined branch passes; otherwise `fail` when all joined branches fail,
- reducer `all_pass`: returns `fail` when any joined branch fails; otherwise `pass` when all joined branches pass,
- missing/unknown join targets are validation errors (strict mode),
- non-winner branch cancellation is reducer/policy dependent.

## Join Validation Rules

For `join`:
- use `join_groups` (non-empty list of group names),
- use `reduce` (`any_pass` or `all_pass`),
- `join_groups` targets must exist and be valid for the workflow scope,
- unknown or missing join targets are rejected at validation/startup.

## Task Registry and Signals

Autopilot runtime provides daemon-scoped task and signal registries.

- task registry lifetime is the daemon process lifetime (survives individual requests),
- signal registry lifetime is the daemon process lifetime,
- `task_join` is strict: all referenced tasks must exist.

### `task_spawn`

Required fields:
- `task`: task name
- `chain`: chain name to execute asynchronously

Starts/replaces a named daemon-scoped task entry and runs the chain in a
background thread.

### `task_join`

Required fields:
- `tasks`: non-empty list of task names
- `reduce`: `any_pass` or `all_pass`

Reducer semantics:
- `any_pass`: pass if any task passes; fail if all tasks fail/cancel.
- `all_pass`: fail if any task fails; pass only if all tasks pass.

### `signal_set`

Required fields:
- `signal`: signal name

Increments signal counter and wakes waiting `signal_wait` steps.

### `signal_wait`

Required fields:
- `signal`: signal name

Optional fields:
- `consume`: bool (default `true`)

Waits for signal counter to become non-zero (within step timeout).
If `consume=true`, decrements counter when matched.

## Runtime Trace Metadata (`chain.json`)

When parallel groups are used, `chain.json` includes:
- `parallel_groups.<group>.split_step`, `parallel_groups.<group>.split_chain`:
  - split origin step/chain for topology-aware tooling
- `parallel_groups.<group>.winner`:
  - `branch`, `status`, `finished_at`
- `parallel_groups.<group>.branches.<name>`:
  - `chain`, `monitor`, `status`, `finished_at`, `cancel_reason`
- `parallel_groups.<group>.join`:
  - `step`, `chain`, `reduce`, `decision`, `joined_groups`

`decision` contains reducer output evidence used by `join`.

`cancel_reason` is set when a branch is canceled due to another branch winning
(for example `winner:ftrace_watchdog`).

## Verdict and Workflow State

Runtime reporting separates:
- `test_verdict`: authoritative test result (`pass`/`fail`),
- `workflow_state`: runtime lifecycle (`running`/`housekeeping`/`completed`/`failed`).

This allows housekeeping/preparation flows to continue after verdict is known.

## Example: set_test_verdict

```json
{
  "type": "set_test_verdict",
  "verdict": "fail",
  "outcomes": [
    { "label": "ok", "next": "prepare_next_run" }
  ],
  "on_timeout": "prepare_next_run"
}
```

## Upload Methods

`upload_kernel` and `upload_efi` support:

- `method: "scp"` (default): copy to remote target via SCP.
- `method: "local_copy"`: copy on the host filesystem to `target_path`.

`local_copy` is useful for host-local deployment targets such as TFTP roots.

## Platform Overrides

`set_overrides` updates runtime override state (deep merge). This can be used in
platform-init chains to establish behavior before request chains start.

Current override keys:

- `chain_aliases`: map requested chain name to an alternate chain name.
