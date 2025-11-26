# Boot Sequence and Menu System

**Last Updated**: 2025-11-20

## Overview

The autopilot system uses a two-phase boot sequence controlled by the extlinux boot menu. The UART console is used to select which boot option to use, enabling automated kernel upload followed by testing.

## Extlinux Boot Menu

The Jetson AGX Orin uses extlinux (syslinux) for boot menu selection. The configuration is stored on the device at `/boot/extlinux/extlinux.conf`:

```
TIMEOUT 30
DEFAULT primary

MENU TITLE L4T boot options

LABEL original                                    ← Option 0 (UPDATE MODE)
      MENU LABEL original 5.15.148
      LINUX /boot/Image-5.15.148
      INITRD /boot/initrd-5.15.148
      FDT /boot/tegra234-p3737-0000+p3701-0000-nv-5.15.148.dtb
      APPEND ... systemd.unit=update.target        ← Boots into update.target

LABEL rescue                                      ← Option 1 (RESCUE MODE)
      MENU LABEL rescue
      LINUX /boot/Image-5.15.148
      INITRD /boot/initrd-5.15.148
      FDT /boot/tegra234-p3737-0000+p3701-0000-nv-5.15.148.dtb
      APPEND ... systemd.unit=rescue.target

LABEL linux617                                    ← Option 2 (TEST MODE)
      MENU LABEL linux617
      LINUX /boot/Image-6.17.0-tegra               ← The kernel we're testing!
      INITRD /boot/initrd-6.17.0-tegra.img
      FDT /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb
      OVERLAYS /boot/tegra234-disable-display-overlay.dtbo
      APPEND ... systemd.unit=multi-user.target    ← Normal boot
```

## Menu Selection via UART

The autopilot sends a single character to the UART console during the boot menu timeout:

| Character | Menu Option | Purpose | systemd Target |
|-----------|-------------|---------|----------------|
| **'0'** | `LABEL original` | Update mode - download new kernel | `update.target` |
| **'1'** | `LABEL rescue` | Rescue mode (unused by autopilot) | `rescue.target` |
| **'2'** | `LABEL linux617` | Test mode - boot the new kernel | `multi-user.target` |

### Why Two Different Kernels?

- **Update Mode (option 0)**: Uses stable 5.15.148 kernel
  - Known-good kernel ensures update process works even if new kernel is broken
  - Boots quickly to download new kernel and reboot
  - Minimal risk of boot failure

- **Test Mode (option 2)**: Uses new 6.17.0 kernel being tested
  - This is the kernel we just built and want to test
  - May panic, hang, or have bugs - that's what we're testing!
  - Uses updated DTB and overlays

## Two-Phase Boot Process

### Phase 1: Update Boot (sends '0' to UART)

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Autopilot: Trigger board reboot                         │
│ 2. UEFI: Show boot menu prompt                             │
│ 3. Autopilot: Press ESC to enter UEFI menu                 │
│ 4. Autopilot: Navigate to Boot Manager → eMMC              │
│ 5. Extlinux: Show menu "Press any other key to boot..."    │
│ 6. Autopilot: Send '0' (select "original" label)           │ ← Update mode
│ 7. Kernel: Boot 5.15.148 with update.target                │
│ 8. Systemd: Start update.service                           │
│ 9. update.sh: Run DHCP client (dhclient eno1)              │
│ 10. update.sh: SSH to 192.168.101.100 to fetch new kernel  │
│ 11. update.sh: Copy to /boot/Image-6.17.0-tegra            │
│ 12. update.sh: Reboot                                       │
└─────────────────────────────────────────────────────────────┘
```

**Key Points**:
- Uses **stable kernel** (5.15.148) to ensure update succeeds
- `systemd.unit=update.target` boots directly to update service
- No other services start (fast boot)
- Automatically reboots after update completes

### Phase 2: Test Boot (sends '2' to UART)

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Board reboots after update                              │
│ 2. UEFI: Show boot menu prompt (again)                     │
│ 3. Autopilot: Press ESC, navigate to Boot Manager → eMMC   │
│ 4. Extlinux: Show menu "Press any other key to boot..."    │
│ 5. Autopilot: Send '2' (select "linux617" label)           │ ← Test mode
│ 6. Kernel: Boot 6.17.0 (the NEW kernel!)                   │
│ 7. Kernel: May panic, trigger SMMU faults, or boot OK      │
│ 8. Autopilot: Monitor console for 60 seconds               │
│    - Detect: "Kernel panic"                                │
│    - Detect: "Unexpected global fault" (SMMU)              │
│    - Detect: Timeout (no panic = success)                  │
│ 9. Autopilot: Collect all logs                             │
│ 10. Autopilot: Process and save results                    │
└─────────────────────────────────────────────────────────────┘
```

