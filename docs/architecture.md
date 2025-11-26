# Autopilot System Architecture

**Last Updated**: 2025-11-20

## System Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Host System (192.168.101.100)                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  Autopilot Service (orin_kernel_autopilot.py)                  │     │
│  │  - Watches ${WORKSPACE}/autopilot/requests/pending/     │     │
│  │  - Processes requests sequentially                             │     │
│  │  - Generates results in results/${TIMESTAMP}/                  │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                    │                                     │
│                                    │ controls                            │
│                                    ▼                                     │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  Board Control (BoardControl.py)                               │     │
│  │  - SSH to 192.168.101.110 ./boot.sh                            │     │
│  │  - Triggers board power/reset via USB relay                    │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                    │                                     │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  Boot Harness (BootHarness.py)                                 │     │
│  │  - UpdateBootHarness: Sends '0' to UART                        │     │
│  │  - PanicBootHarness: Sends '2' to UART                         │     │
│  │  - Monitors UART for panics/faults                             │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                    │                                     │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  UART Connections (USB Serial)                                 │     │
│  │  - /tmp/ttyACM0 → Target main console (ttyTCU0)                │     │
│  │  - /tmp/ttyACM1 → Target UARTI (EL2 hypervisor debug)          │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                    │                                     │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  Kernel Build Output                                           │     │
│  │  source/kernel/linux/arch/arm64/boot/Image                     │     │
│  │  source/kernel-devicetree/generic-dts/dtbs/*.dtb               │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                                                           │
└───────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ UART
                                    │ SSH
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│               Target Hardware (192.168.101.106)                         │
│                 NVIDIA Jetson AGX Orin (Tegra234)                       │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  UEFI Firmware                                                  │     │
│  │  - Boot menu with extlinux                                      │     │
│  │  - Listens for ESC and arrow keys on UART                      │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                    │                                     │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  extlinux.conf Boot Menu                                        │     │
│  │  - Option 0: update.target (5.15.148 kernel)                    │     │
│  │  - Option 1: rescue.target (unused)                             │     │
│  │  - Option 2: multi-user.target (6.17.0 test kernel)             │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                    │                                     │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  Update Service (systemd)                                       │     │
│  │  - update.target → update.service                               │     │
│  │  - Runs /root/bin/update.sh                                     │     │
│  │  - Downloads kernel from 192.168.101.100                        │     │
│  │  - Reboots                                                       │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                                                           │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  Test Kernel (6.17.0-tegra)                                     │     │
│  │  - pKVM enabled (kvm-arm.mode=protected)                        │     │
│  │  - EL2 hypervisor with SMMU support                             │     │
│  │  - May panic or generate SMMU faults                            │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                                                           │
└───────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ power/reset control
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│            Boot Control Server (192.168.101.110)                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  boot.sh Script                                                 │     │
│  │  - Controls USB relay board                                     │     │
│  │  - Triggers board reset                                          │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                                                           │
│  ┌────────────────────────────────────────────────────────────────┐     │
│  │  USB Relay Board                                                │     │
│  │  - Relay 1: Recovery mode (FC_REC) - unused                     │     │
│  │  - Relay 2: Reset (SYS_RST)                                     │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                                                           │
└───────────────────────────────────────────────────────────────────────────┘
```

## Component Details

### Host System Components

#### orin_kernel_autopilot.py

**Purpose**: Main orchestration daemon

**Location**: `${WORKSPACE}/autopilot/orin_kernel_autopilot.py`

**Key Responsibilities**:
- Monitor request queue (`requests/pending/`)
- Move requests through state directories (pending → processing → completed/failed)
- Orchestrate two-phase boot sequence
- Call log filtering and processing scripts
- Handle cleanup on shutdown (SIGINT/SIGTERM)

**State Machine**:
```python
while True:
    # 1. Find oldest pending request
    requests = sorted(PENDING_DIR.glob("*.request"))

    # 2. Move to processing (atomic rename)
    processing_file = PROCESSING_DIR / request_file.name
    request_file.rename(processing_file)

    # 3. Execute test sequence
    try:
        # Phase 1: Update boot
        seq = BootHarness.UpdateBootHarness(...)
        seq.run()

        # Phase 2: Test boot
        seq = BootHarness.PanicBootHarness(...)
        seq.run()

        # Process logs
        filter_panic_log()
        filter_hyp_log()
        extract_smmu_faults()
        disassemble_crash()

        # Success
        processing_file.rename(COMPLETED_DIR / request_file.name)
    except Exception:
        # Failure
        processing_file.rename(FAILED_DIR / request_file.name)
```

#### BootHarness.py

**Purpose**: Boot sequence control and console monitoring

**Location**: `${WORKSPACE}/autopilot/BootHarness.py`

**Classes**:

1. **BootHarness** (base class):
   - Opens serial connection to UART (ttyACM0)
   - Spawns thread to log hypervisor UART (ttyACM1)
   - Uses `pexpect.fdpexpect` for pattern matching
   - Navigates UEFI menus (ESC → Boot Manager → eMMC)
   - Sends boot option ('0' or '2') to extlinux menu

2. **UpdateBootHarness** (inherits BootHarness):
   - Sets `boot_option = '0'` (update mode)
   - Waits for "Rebooting system" message
   - Timeout: 180 seconds (allows time for kernel download)

3. **PanicBootHarness** (inherits BootHarness):
   - Sets `boot_option = '2'` (test mode)
   - Waits for one of:
     - "Kernel panic" → `fault_type = 'panic'`
     - "Unexpected global fault" → `fault_type = 'smmu_fault'`
     - Timeout (60s) → `fault_type = 'timeout'` (success!)
   - Collects extra output after detection (3 seconds)

**UART Pattern Matching**:
```python
idx = self.child.expect([
    r'Kernel panic',              # 0
    r'Unexpected global fault',   # 1
    r'callbacks suppressed',      # 2
    TIMEOUT,                      # 3
    EOF                           # 4
], timeout=5)

if idx == 0:
    self.fault_type = 'panic'
elif idx == 1 or idx == 2:
    smmu_fault_count += 1
    if smmu_fault_count >= 5:
        self.fault_type = 'smmu_fault'
```

#### BoardControl.py

**Purpose**: Hardware power control abstraction

**Location**: `${WORKSPACE}/autopilot/BoardControl.py`

**Classes**:

1. **BoardControlLocal**:
   - Uses `usbrelay_py` library
   - Controls USB relay board directly
   - Relay 1: Recovery mode (unused)
   - Relay 2: Reset signal

2. **BoardControlRemote** (currently used):
   - SSH to remote boot control server (192.168.101.110)
   - Executes `./boot.sh normal` or `./boot.sh recovery`
   - No direct hardware access

**Boot Sequence**:
```python
def boot(self, recovery):
    # Remote:
    mode = "normal" if not recovery else "recovery"
    subprocess.run(["ssh", "192.168.101.110", "./boot.sh", mode])

    # Local (if using USB relay):
    self.set_recovery(recovery)   # Assert recovery pin
    time.sleep(0.1)
    self.set_reset(True)          # Assert reset
    time.sleep(0.1)
    self.set_reset(False)         # Deassert reset → board powers on
    time.sleep(0.5)
    self.set_recovery(False)      # Deassert recovery pin
```

#### Log Processing Scripts

**Location**: `${WORKSPACE}/autopilot/`

1. **filter_nvhe_bug.py**:
   - Extracts kernel panic/oops messages
   - Output: `panic.log`

2. **filter_hyp_output.py**:
   - Filters hypervisor debug output (lines starting with `[hyp-`)
   - Output: `hyp.log`

3. **filter_smmu_faults.py**:
   - Analyzes SMMU global fault messages
   - Counts faults, identifies Stream IDs
   - Output: `smmu_faults.log`

4. **disasm_2nd_frame.py**:
   - Parses kernel panic backtrace
   - Disassembles function at second stack frame (caller)
   - Requires: kernel Image with symbols
   - Output: `disassembly.log`

---

### Target Hardware Components

#### UEFI Firmware

**Boot Flow**:
1. Power-on → UEFI firmware starts
2. Auto-boot timeout (default: continue to OS)
3. ESC pressed → Enter UEFI Setup
4. Menu navigation via arrow keys
5. Select Boot Manager → eMMC boot
6. Load `/boot/extlinux/extlinux.conf`

**Console**:
- Primary: ttyTCU0 (Tegra Combined UART) → UART console
- Hypervisor: UARTI → Raw UART, not muxed with kernel

#### extlinux.conf

**Location**: `/boot/extlinux/extlinux.conf` (on target eMMC)

**Menu Selection**:
- TIMEOUT 30 (3 seconds)
- DEFAULT primary (would boot first label if no input)
- User sends '0', '1', or '2' to select label

**Label 0 (original)**: Update Mode
```
LABEL original
      LINUX /boot/Image-5.15.148                        ← Stable kernel
      FDT /boot/tegra234-p3737-0000+p3701-0000-nv-5.15.148.dtb
      APPEND ... systemd.unit=update.target             ← Custom target
```

**Label 2 (linux617)**: Test Mode
```
LABEL linux617
      LINUX /boot/Image-6.17.0-tegra                    ← Test kernel
      FDT /boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb
      OVERLAYS /boot/tegra234-disable-display-overlay.dtbo
      APPEND ... systemd.unit=multi-user.target         ← Normal boot
             kvm-arm.mode=protected                     ← pKVM enabled
             kvm-arm.hyp_iommu_pages=20480              ← SMMU page pool
```

#### systemd Units

**update.target**: `/lib/systemd/system/update.target`
```ini
[Unit]
Description=Update Mode
Requires=sysinit.target update.service
After=sysinit.target update.service
AllowIsolate=yes
```

**update.service**: `/lib/systemd/system/update.service`
```ini
[Unit]
Description=Update from another host
DefaultDependencies=no         ← Minimal dependencies
After=sysinit.target

[Service]
ExecStart=/root/bin/update.sh
Type=idle                       ← Wait for sysinit to finish
StandardInput=tty-force         ← Attach to console
StandardOutput=inherit
StandardError=inherit
```

#### update.sh

**Location**: `/root/bin/update.sh` (on target)

**Source**: Template at `${WORKSPACE}/autopilot/bin/update.sh`

**Script**:
```bash
#!/bin/sh

export PATH=/bin:/sbin:/usr/bin:/usr/sbin

echo "Fetching IP address"
dhclient eno1   # Get DHCP IP (or use static)

echo "Copying kernel image"
ssh hlyytine@192.168.101.100 \
  'cat ${WORKSPACE}/Linux_for_Tegra/source/kernel/linux/arch/arm64/boot/Image' \
  > /boot/Image-6.17.0-tegra

echo "Rebooting"
reboot
```

**SSH Requirements**:
- SSH key authentication (no password)
- Host key already in `~/.ssh/known_hosts`
- User: hlyytine
- Host: 192.168.101.100

---

### Boot Control Server Components

#### boot.sh Script

**Location**: Remote server at 192.168.101.110: `~/boot.sh`

**Purpose**: Trigger board reset via USB relay

**Script** (example):
```bash
#!/bin/bash

MODE="$1"  # "normal" or "recovery"

if [ "$MODE" = "recovery" ]; then
    # Assert recovery pin (relay 1)
    # Not used by autopilot currently
    echo "Recovery mode not implemented"
    exit 1
else
    # Normal boot: just assert reset
    # Control relay 2 (SYS_RST)
    usbrelay BITFT_2=1   # Assert reset
    sleep 0.1
    usbrelay BITFT_2=0   # Deassert reset
fi
```

**USB Relay Library**:
- Uses `usbrelay` command-line tool
- Or Python library `usbrelay_py`
- Controls USB HID relay boards

---

## Request Queue System

### Directory Structure

```
${WORKSPACE}/autopilot/
├── requests/
│   ├── pending/          ← User creates .request files here
│   ├── processing/       ← Autopilot moves request here during test
│   ├── completed/        ← Moved here on success
│   └── failed/           ← Moved here on failure
└── results/
    └── ${TIMESTAMP}/     ← Test output directory
        ├── kernel.log
        ├── panic.log
        ├── hyp.log
        ├── uarti.log
        ├── smmu_faults.log (if SMMU faults detected)
        ├── disassembly.log (if kernel panic)
        └── kernel-update.log
```

### Request File Format

**Currently**: Empty file with timestamp as filename
```bash
touch requests/pending/20251120220000.request
```

**Timestamp Format**: `YYYYMMDDHHmmss` (year-month-day-hour-minute-second)

**Future**: Could contain JSON metadata
```json
{
  "kernel": "/path/to/Image",
  "dtb": "/path/to/dtb",
  "test_type": "panic_test"
}
```

### State Transitions

```
User creates request
      │
      ▼
┌─────────────┐
│  PENDING    │  ← Queue waiting for processing
└─────────────┘
      │ autopilot detects request
      │ atomic rename()
      ▼
┌─────────────┐
│ PROCESSING  │  ← Currently running test (max 1)
└─────────────┘
      │
      ├─ Success ───────────────┐
      │                          ▼
      │                    ┌──────────┐
      │                    │COMPLETED │
      │                    └──────────┘
      │
      └─ Failure ───────────────┐
                                ▼
                          ┌──────────┐
                          │ FAILED   │
                          └──────────┘
```

### Atomic Operations

**Why Atomic Rename?**
- Prevents race conditions (only one process can rename a file)
- Test either fully succeeds or fails (no partial state)
- Crash recovery: processing requests moved back to pending

**Cleanup on Shutdown**:
```python
def cleanup():
    """Move any processing requests back to pending on shutdown"""
    for request_file in PROCESSING_DIR.glob("*.request"):
        request_file.rename(PENDING_DIR / request_file.name)

signal.signal(signal.SIGINT, handle_signal)   # Ctrl+C
signal.signal(signal.SIGTERM, handle_signal)  # systemd stop
```

---

## Network Architecture

### IP Addresses

| System | IP Address | Role |
|--------|------------|------|
| Host | 192.168.101.100 | Build system, autopilot daemon, UART monitor |
| Target | 192.168.101.106 | Test platform (static or DHCP) |
| Boot Control | 192.168.101.110 | USB relay control server |

### Network Requirements

1. **Host → Target** (SSH):
   - Target must be able to SSH to host
   - Used by update.sh to download kernel
   - Requires: SSH key authentication, known_hosts

2. **Host → Boot Control** (SSH):
   - Host SSH to boot control server
   - Used by BoardControlRemote to trigger resets
   - Requires: SSH key authentication, known_hosts

3. **Host → Target** (UART):
   - USB serial adapters directly connected
   - No network required
   - Used for console I/O and boot menu navigation

---

## Timing Diagrams

### Full Test Sequence

```
Time    Host                          Target                        Boot Control
─────────────────────────────────────────────────────────────────────────────────
0s      Create .request file          (powered on, running)
        Autopilot detects request

5s      SSH boot control
        "ssh 192.168.101.110                                        Receive SSH
         ./boot.sh normal"                                          Assert reset
                                       (board resets)               Deassert reset

10s     Wait for UEFI prompt          UEFI starts
        "ESC to enter Setup"           Show boot menu
        Send ESC

20s     Navigate to Boot Manager
        Send arrow keys
        Send Enter

25s     Navigate to eMMC
        Send arrows + Enter

30s     Wait for extlinux menu        extlinux menu shown
        "Press any other key..."
        Send '0' (update mode)        Boot 5.15.148 kernel

40s                                   update.service starts
                                      /root/bin/update.sh runs
                                      dhclient eno1

50s                                   SSH to 192.168.101.100
                                      Download kernel Image

80s                                   Download complete
                                      Reboot

90s     Wait for UEFI prompt          UEFI starts (again)
        Send ESC

100s    Navigate menus again

110s    Send '2' (test mode)          Boot 6.17.0 kernel

120s    Monitor console for panic     Kernel boots

        [60 second timeout]           (may panic or complete boot)

180s    Detect timeout/panic/fault
        Collect remaining logs

185s    Process logs:
        - filter_nvhe_bug.py
        - filter_hyp_output.py
        - filter_smmu_faults.py
        - disasm_2nd_frame.py

190s    Move request to completed/

        Test complete!
        Results in results/$TIMESTAMP/
```

**Total Time**: ~5 minutes per test

---

## Error Recovery

### Boot Fallthrough to HTTP Boot

**Problem**: UEFI sometimes skips boot menu and falls through to network boot

**Detection**:
```python
idx = self.child.expect([
    r'ESC   to enter Setup.',    # 0: Normal (success)
    r'Start HTTP Boot',          # 1: Fallthrough (retry needed)
    TIMEOUT,
    EOF
], timeout=60)

if idx == 1:
    # Retry boot
    board.boot(False)
```

**Retry Logic**: Up to 3 attempts, usually succeeds on 2nd try

### Network Timeout During Update

**Problem**: Kernel download takes too long or network drops

**Detection**: UpdateBootHarness has 180-second timeout

**Recovery**: Test fails, request moved to `failed/`, user investigates network

### Kernel Panic During Update

**Problem**: Stable 5.15.148 kernel panics (very rare)

**Detection**: UpdateBootHarness sees "Kernel panic" instead of "Rebooting"

**Recovery**: Test fails, update.sh never completes, manual intervention needed

### UART Disconnect

**Problem**: USB serial adapter disconnects during test

**Detection**: `pexpect` EOF exception

**Recovery**: Test fails, request moved to `failed/`, partial logs saved

---

## Future Architecture Enhancements

### 1. Parallel Testing

**Current**: Sequential processing (one test at a time)

**Enhancement**: Support multiple target boards

**Changes**:
- Board pool management
- Request metadata includes board selection
- Parallel BootHarness instances
- Lock files per board

### 2. Result Database

**Current**: Filesystem-based results

**Enhancement**: SQLite database for results

**Schema**:
```sql
CREATE TABLE tests (
    timestamp TEXT PRIMARY KEY,
    fault_type TEXT,  -- 'panic', 'smmu_fault', 'timeout'
    kernel_hash TEXT,
    dtb_hash TEXT,
    duration_seconds INTEGER,
    status TEXT  -- 'completed', 'failed'
);
```

**Benefits**:
- Query test history
- Track regression trends
- Compare kernel versions

### 3. Web Dashboard

**Current**: Manual log inspection

**Enhancement**: Web UI for monitoring and results

**Features**:
- Live test status
- Historical results browser
- Panic rate graphs
- Log viewer with syntax highlighting

---

## See Also

- [Overview](overview.md) - Quick start and system overview
- [Boot Sequence](boot-sequence.md) - Detailed boot flow and UART interaction
- [Extending DTB Support](extending-dtb-support.md) - How to add device tree upload
