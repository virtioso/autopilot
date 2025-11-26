# Extending Autopilot for Device Tree Blob (DTB) Upload

**Last Updated**: 2025-11-20

## Current Limitation

The autopilot system currently only supports uploading kernel images (`Image` files). Device tree blobs (`.dtb` files) must be manually copied to the target hardware, which slows down development iteration when testing device tree changes.

## Goal

Enable automatic DTB upload alongside kernel upload, allowing device tree changes to be tested as easily as kernel changes.

## Building Device Tree Blobs (DTBs)

**⚠️ IMPORTANT: Use ONLY This Method**

To build device tree blobs, you MUST use the following commands:

```bash
cd ${WORKSPACE}/Linux_for_Tegra/source

# Set kernel headers path (REQUIRED!)
export KERNEL_HEADERS=${WORKSPACE}/Linux_for_Tegra/source/kernel/linux

# Build DTBs (alias for 'make nvidia-dtbs')
make dtbs
```

**Output Location**:
```
${WORKSPACE}/Linux_for_Tegra/source/kernel-devicetree/generic-dts/dtbs/tegra234-p3737-0000+p3701-0000-nv.dtb
```

**DO NOT use**:
- ❌ `nvbuild.sh` for DTBs (outputs to old `kernel_out/` directory)
- ❌ Building from kernel source directory directly
- ❌ Any path other than `source/kernel-devicetree/generic-dts/dtbs/`

**Why This Matters**:
- The Makefile target `nvidia-dtbs` uses `KERNEL_HEADERS` to find kernel sources
- Output goes to `source/kernel-devicetree/generic-dts/dtbs/` (NOT `kernel_out/`)
- This is the ONLY location autopilot should reference

**Verification**:
```bash
# After building, check timestamp
ls -lh ${WORKSPACE}/Linux_for_Tegra/source/kernel-devicetree/generic-dts/dtbs/tegra234-p3737-0000+p3701-0000-nv.dtb

# Should show recent timestamp (just built)
```

## Design Options

### Option A: Extend update.sh (Recommended)

**Approach**: Modify the target's `/root/bin/update.sh` script to download both kernel and DTB.

**Pros**:
- Minimal changes to existing system
- Reuses existing SSH infrastructure
- Target-side control (can validate DTB before deploying)
- Easy to test incrementally

**Cons**:
- Requires update to `/root/bin/update.sh` on target (one-time)
- Fixed DTB filename (must know which DTB to fetch)

**Implementation**:

#### Modified `/root/bin/update.sh` on Target:

```bash
#!/bin/sh

export PATH=/bin:/sbin:/usr/bin:/usr/sbin

echo "Fetching IP address"
dhclient eno1

echo "Copying kernel image"
ssh hlyytine@192.168.101.100 \
  'cat ${WORKSPACE}/Linux_for_Tegra/source/kernel/linux/arch/arm64/boot/Image' \
  > /boot/Image-6.17.0-tegra

echo "Copying device tree blob"
ssh hlyytine@192.168.101.100 \
  'cat ${WORKSPACE}/Linux_for_Tegra/source/kernel-devicetree/generic-dts/dtbs/tegra234-p3737-0000+p3701-0000-nv.dtb' \
  > /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb

echo "Rebooting"
reboot
```

**Changes**:
- Added second `ssh` command to fetch DTB
- DTB saved to `/boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb`
- Same path referenced in extlinux.conf `LABEL linux617` → `FDT` line

**Deployment**:
1. Update `/root/bin/update.sh` on target (one-time manual update)
2. No autopilot changes required
3. Works immediately for all future tests

---

### Option B: Request Metadata with DTB Path

**Approach**: Include DTB path in request file, pass to target via environment variable.

**Pros**:
- Flexible - can test different DTBs per request
- No hardcoded paths
- Target can validate DTB exists before deploying

**Cons**:
- More complex implementation
- Requires changes to autopilot, update.service, and update.sh
- Need to pass metadata from host to target

**Implementation**:

#### Request File Format (host):

Instead of empty `.request` files, use JSON:

```bash
cat > ${WORKSPACE}/autopilot/requests/pending/20251120220000.request << 'EOF'
{
  "kernel": "${WORKSPACE}/Linux_for_Tegra/source/kernel/linux/arch/arm64/boot/Image",
  "dtb": "${WORKSPACE}/Linux_for_Tegra/source/kernel-devicetree/generic-dts/dtbs/tegra234-p3737-0000+p3701-0000-nv.dtb"
}
EOF
```

#### Modified `orin_kernel_autopilot.py` (host):

```python
# Parse request file
request_file = requests[0]
timestamp = request_file.stem

# Read metadata
metadata = {}
if request_file.stat().st_size > 0:
    with open(request_file, 'r') as f:
        metadata = json.load(f)

# Create environment for update harness
update_env = {
    'KERNEL_PATH': metadata.get('kernel', '${WORKSPACE}/Linux_for_Tegra/source/kernel/linux/arch/arm64/boot/Image'),
    'DTB_PATH': metadata.get('dtb', '')
}

# Pass environment to UpdateBootHarness
seq = BootHarness.UpdateBootHarness(
    board,
    '/tmp/ttyACM0',
    str(result_dir / 'kernel-update.log'),
    '/tmp/ttyACM1',
    str(result_dir / 'uarti-dummy.log'),
    env=update_env  # New parameter
)
```

#### Modified `update.service` (target):

Add environment passing:

```ini
[Service]
Environment=HOME=/root
Environment=DTB_PATH=""         # Will be overridden if provided
WorkingDirectory=-/root
ExecStart=/root/bin/update.sh
```

**Problem**: Can't dynamically set environment from SSH! Need different approach...

#### Alternative: Use SSH to pass paths directly

Modified `update.sh` (target):

```bash
#!/bin/sh

export PATH=/bin:/sbin:/usr/bin:/usr/sbin

echo "Fetching IP address"
dhclient eno1

echo "Copying kernel image"
# Kernel path passed as first argument (defaults to standard location)
KERNEL_PATH="${1:-${WORKSPACE}/Linux_for_Tegra/source/kernel/linux/arch/arm64/boot/Image}"
ssh hlyytine@192.168.101.100 "cat ${KERNEL_PATH}" > /boot/Image-6.17.0-tegra

# DTB path passed as second argument (skip if empty)
DTB_PATH="${2:-}"
if [ -n "${DTB_PATH}" ]; then
    echo "Copying device tree blob"
    ssh hlyytine@192.168.101.100 "cat ${DTB_PATH}" > /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb
fi

echo "Rebooting"
reboot
```

Modified `UpdateBootHarness` to inject commands via console:

```python
def run(self):
    super().run()  # Boot to update mode

    # Wait for shell prompt (update.sh doesn't auto-run anymore)
    self.child.expect(r'root@.*#', timeout=60)

    # Execute update.sh with custom paths
    kernel_path = self.env.get('KERNEL_PATH', '${WORKSPACE}/Linux_for_Tegra/source/kernel/linux/arch/arm64/boot/Image')
    dtb_path = self.env.get('DTB_PATH', '')

    cmd = f'/root/bin/update.sh "{kernel_path}" "{dtb_path}"\n'
    self.child.send(cmd)

    # Wait for reboot
    self.child.expect(r'Rebooting system', timeout=180)
```

**Complexity**: High - requires changing update.service to drop to shell instead of auto-running update.sh.

---

### Option C: Autopilot-Controlled Upload (Advanced)

**Approach**: Autopilot uploads files directly via SCP instead of having target pull them.

**Pros**:
- Complete control from host side
- Can upload arbitrary files
- Target doesn't need SSH access to host
- Can verify checksums

**Cons**:
- Requires SSH access FROM host TO target
- Need to know target's IP address
- More complex error handling
- Changes boot timing (upload before boot vs during boot)

**Implementation**:

#### New Upload Phase in `orin_kernel_autopilot.py`:

```python
def upload_files_to_target(target_ip, files):
    """Upload files to target via SCP before booting"""
    for local_path, remote_path in files.items():
        print(f"[AUTOPILOT] Uploading {local_path} to {target_ip}:{remote_path}")
        subprocess.run([
            'scp',
            local_path,
            f'root@{target_ip}:{remote_path}'
        ], check=True)

# In main loop:
try:
    # Upload files BEFORE update boot
    files_to_upload = {
        '${WORKSPACE}/Linux_for_Tegra/source/kernel/linux/arch/arm64/boot/Image':
            '/boot/Image-6.17.0-tegra',
        '${WORKSPACE}/Linux_for_Tegra/source/kernel-devicetree/generic-dts/dtbs/tegra234-p3737-0000+p3701-0000-nv.dtb':
            '/boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb'
    }

    # Wait for target to boot to rescue mode first (for upload)
    board.boot(False)
    time.sleep(60)  # Wait for target to boot
    upload_files_to_target('192.168.101.106', files_to_upload)

    # Then trigger normal test boot
    seq = BootHarness.PanicBootHarness(...)
    seq.run()
```

**Issues**:
- Need target to be running before upload (adds boot phase)
- Target IP must be known and reachable
- Timing is tricky (how long to wait for target boot?)

---

## Recommended Implementation: Option A (Enhanced)

**Rationale**:
1. **Simplest**: One-line change to target's update.sh
2. **Reliable**: Reuses existing SSH infrastructure
3. **Fast**: No additional boot phases
4. **Maintainable**: All logic in one script

### Enhanced Option A: Conditional DTB Upload

To make it even more flexible, check if DTB exists before downloading:

#### Enhanced `/root/bin/update.sh`:

```bash
#!/bin/sh

export PATH=/bin:/sbin:/usr/bin:/usr/sbin

HOST="hlyytine@192.168.101.100"
KERNEL_SRC="${WORKSPACE}/Linux_for_Tegra/source/kernel/linux/arch/arm64/boot/Image"
DTB_SRC="${WORKSPACE}/Linux_for_Tegra/source/kernel-devicetree/generic-dts/dtbs/tegra234-p3737-0000+p3701-0000-nv.dtb"

echo "Fetching IP address"
dhclient eno1

echo "Copying kernel image"
ssh ${HOST} "cat ${KERNEL_SRC}" > /boot/Image-6.17.0-tegra

echo "Checking for device tree blob"
if ssh ${HOST} "test -f ${DTB_SRC}"; then
    echo "Copying device tree blob"
    ssh ${HOST} "cat ${DTB_SRC}" > /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb
else
    echo "No DTB found, skipping (using existing DTB on target)"
fi

echo "Rebooting"
reboot
```

**Benefits**:
- Backwards compatible (works if DTB not built)
- Automatic (downloads DTB whenever it exists)
- Fallback (uses existing DTB if new one not available)
- Zero autopilot changes required

---

## Implementation Plan

### Phase 1: Basic DTB Upload ✅ **RECOMMENDED**

1. **Update `/root/bin/update.sh` on target** (one-time):
   ```bash
   # Add DTB download lines after kernel download
   echo "Copying device tree blob"
   ssh hlyytine@192.168.101.100 \
     'cat ${WORKSPACE}/Linux_for_Tegra/source/kernel-devicetree/generic-dts/dtbs/tegra234-p3737-0000+p3701-0000-nv.dtb' \
     > /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb
   ```

2. **Test manually**:
   ```bash
   # On target, run:
   /root/bin/update.sh

   # Verify DTB was updated:
   ls -lh /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb
   ```

3. **Test via autopilot**:
   ```bash
   # Build kernel with DTB
   cd ${WORKSPACE}/Linux_for_Tegra/source
   export KERNEL_HEADERS=${WORKSPACE}/Linux_for_Tegra/source/kernel/linux
   make dtbs

   # Submit test request
   TIMESTAMP=$(date +%Y%m%d%H%M%S)
   touch ${WORKSPACE}/autopilot/requests/pending/${TIMESTAMP}.request

   # Check results - DTB should be updated
   ```