**Key Points**:
- Uses **new kernel** (6.17.0-tegra) just downloaded
- `systemd.unit=multi-user.target` boots normally
- All services start (realistic test environment)
- pKVM parameters: `kvm-arm.mode=protected kvm-arm.hyp_iommu_pages=20480`

## Update Service Implementation

### systemd Unit: `/lib/systemd/system/update.target`

```ini
[Unit]
Description=Update Mode
Requires=sysinit.target update.service
After=sysinit.target update.service
AllowIsolate=yes
```

**Purpose**: Custom systemd target that only runs update.service

### systemd Service: `/lib/systemd/system/update.service`

```ini
[Unit]
Description=Update from another host
DefaultDependencies=no
Conflicts=shutdown.target
After=sysinit.target
Before=shutdown.target

[Service]
Environment=HOME=/root
WorkingDirectory=-/root
ExecStart=/root/bin/update.sh
Type=idle
StandardInput=tty-force
StandardOutput=inherit
StandardError=inherit
KillMode=process
IgnoreSIGPIPE=no
SendSIGHUP=yes
```

**Key Settings**:
- `DefaultDependencies=no` - Minimal dependencies for fast boot
- `Type=idle` - Wait for other sysinit jobs to complete
- `StandardInput=tty-force` - Attach to console for debugging

### Update Script: `/root/bin/update.sh`

```bash
#!/bin/sh

export PATH=/bin:/sbin:/usr/bin:/usr/sbin

echo "Fetching IP address"
dhclient eno1  # Get IP via DHCP

echo "Copying kernel image"
ssh hlyytine@192.168.101.100 \
  'cat ${WORKSPACE}/Linux_for_Tegra/source/kernel/linux/arch/arm64/boot/Image' \
  > /boot/Image-6.17.0-tegra

echo "Rebooting"
reboot
```

**How It Works**:
1. Acquire IP address via DHCP on eno1 interface
2. SSH to build host (192.168.101.100)
3. Stream kernel Image directly to `/boot/Image-6.17.0-tegra`
4. Trigger reboot (which starts Phase 2)

**Network Requirements**:
- Target must be able to reach 192.168.101.100 (build host)
- SSH key authentication (no password prompt)
- Sufficient bandwidth (~50 MB kernel in ~30 seconds)

## UART Console Flow

### UpdateBootHarness (Phase 1)

```python
class UpdateBootHarness(BootHarness):
    def __init__(self, board, tty, filename, hyp_tty, hyp_filename):
        super().__init__(board, tty, filename, hyp_tty, hyp_filename)
        # Inherits boot_option = '0'  ← Sends '0' to select update mode

    def run(self):
        super().run()  # Navigate UEFI → Boot Manager → eMMC → send '0'

        # Wait for update.sh to complete
        self.child.expect(r'Rebooting system', timeout=180)
```

**Expects**:
- "Fetching IP address" from update.sh
- "Copying kernel image" from update.sh
- "Rebooting" from update.sh
- systemd "Rebooting system" message

### PanicBootHarness (Phase 2)

