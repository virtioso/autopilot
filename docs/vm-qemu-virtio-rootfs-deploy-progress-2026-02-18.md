# VM QEMU Virtio Rootfs Deploy Progress (2026-02-18)

## Summary
This plan enforces a deterministic `vm-qemu-virtio` workflow where the latest Driver-VM rootfs is checksum-synced to Orin AGX stock Linux before EFI reboot, with fail-fast behavior on deploy issues.

## Fixed Decisions
- Canonical target storage path: `/var/lib/virtioso-vm-images`
- Migration mode: hard cutover to canonical path
- Deploy mode: checksum-gated upload (`sha256`)
- Failure policy: fail fast
- Artifact scope: Driver rootfs as SSOT (user-vm freshness inherited via driver rootfs build)

## Commit Discipline
- One functional step per commit.
- Immediately follow with a tracking update commit in this file.
- Commit message pattern:
  - `step(SN): <change>`
  - `track(SN): update progress and commit refs`

## Step Checklist
- [x] S0 Preflight checkpoint commits (dirty repo checkpoints + create this file)
- [ ] S1 Remove `initcall_debug` from VM1 bootargs in `projects/virtioso-camkes-vm/apps/Arm/vm_qemu_virtio/orinagx/devices.camkes`
- [ ] S2 Add Autopilot runtime step `upload_file` with checksum-gated logic
- [ ] S3 Integrate rootfs sync into `deploy_and_boot_test_efi.json` before EFI upload
- [ ] S4 Update init scripts default `ROOTFS_IMAGE` to canonical path under `/var/lib/virtioso-vm-images`
- [ ] S5 Run Autopilot preflight validation and record results
- [ ] S6 Run full clean build + test workflow and record evidence
- [ ] S7 Final acceptance closure

## Detailed Plan

### S0 Preflight checkpoints
- Commit current dirty state in:
  - `/home/hlyytine/autopilot`
  - `/home/hlyytine/tii-sel4/vm-images/virtioso-yocto-layers`
  - `/home/hlyytine/tii-sel4/projects/virtioso-camkes-vm` (if touched in this effort)
- Initialize progress tracking in this file.

### S1 First functional change: remove VM1 `initcall_debug`
- Edit:
  - `projects/virtioso-camkes-vm/apps/Arm/vm_qemu_virtio/orinagx/devices.camkes`
- Remove token:
  - `"initcall_debug "`
- Commit in `projects/virtioso-camkes-vm`.

### S2 Runtime support: generic file deploy with checksum gate
- Edit:
  - `/home/hlyytine/autopilot/chain_runtime.py`
  - `/home/hlyytine/autopilot/docs/chain-spec.md`
  - `/home/hlyytine/autopilot/docs/runbook.md`
- Add new step type: `upload_file`
- Required fields: `local_path`, `target_user`, `target_ip`, `target_path`
- Optional behavior:
  - `skip_if_same: sha256`
  - `method: scp` (default)
  - `atomic_replace: true` (default)
- Flow:
  1. Compute local sha256.
  2. Query remote sha256.
  3. Skip upload if hash matches.
  4. Otherwise upload to temp path, verify hash, atomic move to target.
  5. Error out on any mismatch/failure.

### S3 Chain integration: deploy rootfs before reboot
- Edit:
  - `/home/hlyytine/autopilot/chains/deploy_and_boot_test_efi.json`
- Insert before existing `upload_efi`:
  1. `ssh_cmd`: `mkdir -p /var/lib/virtioso-vm-images`
  2. `upload_file`:
     - `local_path`: `/home/hlyytine/tii-sel4/vm-images/build/tmp/deploy/images/vm-jetson-agx-orin/vm-image-driver-vm-jetson-agx-orin.rootfs.ext4`
     - `target_path`: `/var/lib/virtioso-vm-images/vm-image-driver-vm-jetson-agx-orin.rootfs.ext4`
     - `skip_if_same`: `sha256`
- Any deploy failure routes to `fail`.

### S4 Initramfs hard cutover to canonical path
- Edit:
  - `vm-images/virtioso-yocto-layers/meta-virtioso/recipes-core/bridge-initramfs-init/bridge-initramfs-init/init`
  - `vm-images/virtioso-yocto-layers/meta-virtioso/recipes-core/minimal-init/minimal-init/init`
- Change default:
  - `ROOTFS_IMAGE="var/lib/virtioso-vm-images/vm-image-driver-vm-jetson-agx-orin.rootfs.ext4"`

### S5 Validation
- Run:
  - `python3 /home/hlyytine/autopilot/scripts/preflight_autopilot.py`
- Record output and pass/fail in this file.

### S6 End-to-end verification
- Build sequence:
  1. `make mrproper`
  2. `make orinagx_defconfig`
  3. `make vm_qemu_virtio`
  4. `make linux-image` (ensure fresh Driver VM rootfs artifact)
- Test sequence:
  - restart autopilot (tmux + explicit UARTs)
  - submit `vm-qemu-virtio`
- Verify logs show:
  - rootfs sync step executed
  - no `/usr/bin/hyp-ftrace-ctl: No such file or directory`
  - init lookup path uses `/mnt/emmc/var/lib/virtioso-vm-images/...`

### S7 Closure
- Mark acceptance checklist complete.
- Final tracking commit.

## Commit Log
| Step | Repo | Commit | Status | Notes |
|---|---|---|---|---|
| S0 | autopilot | edc0b96 | completed | Preflight checkpoint |
| S0 | vm-images/virtioso-yocto-layers | a9e107e | completed | Preflight checkpoint |
| S0 | projects/virtioso-camkes-vm | 610b190 | completed | Preflight checkpoint |
| S1 | projects/virtioso-camkes-vm | _pending_ | not started | Remove initcall_debug |
| S2 | autopilot | _pending_ | not started | Add upload_file runtime + docs |
| S3 | autopilot | _pending_ | not started | Chain deploy integration |
| S4 | vm-images/virtioso-yocto-layers | _pending_ | not started | Initramfs path cutover |
| S5 | autopilot | _pending_ | not started | Validation evidence update |
| S6 | autopilot | _pending_ | not started | Test evidence update |
| S7 | autopilot | _pending_ | not started | Final closure |

## Acceptance Checklist
- [ ] `initcall_debug` removed from VM1 bootargs
- [ ] `upload_file` step implemented and documented
- [ ] `deploy_and_boot_test_efi` syncs driver rootfs before EFI upload
- [ ] initramfs default rootfs path uses `/var/lib/virtioso-vm-images`
- [ ] preflight validation passes
- [ ] end-to-end run confirms updated rootfs used by driver-vm
