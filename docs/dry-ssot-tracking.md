# DRY and SSOT Tracking

**Created**: 2026-02-12  
**Scope**: `/home/hlyytine/autopilot`  
**Purpose**: Track DRY (Don't Repeat Yourself) and SSOT (Single Source of Truth) findings and remediation progress.

## Status

- Open findings: 0
- In progress: 0
- Resolved: 7

## Baseline Findings (2026-02-12)

### F-001 (High): Daemon launch command path not single-sourced
- **Summary**: Default daemon command hardcodes code path instead of deriving from runtime location.
- **Evidence**:
  - `autopilot_manager.py:16`
  - `AGENTS.md:21` (states code can live anywhere)
- **Risk**: Relocation breaks MCP start/restart defaults unless command is overridden.
- **Proposed direction**: Build default command from `Path(__file__).resolve().parent`.
- **Status**: Resolved

### F-002 (High): Chain steps hardcode absolute script paths
- **Summary**: Multiple chains call analysis scripts with `/home/hlyytine/autopilot/...` absolute paths.
- **Evidence**:
  - `chains/sel4test.json:111`
  - `chains/sel4test.json:126`
  - `chains/vm-minimal.json:144`
  - `chains/vm-minimal.json:159`
  - `chains/vm-qemu-virtio.json:144`
  - `chains/vm-qemu-virtio.json:159`
- **Risk**: Non-relocatable chain execution and repeated literals.
- **Proposed direction**: Introduce a chain-runtime token (for example `{code_dir}`) or env-backed script root.
- **Status**: Resolved

### F-003 (Medium): Target network constants duplicated across codepaths
- **Summary**: Board and target IP values are repeated in multiple modules/scripts.
- **Evidence**:
  - `orin_kernel_autopilot.py:29`
  - `BootHarness.py:357`
  - `seL4BootHarness.py:26`
  - `BoardControl.py:36`
  - `bin/update.sh:5`
- **Risk**: Drift when network topology changes.
- **Proposed direction**: Consolidate into `config.py` and pass through call sites.
- **Status**: Resolved

### F-004 (Medium): VM profile chains duplicate large logic blocks
- **Summary**: `vm-minimal` and `vm-qemu-virtio` repeat large shared blocks, differing mostly in one wait pattern/source.
- **Evidence**:
  - `chains/vm-minimal.json:139`
  - `chains/vm-minimal.json:154`
  - `chains/vm-qemu-virtio.json:139`
  - `chains/vm-qemu-virtio.json:154`
  - Main delta: `chains/vm-minimal.json:132` vs `chains/vm-qemu-virtio.json:132`
- **Risk**: Fixes must be duplicated manually; easy to diverge.
- **Proposed direction**: Extract common subchain and parameterize pattern/source.
- **Status**: Resolved

### F-005 (Medium): Docs and MCP metadata still hardcode install paths
- **Summary**: Runtime now resolves code paths relative to code root, but multiple docs and MCP tool descriptions still present fixed `/home/hlyytine/autopilot` paths as defaults/authoritative locations.
- **Evidence**:
  - `docs/chain-spec.md:11`
  - `docs/chain-spec.md:80`
  - `docs/overview.md:81`
  - `docs/runbook.md:268`
  - `docs/architecture.md:15`
  - `docs/ai-interactive-console.md:43`
  - `sel4_mcp_server.py:442`
  - `sel4_mcp_server.py:491`
  - `orin_kernel_autopilot.py:20`
- **Risk**: SSOT drift and operator confusion when repo is moved/renamed.
- **Proposed direction**: Reword docs and MCP schema text to describe script-relative/code-root behavior; keep absolute paths only as explicitly marked examples.
- **Status**: Resolved

### F-006 (Low): Duplicate tty validation logic in MCP handlers
- **Summary**: `autopilot_start` and `autopilot_restart` each repeat tty argument validation logic.
- **Evidence**:
  - `sel4_mcp_server.py:915`
  - `sel4_mcp_server.py:954`
- **Risk**: Small maintenance overhead.
- **Proposed direction**: Helper function for shared validation/error response.
- **Status**: Resolved

### F-007 (Low): `sel4_client.py` duplicates queue path definitions
- **Summary**: Module-level legacy path constants duplicate path mapping available via `get_paths()`.
- **Evidence**:
  - `sel4_client.py:44`
  - `sel4_client.py:51`
  - `sel4_client.py:86` (already uses `get_paths()`)
- **Risk**: Dual path authority in one module.
- **Proposed direction**: Deprecate/remove legacy constants or confine to compatibility wrapper.
- **Status**: Resolved

## Progress Log

### 2026-02-12
- Created this tracking file.
- Added baseline DRY/SSOT findings F-001 through F-007.
- Added execution plan for absolute-path removal phase (`Plan-AP-1`..`Plan-AP-5`).
- Implemented `config.get_code_root()` as code-root SSOT.
- Updated daemon default command to be code-root derived (no fixed `/home/hlyytine/autopilot`).
- Added runtime format tokens in chain context resolution:
  - `code_root`
  - `chains_dir`
  - `profiles_dir`
- Converted chain script commands from absolute code paths to `{code_root}` token:
  - `chains/sel4test.json`
  - `chains/vm-minimal.json`
  - `chains/vm-qemu-virtio.json`
- Validation:
  - Python syntax check passed for touched modules.
  - `rg '/home/hlyytine/autopilot' /home/hlyytine/autopilot/chains/*.json` returned no matches.
  - Runtime sanity: `sel4test` request `20260212-154345` completed `pass`.
  - Note: request `20260212-154313` failed due stale MCP-server process relaunching daemon without platform env (known independent issue); restarting through updated manager path resolved it.
- Finding status updates:
  - `F-001` -> Resolved
  - `F-002` -> Resolved
- Reformulated `F-005` to cover broader docs + MCP metadata path drift after runtime relocation fixes.
- Implemented network endpoint SSOT in `config.py`:
  - `get_target_ip()`
  - `get_target_user()`
  - `get_boot_control_host()`
- Updated runtime consumers to read centralized values:
  - `orin_kernel_autopilot.py`
  - `BoardControl.py`
  - `BootHarness.py`
  - `seL4BootHarness.py`
  - `bin/update.sh` (env-backed defaults)
- Validation:
  - Python syntax check passed for all touched Python files.
  - `bash -n bin/update.sh` passed.
  - Endpoint literal scan in `*.py` and `bin/*.sh` shows constants centralized in `config.py` and env-default usage in `bin/update.sh`.
- Finding status updates:
  - `F-003` -> Resolved
- Extracted shared VM flow and reduced profile wrappers:
  - Added `chains/vm_common.json`.
  - Added chain-specific wait subchains:
    - `chains/vm_wait_boot_minimal.json`
    - `chains/vm_wait_boot_qemu_virtio.json`
  - Converted wrappers to alias + shared call:
    - `chains/vm-minimal.json`
    - `chains/vm-qemu-virtio.json`
- Removed duplicated MCP tty validation logic:
  - Added `_require_ttys()` helper in `sel4_mcp_server.py`.
  - Reused helper in `autopilot_start` and `autopilot_restart` handlers.
- Removed duplicated queue-path literals in `sel4_client.py`:
  - Module-level compatibility constants now mirror `get_paths()` output.
- Validation:
  - JSON parse check passed for all files in `chains/`.
  - Python syntax check passed for `sel4_mcp_server.py` and `sel4_client.py`.
- Finding status updates:
  - `F-004` -> Resolved
  - `F-006` -> Resolved
  - `F-007` -> Resolved
- Reworded docs and MCP metadata to avoid hardcoded code install paths:
  - Replaced fixed `/home/hlyytine/autopilot` references with `<code_root>` in:
    - `docs/chain-spec.md`
    - `docs/overview.md`
    - `docs/runbook.md`
    - `docs/ai-interactive-console.md`
    - `docs/architecture.md`
    - `sel4_mcp_server.py` (usage/example + tool schema descriptions)
- Validation:
  - `rg '/home/hlyytine/autopilot'` over these docs + MCP server now returns no matches.
- Finding status updates:
  - `F-005` -> Resolved

### 2026-02-13
- Added parallel-chain planning document:
  - `docs/parallel-chains-plan.md`
- Implemented runtime support for concurrent branch groups:
  - new step types in `chain_runtime.py`: `parallel_split`, `parallel_join`
  - first-result-wins winner latching and branch cancellation
  - `chain.json` parallel metadata (`parallel_groups`)
  - monitor branch fail-only validation for `parallel_split` monitor branches
- Updated VM chain flow:
  - `chains/vm_common.json`
  - renamed `wait_capdl` -> `elfloader_started`
  - success marker now `ELF-loader started on CPU`
  - timeout updated to 180s for netboot stage
  - added parallel watchdog branch execution after ELF-loader start
  - added `AUTOPILOT_FAIL: ELFLOADER_NOT_REACHED` classification
- Added fail-only monitor chain:
  - `chains/monitor_ftrace_storage_full.json`
- SSOT docs updated for new semantics:
  - `docs/chain-spec.md`
  - `docs/runbook.md`
  - `docs/overview.md`
  - `docs/architecture.md`
  - `docs/README.md`
  - `AGENTS.md`
- Validation:
  - `python3 -m py_compile chain_runtime.py` passed
  - JSON parse checks passed for new/updated chain files
  - chain schema validation passed (`validate_chain`)
  - runtime smoke submission accepted new flow (`vm-qemu-virtio` request `20260213-221137` progressed past `run_bootefi` to `elfloader_started` without unknown-step/validation errors)

## Execution Plan (Absolute Path Removal Phase)

### Plan-AP-1: Introduce code-root SSOT
- Add `get_code_root()` in `config.py`.
- Keep default behavior script-relative (`Path(__file__).resolve().parent`).

### Plan-AP-2: Remove hardcoded daemon command path
- Replace `autopilot_manager.py` default command literal with code-root derived path.

### Plan-AP-3: Make chain script invocations relocatable
- Expose runtime format tokens from chain runner context:
  - `code_root`
  - `chains_dir`
  - `profiles_dir`
- Update chain `analyze_logs` commands to use `{code_root}` instead of `/home/hlyytine/autopilot`.

### Plan-AP-4: Validate relocation behavior
- Static checks:
  - Python syntax compile for touched modules.
  - Search for `/home/hlyytine/autopilot` in executable chain files.
- Runtime sanity check:
  - Submit one `sel4test` run and confirm chain completes.

### Plan-AP-5: Update tracker and commit
- Record implementation results and finding status updates.
- Commit code + tracker updates in one changeset.

## Update Rules

- Add a new dated entry under **Progress Log** for every remediation step.
- When a finding is completed:
  - Mark finding status as `Resolved`.
  - Add commit hash(es) and changed file references.
  - Keep the original finding text for audit trail.
