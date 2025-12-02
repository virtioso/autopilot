# Autopilot System Overview

**Last Updated**: 2025-11-20

## Purpose

The Autopilot system provides automated kernel testing for the NVIDIA Jetson AGX Orin (Tegra234) platform. It enables rapid development iteration by automatically uploading kernel images to hardware, booting them, and collecting comprehensive logs including panic traces, hypervisor output, and SMMU fault information.

## Key Features

- **Automated Kernel Upload**: Fetches latest kernel from build host via SSH
- **Remote Boot Control**: Controls board power/reset via relay or remote SSH
- **UART Monitoring**: Captures both kernel console (ttyACM0) and hypervisor debug output (ttyACM1/UARTI)
- **Crash Detection**: Detects kernel panics, SMMU faults, and timeout conditions
- **Log Processing**: Automatically filters and organizes logs for easy debugging
- **Request Queue**: Directory-based queue system for submitting multiple tests
- **Non-blocking**: Tests run asynchronously without manual intervention

## Prerequisites

### SSH Key Setup

The autopilot system requires passwordless SSH access to the Tegra board for kernel upload:

1. Ensure your SSH public key is in `/root/.ssh/authorized_keys` on the target
2. Target IP: `192.168.101.112`
3. Test with: `ssh root@192.168.101.112 hostname`

## Quick Start

### Submit a Test Request

After building a kernel:

```bash
# Submit test request (creates timestamped request file)
TIMESTAMP=$(date +%Y%m%d%H%M%S)
touch ${WORKSPACE}/autopilot/requests/pending/${TIMESTAMP}.request

# Wait for completion (~5 minutes)
# Results appear in ${WORKSPACE}/autopilot/results/${TIMESTAMP}/
```

### Check Test Status

```bash
# Check request queue status
ls -la ${WORKSPACE}/autopilot/requests/*/

# Monitor autopilot service logs
journalctl -u autopilot -f
```

### View Results

```bash
TIMESTAMP=20251120222200  # Your test timestamp

# Check what happened
cat ${WORKSPACE}/autopilot/results/${TIMESTAMP}/panic.log    # Kernel panic details
cat ${WORKSPACE}/autopilot/results/${TIMESTAMP}/hyp.log      # EL2 hypervisor logs
cat ${WORKSPACE}/autopilot/results/${TIMESTAMP}/kernel.log   # Full kernel boot log
cat ${WORKSPACE}/autopilot/results/${TIMESTAMP}/smmu_faults.log  # SMMU faults (if detected)
```

## System Components

### Host System (192.168.101.100)
- **Autopilot Service**: Python daemon monitoring request queue
- **Kernel Build**: Build system produces kernel at `source/kernel/linux/arch/arm64/boot/Image`
- **UART Access**: USB serial adapters connected to target board
  - `/tmp/ttyACM0` → Target's main console (ttyTCU0)
  - `/tmp/ttyACM1` → Target's UARTI (hypervisor debug output)

### Target Hardware (192.168.101.112)
- **Jetson AGX Orin**: Test platform running custom pKVM kernel
- **SSH Access**: Root SSH with public key authentication
- **Network**: Static IP 192.168.101.112
- **Boot Configuration**: UEFI with extlinux boot menu (3 options)

### Boot Control (192.168.101.110)
- **Remote Boot Server**: Controls board power via USB relay or GPIO
- **SSH Access**: Autopilot calls `ssh 192.168.101.110 ./boot.sh normal`

## Typical Workflow

1. **Developer builds kernel** on host (192.168.101.100)
2. **Submit test request**: Create `.request` file in pending directory
3. **Autopilot detects request**: Moves to processing directory
4. **Phase 1 - Upload**:
   - Boots board into vanilla Jetson Linux (extlinux option 1)
   - Waits for shell prompt (indicates SSH ready)
   - Uploads kernel via SCP to `/boot/Image-${KVER}`
   - Reboots target via SSH
5. **Phase 2 - Test**:
   - Boots board into test mode (extlinux option 2)
   - Monitors kernel console for panics/faults
   - Collects hypervisor debug output
6. **Log Processing**:
   - Filters panic messages
   - Extracts hypervisor logs
   - Disassembles crash site (if panic occurred)
   - Analyzes SMMU faults (if detected)
7. **Results Ready**: Request moved to completed directory

## Output Files

Each test produces several log files in `results/${TIMESTAMP}/`:

| File | Description | When Generated |
|------|-------------|----------------|
| `kernel.log` | Full kernel console output | Always |
| `panic.log` | Filtered kernel panic/oops details | Always (empty if no panic) |
| `hyp.log` | Filtered EL2 hypervisor debug output | Always |
| `uarti.log` | Raw UART from UARTI (hypervisor) | Always |
| `smmu_faults.log` | Detailed SMMU fault analysis | Only if SMMU faults detected |
| `disassembly.log` | Disassembly of crash function | Only if kernel panic |
| `kernel-update.log` | Log from kernel upload phase | Always |

## Request Queue States

Requests flow through directories representing their state:

```
requests/
├── pending/          # Submitted requests waiting to be processed
├── processing/       # Currently running test (max 1 at a time)
├── completed/        # Successfully completed tests
└── failed/           # Tests that encountered errors
```

## Error Handling

- **Boot Fallthrough**: Retries up to 3 times if board falls through to HTTP boot
- **Timeout Detection**: 60-second timeout for kernel panic/fault detection
- **Signal Handling**: SIGINT/SIGTERM moves processing requests back to pending
- **Partial Results**: Failed tests still produce whatever logs were collected

## Performance

- **Kernel Upload**: ~30 seconds (over 1 Gbps network)
- **Boot Time**: ~30-40 seconds from power-on to kernel start
- **Total Test Time**: ~5 minutes (including upload, 2 boots, log processing)
- **Queue Throughput**: 1 test every 5 minutes (sequential processing)

## Dependencies

### Host System
- Python 3 with libraries: `pexpect`, `pyserial`
- SSH access to boot control server (192.168.101.110)
- USB serial adapters for UART monitoring

### Target Hardware
- UEFI firmware with extlinux boot support
- SSH server running on boot with root access (public key auth)
- Network connectivity (static IP: 192.168.101.112)

### Boot Control Server
- USB relay board (for BoardControlLocal) OR
- Remote boot script accepting `normal` / `recovery` modes

## Limitations

- **Sequential Processing**: One test at a time (hardware limitation)
- **Network Required**: Target must have network access to fetch kernel
- **UART Required**: Relies on serial console for monitoring
- **No DTB Upload**: Currently only supports kernel upload (not device trees)

## See Also

- [Architecture Details](architecture.md) - Technical implementation details
- [Boot Sequence](boot-sequence.md) - How the boot menu and update cycle works
- [Request Queue System](request-queue.md) - Queue implementation and file states
- [Extending for DTB Support](extending-dtb-support.md) - Adding device tree upload capability
