# Autopilot System Architecture

**Last Updated**: 2026-02-05

## Purpose

Autopilot is a host-side orchestration service for automated boot testing on an
NVIDIA Orin AGX target. It provides a chain-based state machine that drives
boot sequences, uploads, and log collection, with parallel recovery and an
interactive TUI for operators.

## Architecture Overview (Chain-Based)

- Requests are JSON files that reference a **profile**.
- Profiles define a **chain**: steps, outcomes, and optional subchains.
- The chain runner executes steps and routes based on regex outcomes.
- A forked recovery chain can run in parallel while logs are parsed.
- UART sources are dynamically mapped at runtime via chain steps.

## Core Components

### 1) Orchestrator: `orin_kernel_autopilot.py`

**Responsibilities**
- Polls `requests/pending/` for new requests.
- Loads profile chains and runs them through the chain runner.
- Writes results and `chain.json` into `results/<timestamp>/`.
- Runs a startup chain on launch to establish default source/window mappings.

### 2) Chain Runner

**Key concepts**
- **Step**: A unit of work such as `boot_menu`, `wait_pattern`, `upload_efi`.
- **Outcome**: Regex match on a source, routes to the next step.
- **Subchain**: A named chain launched via `fork` for parallel recovery.

**Data outputs**
- `chain.json`: structured step results, outcomes, error codes, and log offsets.

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

## Data Flow (New Model)

1. Request is read from `requests/pending`.
2. Profile chain is validated.
3. Startup chain runs once (on daemon start).
4. Main chain runs with optional forked recovery boot.
5. Results are written to `results/<ts>/` and `chain.json` is finalized.

## Result Artifacts

Single-run output:
- `console/<source>.jsonl` (raw UART transcripts)
- Additional logs under `console/` as defined by the profile chain
- `chain.json` (structured step results)
- `console/*.jsonl` (source logs when mapped)

## TUI Behavior

If Autopilot has a TTY:
- `Ctrl-A` then `1..9` switches windows.
- `Ctrl-A` then `W` shows window list.
- `Ctrl-A` then `X` exits UI.
- `Ctrl-A` then `R` aborts the current test and starts recovery.

If no TTY is present, the UI is disabled.
