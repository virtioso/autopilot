# Autopilot System Architecture

**Last Updated**: 2026-02-11

## Purpose

Autopilot is a host-side orchestration service for automated boot testing on an
NVIDIA Orin AGX target. It provides a chain-based state machine that drives
boot sequences, uploads, and log collection, with parallel recovery and tmux-native
operator interaction.

## Architecture Overview (Chain-Based)

- Requests are JSON files that reference a root chain name in `profile`.
- Chain files live in `<code_root>/chains`.
- The chain runner executes steps and routes based on regex outcomes.
- Chains can invoke other chains:
  - `task_spawn` for asynchronous execution
  - `call_chain` for synchronous inline execution
  - `split`/`join` for concurrent groups with reducer-based join
  - `task_spawn`/`task_join` for daemon-scoped background task orchestration
  - `signal_set`/`signal_wait` for daemon-scoped inter-thread signaling
- UART sources are dynamically mapped at runtime via chain steps.
- Parallel task bookkeeping is moving to persistent daemon-level registries with
  explicit lifecycle states and signal/event coordination.

## Core Components

### 1) Orchestrator: `orin_kernel_autopilot.py`

**Responsibilities**
- Polls `requests/pending/` for new requests.
- Loads root chains and runs them through the chain runner.
- Writes results and `chain.json` into `results/<timestamp>/`.
- Runs a startup chain on launch to establish default source/window mappings.
- Publishes tmux UI state and exposes a local control socket for abort/input.

### 2) Chain Runner

**Key concepts**
- **Step**: A unit of work such as `boot_menu`, `wait_pattern`, `upload_efi`.
- **Outcome**: Regex match on a source, routes to the next step.
- **Named Chain**: A reusable chain file addressable by name.

**Data outputs**
- `chain.json`: structured step results, outcomes, error codes, log offsets,
  and parallel reducer/cancellation metadata.
- Status model separates `test_verdict` from `workflow_state` so post-test
  housekeeping can run without changing test outcome.

### 3) Boot Harnesses

Low-level UART and boot control utilities are still used:
- `BootHarness.py`
- `seL4BootHarness.py`

These provide serial handling and existing boot helpers, while control flow is
now driven by chain steps.

### 4) Board Control

- `BoardControlLocal` (default): uses `usbrelay_py` to toggle power/reset.
- `BoardControlRemote` (optional): SSH to a boot server.

### 5) Client + MCP Integration

- `sel4_client.py` provides CLI/API for submitting requests.
- `sel4_mcp_server.py` exposes the same to AI tools.

## Diagrams

PlantUML sources live in `docs/diagrams/`.

- `docs/diagrams/chain-overview.puml`
- `docs/diagrams/uart-source-mapping.puml`
- `docs/diagrams/fork-join-recovery.puml`
- `docs/diagrams/tui-windows.puml`
- `docs/diagrams/startup-chain.puml`

## Data Flow

1. Request is read from `requests/pending`.
2. Root chain is loaded from `chains/<profile>.json`.
3. Chain is validated.
4. Main chain runs as test logic only (no prepare lifecycle orchestration in request chains).
5. Results are written to `results/<ts>/` and `chain.json` is finalized.

Prepare lifecycle is daemon-owned:
- startup probe chain checks stock readiness,
- post-request prepare chain runs between requests,
- queue admission is gated by daemon prepare state.

## Result Artifacts

Single-run output:
- `console/<source>.jsonl` (raw UART transcripts)
- Additional logs under `console/` as defined by chain steps
- `chain.json` (structured step results)

## tmux UI Behavior

When started in tmux:
- `Ctrl-B` then `0..9` switches windows.
- `Ctrl-B` then `r` sends abort and starts recovery.
- status is rendered via `runtime/ui/state.json`.

`map_window` maps logical sources to tmux windows at runtime.
