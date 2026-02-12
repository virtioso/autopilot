# Autopilot Code DRY/SSOT Findings

Date: 2026-02-12
Scope: `/home/hlyytine/autopilot` codebase (Python runtime and MCP server)

## Summary

This document tracks concrete DRY and SSOT violations found in Autopilot code (not docs-only drift).

## Findings

### C-001 (High): `autopilot_status` has duplicate tool definitions and conflicting semantics

- Type: SSOT violation, behavior ambiguity, dead code
- Impact:
  - MCP tool contract is ambiguous because `autopilot_status` is declared twice with different meanings.
  - One handler branch is unreachable, increasing maintenance risk and confusion.
- Evidence:
  - `sel4_mcp_server.py:280` defines `autopilot_status` as queue summary.
  - `sel4_mcp_server.py:551` defines `autopilot_status` again as daemon status.
  - `sel4_mcp_server.py:738` handles `autopilot_status` via `get_autopilot_status` (queue summary).
  - `sel4_mcp_server.py:1004` second `autopilot_status` branch (daemon status) is unreachable.
- Recommended direction:
  - Split into distinct tool names and keep a single definition per behavior, e.g.:
    - `autopilot_queue_status` -> queue/request status
    - `autopilot_daemon_status` -> daemon process/tmux status
  - Keep `autopilot_status` as backward-compatible alias temporarily, with explicit deprecation note.

### C-002 (High): Relocatability breaks due to hardcoded absolute paths in runtime code

- Type: SSOT violation for path resolution
- Impact:
  - Moving/renaming workspace breaks runtime behavior despite prior portability goals.
- Evidence:
  - `config.py:7` default `AUTOPILOT_DIR` hardcoded to `/home/hlyytine/tii-sel4/autopilot`.
  - `sel4_mcp_server.py:637` hardcoded build config path `/home/hlyytine/tii-sel4/orinagx_sel4test/.config`.
  - `sel4_mcp_server.py:829` hardcoded ftrace query tool path `/home/hlyytine/tii-sel4/kernel/tools/ftrace_indexed.py`.
  - `orin_kernel_autopilot.py:24` and `disasm_2nd_frame.py:55` fallback `WORKSPACE` hardcoded to `/home/hlyytine/pkvm`.
- Recommended direction:
  - Centralize path discovery in one helper module (single source of truth) and reference it everywhere.
  - Prefer:
    - explicit request/env override,
    - then derived paths from code root/workspace root,
    - then documented fallback.

### C-003 (Medium): Utility scripts duplicate workspace-specific defaults

- Type: DRY + SSOT drift risk
- Impact:
  - Same path knowledge duplicated across tools; updates are easy to miss.
- Evidence:
  - `analyze_sel4log.py:20` and `analyze_sel4log.py:21` hardcoded ELF defaults.
  - `extract_ftrace.py:234` and `extract_ftrace.py:235` hardcoded indexer locations.
- Recommended direction:
  - Route these defaults through shared config/path helpers.
  - Keep CLI overrides, but remove host-specific literals from script internals.

## Prioritization

1. C-001 (tool semantics SSOT): fixes API contract ambiguity and dead branches.
2. C-002 (absolute path SSOT): mandatory for portability and future repo moves.
3. C-003 (script DRY): cleanup after SSOT path helpers are established.

## Tracking

- Status: Open
- Next action: implement C-001, then C-002, then C-003 in separate atomic commits.
