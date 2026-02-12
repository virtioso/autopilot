# DRY and SSOT Tracking

**Created**: 2026-02-12  
**Scope**: `/home/hlyytine/autopilot`  
**Purpose**: Track DRY (Don't Repeat Yourself) and SSOT (Single Source of Truth) findings and remediation progress.

## Status

- Open findings: 5
- In progress: 0
- Resolved: 2

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
- **Status**: Open

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
- **Status**: Open

### F-005 (Medium): Docs claim fixed chain path while runtime resolves relative path
- **Summary**: Documentation presents fixed absolute chain resolution path; runtime uses script-relative chain directory.
- **Evidence**:
  - `docs/chain-spec.md:11`
  - `docs/chain-spec.md:80`
  - `orin_kernel_autopilot.py:20`
  - `orin_kernel_autopilot.py:22`
- **Risk**: SSOT drift between docs and implementation.
- **Proposed direction**: Update docs to describe script-relative resolution and examples separately.
- **Status**: Open

### F-006 (Low): Duplicate tty validation logic in MCP handlers
- **Summary**: `autopilot_start` and `autopilot_restart` each repeat tty argument validation logic.
- **Evidence**:
  - `sel4_mcp_server.py:915`
  - `sel4_mcp_server.py:954`
- **Risk**: Small maintenance overhead.
- **Proposed direction**: Helper function for shared validation/error response.
- **Status**: Open

### F-007 (Low): `sel4_client.py` duplicates queue path definitions
- **Summary**: Module-level legacy path constants duplicate path mapping available via `get_paths()`.
- **Evidence**:
  - `sel4_client.py:44`
  - `sel4_client.py:51`
  - `sel4_client.py:86` (already uses `get_paths()`)
- **Risk**: Dual path authority in one module.
- **Proposed direction**: Deprecate/remove legacy constants or confine to compatibility wrapper.
- **Status**: Open

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