**Timeline**: 15 minutes

### Phase 2: Enhanced DTB Upload (Optional)

1. **Make DTB upload conditional** (handles missing DTB gracefully)
2. **Add checksums** (verify successful upload)
3. **Add DTB version logging** (track which DTB was tested)

**Timeline**: 1 hour

### Phase 3: Multiple DTB Support (Future)

1. **Support multiple DTBs** (main + overlays)
2. **Request metadata** (specify which DTB per test)
3. **Parallel upload** (kernel + DTB + overlays simultaneously)

**Timeline**: 4-6 hours

---

## Testing Procedure

### Test 1: Verify DTB Upload Works

```bash
# 1. Build DTB with a marker change
cd ${WORKSPACE}/Linux_for_Tegra/source/hardware/nvidia/t23x/nv-public
# Add a comment to tegra234.dtsi
echo "/* DTB upload test $(date) */" >> tegra234.dtsi

# 2. Rebuild DTB
cd ${WORKSPACE}/Linux_for_Tegra/source
export KERNEL_HEADERS=${WORKSPACE}/Linux_for_Tegra/source/kernel/linux
make dtbs

# 3. Verify DTB timestamp
ls -lh kernel-devicetree/generic-dts/dtbs/tegra234-p3737-0000+p3701-0000-nv.dtb

# 4. Submit autopilot test
TIMESTAMP=$(date +%Y%m%d%H%M%S)
touch ${WORKSPACE}/autopilot/requests/pending/${TIMESTAMP}.request

# 5. After test completes, verify DTB on target
ssh root@192.168.101.106 "ls -lh /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb"

# 6. Compare checksums
md5sum kernel-devicetree/generic-dts/dtbs/tegra234-p3737-0000+p3701-0000-nv.dtb
ssh root@192.168.101.106 "md5sum /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb"
# Checksums should match!
```

### Test 2: Verify DTB Changes Take Effect

```bash
# 1. Modify device tree (e.g., disable a device)
cd ${WORKSPACE}/Linux_for_Tegra/source/hardware/nvidia/t23x/nv-public
# Example: Comment out supports-cqe property
sed -i 's/supports-cqe;/\/\* supports-cqe; \*\//' tegra234.dtsi

# 2. Rebuild DTB
cd ${WORKSPACE}/Linux_for_Tegra/source
export KERNEL_HEADERS=${WORKSPACE}/Linux_for_Tegra/source/kernel/linux
make dtbs

# 3. Submit test
TIMESTAMP=$(date +%Y%m%d%H%M%S)
touch ${WORKSPACE}/autopilot/requests/pending/${TIMESTAMP}.request

# 4. Check results - verify device behavior changed
grep -i "cqhci" ${WORKSPACE}/autopilot/results/${TIMESTAMP}/kernel.log
# Should NOT see CQHCI initialization if disabled correctly
```

---

## Error Handling

### DTB Download Failure

**Scenario**: DTB doesn't exist on host (not yet built)

**Current Behavior**: SSH would succeed but cat empty file → 0-byte DTB → boot failure

**Solution**: Enhanced update.sh with existence check:
```bash
if ssh ${HOST} "test -f ${DTB_SRC}"; then
    echo "Copying device tree blob"
    ssh ${HOST} "cat ${DTB_SRC}" > /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb
else
    echo "DTB not found on host, keeping existing DTB"
fi
```

### Network Failure During DTB Download

**Scenario**: Network drops between kernel upload and DTB upload

**Current Behavior**: update.sh exits with error, system hangs

**Solution**: Add retry logic:
```bash
MAX_RETRIES=3
for attempt in $(seq 1 $MAX_RETRIES); do
    echo "Copying device tree blob (attempt $attempt/$MAX_RETRIES)"
    if ssh ${HOST} "cat ${DTB_SRC}" > /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb; then
        echo "DTB upload successful"
        break
    else
        echo "DTB upload failed, retrying..."
        sleep 2
    fi
done
```

### Corrupted DTB

**Scenario**: DTB download corrupted due to network error

