# AGENTS.md

This file provides guidance to Codex (and other coding agents) for working on
the Autopilot codebase.

## Mandatory Preflight (Before Any Planning or Implementation)

Always read the following documents before you start planning or making changes:

1. `docs/README.md`
2. `docs/overview.md`
3. `docs/runbook.md`
4. `docs/chain-spec.md`
5. `docs/ai-interactive-console.md`

## Working Directory vs Code

`AUTOPILOT_DIR` is the working directory (queues/results/runtime), not the code
path. Executable chains live in the code repo at
`<code_root>/chains`. Console login/prompt profiles live at
`<code_root>/profiles`. The code can live anywhere (for example
`$WORKSPACE/tools/autopilot`), while each project uses its own `AUTOPILOT_DIR`, typically
`$WORKSPACE/autopilot`. `WORKSPACE` must be set.

## Orin AGX Default Workflow (Mandatory)

For Orin AGX EFI testing, always run Autopilot with:

- `AUTOPILOT_PLATFORM=orin-agx-uefi-netboot`

This enforces the platform-init chain for Orin AGX behavior. Current EFI
deployment flow is SCP upload to `/efiboot/{target_binary_name}` + SSH reboot +
UEFI boot command dispatch via `boot_efi` chain step.

## Agent-Facing Autopilot API

The only supported agent-facing control surface is the `autopilot` command in
`PATH`. It returns JSON and is the API agents must use for lifecycle, submit,
status, evidence, and logs.

Required pattern:

```bash
autopilot --autopilot-dir "$WORKSPACE/autopilot" <command> --json
```

Do not use MCP tools, direct request/result queue edits, or Python internals as
fallback paths. If the command API fails, returns invalid/ambiguous JSON, or
lacks a needed operation, stop and ask the human owner.

Orin AGX note: `orin-agx-uefi-netboot` defaults to `/dev/ttyACM0` and
`/dev/ttyACM1` when UARTs are not specified. Replace these explicitly for other
platforms (e.g. Raspberry Pi 4 uses `/dev/ttyUSB*` and a different platform
override chain).

For failed tests, start with:

```bash
autopilot --autopilot-dir "$WORKSPACE/autopilot" get <request-id> --json
autopilot --autopilot-dir "$WORKSPACE/autopilot" evidence <request-id> --json
```

Use bounded log reads, not full raw dumps:

```bash
autopilot --autopilot-dir "$WORKSPACE/autopilot" logs <request-id> --grep 'AUTOPILOT_FAIL|ERROR' --tail 100 --json
```

On daemon startup, Autopilot clears all pending and processing requests before
accepting new work. Old queue entries are not durable intent after restart.

## When Working From Another Project

If you are running Autopilot from a different project repo, ensure that project
has its own `AGENTS.md` referencing the Autopilot docs above and that it defines
`AUTOPILOT_DIR` for that project.

## Guest DTB Artifacts (Mandatory Workflow)

After every test run, Autopilot extracts guest DTB dumps from logs into
`results/<request_id>/device-trees/` and converts them to `.dts` when possible.

If guest behavior is abnormal, always inspect the generated `.dts` files as part
of first-pass triage.

## Parallel Chain Policy (Mandatory)

- Use `split` + `join` for fail-fast concurrent monitoring.
- `join` returns the first terminal branch result (`pass` or `fail`).
- Monitor branches must be fail-only by design.
- Monitor branches must never contain terminal `pass` steps.
- Runtime validation rejects monitor branches that can reach `pass`.
- For single-source ordered classification (non-concurrent), use `case`.