```python
class PanicBootHarness(BootHarness):
    def __init__(self, board, tty, filename, hyp_tty, hyp_filename):
        super().__init__(board, tty, filename, hyp_tty, hyp_filename)
        self.boot_option = '2'  # ← Sends '2' to select test mode
        self.fault_type = None

    def run(self):
        super().run()  # Navigate UEFI → Boot Manager → eMMC → send '2'

        # Monitor for crashes
        while timeout_total > 0:
            idx = self.child.expect([
                r'Kernel panic',              # Panic detected
                r'Unexpected global fault',   # SMMU fault
                r'callbacks suppressed',      # SMMU callback suppression
                TIMEOUT,
                EOF
            ], timeout=5)

            if idx == 0:
                self.fault_type = 'panic'
                break
            elif idx == 1 or idx == 2:
                smmu_fault_count += 1
                if smmu_fault_count >= 5:
                    self.fault_type = 'smmu_fault'
                    break
```

**Fault Detection**:
- Stops on first "Kernel panic" message
- Accumulates SMMU faults, stops after 5 instances
- Times out after 60 seconds if no faults (success!)

## UEFI Navigation Sequence

The autopilot navigates UEFI menus using escape sequences:

```python
# 1. Press ESC to enter UEFI menu
self.child.send('\x1b')

# 2. Navigate to Boot Manager (2 down arrows + Enter)
self.child.send('\x1b[B')  # Down arrow
time.sleep(0.3)
self.child.send('\x1b[B')  # Down arrow
time.sleep(0.3)
self.child.send('\r')      # Enter

# 3. Select eMMC boot (3 down arrows + Enter)
self.child.send('\x1b[B')  # Down arrow
time.sleep(0.3)
self.child.send('\x1b[B')  # Down arrow
time.sleep(0.3)
self.child.send('\x1b[B')  # Down arrow
time.sleep(0.3)
self.child.send('\r')      # Enter

# 4. Select boot option ('0' or '2')
self.child.send(self.boot_option)  # '0' or '2'
```

**UEFI Menu Structure**:
```
Main Menu:
  ├─ Continue                    (default)
  ├─ Boot Manager               ← We select this
  └─ Device Manager

Boot Manager:
  ├─ UEFI Shell
  ├─ HTTP Boot
  ├─ eMMC Boot                  ← We select this
  └─ USB Boot

Extlinux Menu:
  ├─ original (0)               ← Update mode
  ├─ rescue (1)                 ← Unused
  └─ linux617 (2)               ← Test mode
```

## Timing and Timeouts

| Phase | Operation | Timeout | Reason |
|-------|-----------|---------|--------|
| Boot | UEFI prompt detection | 60s | May fall through to HTTP boot |
| Boot | UEFI menu navigation | 60s | Allow time for menu rendering |
| Boot | Extlinux menu | 60s | Standard extlinux timeout |
| Update | Kernel download | 180s | Large file over network |
| Test | Panic detection | 60s | Most panics happen in first minute |
| Test | SMMU fault collection | 60s | Wait for fault storm to settle |

## HTTP Boot Fallthrough Handling

Sometimes UEFI falls through to HTTP boot instead of showing the menu. The autopilot handles this:

```python
max_boot_retries = 3
for boot_attempt in range(max_boot_retries):
    boot()

    idx = self.child.expect([
        r'ESC   to enter Setup.',    # Normal UEFI prompt (success)
        r'Start HTTP Boot',          # Fallthrough (retry needed)
        TIMEOUT,
        EOF
    ], timeout=60)

    if idx == 0:
        # Success! Send ESC and continue
        self.child.send('\x1b')
        break
    elif idx == 1:
        # Retry boot
        continue
```

**Why This Happens**:
- Race condition in UEFI firmware
- Timing-dependent on board initialization
- Retry almost always succeeds on second attempt

## See Also

- [Overview](overview.md) - System overview and quick start
- [Architecture](architecture.md) - Technical implementation details
- [Extending DTB Support](extending-dtb-support.md) - Adding device tree upload