**Current Behavior**: Boot hangs or kernel panic with cryptic error

**Solution**: Add checksum verification:
```bash
# Get checksum from host
EXPECTED_MD5=$(ssh ${HOST} "md5sum ${DTB_SRC} | cut -d' ' -f1")

# Download DTB
ssh ${HOST} "cat ${DTB_SRC}" > /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb

# Verify checksum
ACTUAL_MD5=$(md5sum /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb | cut -d' ' -f1)

if [ "${EXPECTED_MD5}" != "${ACTUAL_MD5}" ]; then
    echo "ERROR: DTB checksum mismatch!"
    echo "Expected: ${EXPECTED_MD5}"
    echo "Got:      ${ACTUAL_MD5}"
    echo "Keeping old DTB"
    # Don't reboot - manual intervention needed
    exit 1
fi
```

---

## Future Enhancements

### 1. DTB Overlays Support

Support for device tree overlays (`.dtbo` files):

```bash
# Download base DTB
ssh ${HOST} "cat ${DTB_SRC}" > /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb

# Download overlay (if exists)
OVERLAY_SRC="${WORKSPACE}/Linux_for_Tegra/source/kernel-devicetree/generic-dts/dtbs/tegra234-disable-display-overlay.dtbo"
if ssh ${HOST} "test -f ${OVERLAY_SRC}"; then
    ssh ${HOST} "cat ${OVERLAY_SRC}" > /boot/tegra234-disable-display-overlay.dtbo
fi
```

### 2. Initramfs Upload

Support for custom initramfs:

```bash
INITRAMFS_SRC="${WORKSPACE}/Linux_for_Tegra/rootfs/boot/initrd-6.17.0-tegra.img"
if ssh ${HOST} "test -f ${INITRAMFS_SRC}"; then
    echo "Copying initramfs"
    ssh ${HOST} "cat ${INITRAMFS_SRC}" > /boot/initrd-6.17.0-tegra.img
fi
```

### 3. Module Upload

Support for out-of-tree kernel modules:

```bash
# Upload modules tarball
MODULES_SRC="${WORKSPACE}/Linux_for_Tegra/source/modules-6.17.0.tar.gz"
if ssh ${HOST} "test -f ${MODULES_SRC}"; then
    echo "Copying kernel modules"
    ssh ${HOST} "cat ${MODULES_SRC}" > /tmp/modules.tar.gz
    tar -xzf /tmp/modules.tar.gz -C /lib/modules/
    rm /tmp/modules.tar.gz
    depmod -a
fi
```

---

## Performance Considerations

### Upload Times

Current kernel upload: ~30 seconds (50 MB over 1 Gbps)

Estimated DTB upload times:
- DTB (250 KB): ~0.5 seconds (negligible)
- DTB + Overlay (300 KB): ~0.6 seconds
- DTB + Initramfs (50 MB): ~30 seconds (doubles total upload time)
- DTB + Modules (100 MB): ~60 seconds (triples total upload time)

**Optimization**: Parallel uploads using background jobs:

```bash
# Upload kernel and DTB in parallel
(ssh ${HOST} "cat ${KERNEL_SRC}" > /boot/Image-6.17.0-tegra) &
(ssh ${HOST} "cat ${DTB_SRC}" > /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb) &
wait  # Wait for both to complete
```

**Savings**: ~5-10% reduction in total upload time (not significant for single file)

---

## Summary

**Recommended Approach**: Option A (Enhanced)
- Update `/root/bin/update.sh` to download DTB after kernel
- Add conditional check to handle missing DTB gracefully
- No autopilot changes required
- Works immediately for all future tests

**Implementation Time**: 15 minutes

**Benefits**:
- ✅ Automatic DTB upload with every kernel test
- ✅ Backwards compatible (works without DTB)
- ✅ Simple and maintainable
- ✅ No autopilot code changes
- ✅ Reuses existing SSH infrastructure

**Next Steps**:
1. Update `/root/bin/update.sh` on target (one-time)
2. Test with manual run of update.sh
3. Test via autopilot
4. Document DTB build process in workflow docs
