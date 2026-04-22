# Unify Remote Upload Verification with SHA-256 and Remove Invalid Legacy Code

## Summary

Speed up Autopilot deploys by replacing the current upload verification flow with a single generic SCP helper that:

1. Computes local SHA-256.
2. Probes the target file's SHA-256 if it already exists.
3. Skips upload immediately when hashes match.
4. Otherwise uploads the file.
5. Verifies SHA-256 on the target over SSH, without downloading the file back.
6. Runs `sync && sync` at the required points.
7. Fails on any checksum, SSH, SCP, move, or sync error.

This applies to both EFI deployment and Driver-VM rootfs deployment. The plan also removes invalid code artifacts from `<workspace>/autopilot` and deletes the legacy EFI harness from the Autopilot code repo.

## Key Changes

- In `chain_runtime.py`, replace duplicated SCP logic in `_step_upload()` and `_step_upload_file()` with one generic helper used by:
  - `upload_efi`
  - `upload_file`
  - optionally `upload_kernel` only if it can adopt the same behavior cleanly without widening scope
- Standardize on SHA-256 everywhere:
  - EFI upload adopts the same remote SHA-256 probe/verify model already conceptually used by `upload_file`
  - rootfs keeps SHA-256 policy, but loses the download-back verification
- Keep `upload_file` `atomic_replace=true` semantics:
  - probe final target for skip
  - upload to temp path
  - verify temp path SHA-256 remotely
  - `sync && sync`
  - `mv -f` into place
  - `sync && sync`
  - verify final target SHA-256 remotely
- For non-atomic SCP uploads such as EFI:
  - probe final target for skip
  - SCP directly to final path
  - `sync && sync`
  - verify final target SHA-256 remotely
- Narrow `upload_file.skip_if_same` policy to a single canonical checksum mode:
  - keep `sha256`
  - do not add MD5 support
- Delete legacy EFI upload code `seL4BootHarness.py`
- Remove invalid code artifacts from `<workspace>/autopilot` that violate the "runtime dir only" rule:
  - delete `chain_runtime.py`
  - delete stale `diagrams/`
  - leave runtime-owned queues/results/runtime state intact

## Public Interface / Behavior Changes

- `upload_file.skip_if_same` remains SHA-256-based and becomes the only supported checksum mode for this workflow.
- `upload_efi` gains the same fast-path behavior as `upload_file`:
  - skip transfer if target already matches
  - verify on target via SSH
  - no round-trip download
- The documented SCP contract becomes:
  - remote checksum probe before upload
  - remote checksum verification after upload
  - no verification download-back step

## Test Plan

- Unit/mock coverage for the generic SCP helper:
  - remote file exists and SHA-256 matches, so SCP is skipped
  - remote file missing, upload succeeds, sync succeeds, remote SHA-256 matches
  - remote file exists with different SHA-256, upload proceeds and succeeds
  - malformed remote SHA-256 output fails clearly
  - post-upload remote SHA-256 mismatch fails clearly
  - `sync && sync` failure fails clearly
  - atomic replace verifies temp path, moves, syncs, and verifies final path
- Step-level coverage:
  - `upload_efi` routes through the shared helper
  - `upload_file` routes through the shared helper
  - existing `local_copy` behavior remains unchanged unless explicitly refactored to share only checksum helpers
- Validation/docs checks:
  - `docs/chain-spec.md` matches runtime behavior
  - stale progress text that claims rootfs uses download-back or different checksum policy is updated where it is still presented as current behavior
  - any generated chain-diagram mirrors in `<workspace>` are regenerated only if the chain shape changes

## Assumptions

- `<code_root>` is the only authoritative code repo.
- `<workspace>/autopilot` is runtime state only; deleting code-like files there is correct and required.
- Deleting the legacy EFI harness is acceptable because it is non-authoritative and not part of the supported workflow.
- Scope does not include changing request/result history under `<workspace>/autopilot/requests` or `<workspace>/autopilot/results`.
