# Autopilot System Documentation

**Last Updated**: 2025-11-20

This directory contains comprehensive documentation for the Autopilot automated kernel testing system for the NVIDIA Jetson AGX Orin (Tegra234) platform.

## Documentation Index

### 1. [Overview](overview.md)
**Start here!** System overview, quick start guide, and feature summary.

**Topics**:
- What is autopilot?
- Quick start (submit a test)
- System components
- Typical workflow
- Output files
- Performance and limitations

**Best for**: New users, getting started, understanding what autopilot does

---

### 2. [Boot Sequence](boot-sequence.md)
Detailed explanation of the two-phase boot process and how the boot menu system works.

**Topics**:
- Extlinux boot menu configuration
- Why send '0' or '2' to UART
- Update mode vs Test mode
- systemd unit files (update.target, update.service)
- update.sh script explained
- UEFI navigation sequence
- Timing and timeouts
- HTTP boot fallthrough handling

**Best for**: Understanding how boots are triggered, debugging boot issues, modifying boot behavior

---

### 3. [Architecture](architecture.md)
Technical implementation details and system architecture.

**Topics**:
- System diagram (host, target, boot control)
- Component details (Python modules, scripts)
- Request queue system
- Network architecture
- Timing diagrams
- Error recovery
- Future enhancements

**Best for**: Developers, debugging issues, understanding internal flow, extending the system

---

### 4. [Extending DTB Support](extending-dtb-support.md)
How to add device tree blob (DTB) upload capability to autopilot.

**Topics**:
- Current limitation (kernel only)
- Design options (3 approaches compared)
- Recommended implementation (Option A)
- Enhanced update.sh script
- Testing procedure
- Error handling
- Future enhancements (overlays, initramfs, modules)

**Best for**: Adding DTB upload support, understanding design tradeoffs, modifying update.sh

---

## Quick Reference

### Build Device Trees (DTBs)

**⚠️ CRITICAL: Use ONLY this method**

```bash
cd ${WORKSPACE}/Linux_for_Tegra/source

# Set kernel headers (REQUIRED!)
export KERNEL_HEADERS=${WORKSPACE}/Linux_for_Tegra/source/kernel/linux

# Build DTBs
make dtbs

# Output location (this is the ONLY correct path):
# ${WORKSPACE}/Linux_for_Tegra/source/kernel-devicetree/generic-dts/dtbs/tegra234-p3737-0000+p3701-0000-nv.dtb
```

**DO NOT use**: nvbuild.sh, kernel_out/ directory, or any other method!

### Submit a Test

```bash
# After building kernel
TIMESTAMP=$(date +%Y%m%d%H%M%S)
touch ${WORKSPACE}/autopilot/requests/pending/${TIMESTAMP}.request

# Wait ~5 minutes, then check results:
cat ${WORKSPACE}/autopilot/results/${TIMESTAMP}/panic.log
cat ${WORKSPACE}/autopilot/results/${TIMESTAMP}/hyp.log
```

### Check Test Status

```bash
# See all requests
ls -la ${WORKSPACE}/autopilot/requests/*/

# Monitor autopilot service
journalctl -u autopilot -f
```

### Key Files

| File | Purpose |
|------|---------|
| `orin_kernel_autopilot.py` | Main orchestration daemon |
| `BootHarness.py` | Boot sequence control and console monitoring |
| `BoardControl.py` | Hardware power control |
| `filter_*.py` | Log processing scripts |

### Key Directories on Host

| Directory | Purpose |
|-----------|---------|
| `requests/pending/` | Submit test requests here |
| `requests/processing/` | Currently running test |
| `requests/completed/` | Successfully completed tests |
| `requests/failed/` | Failed tests |
| `results/${TIMESTAMP}/` | Test output logs |

### Key Files on Target

| File | Purpose |
|------|---------|
| `/root/bin/update.sh` | Download kernel from host |
| `/boot/extlinux/extlinux.conf` | Boot menu configuration |
| `/lib/systemd/system/update.target` | systemd target for update mode |
| `/lib/systemd/system/update.service` | systemd service runs update.sh |
| `/boot/Image-6.17.0-tegra` | Test kernel (updated by autopilot) |
| `/boot/dtb/tegra234-p3737-0000+p3701-0000-nv.dtb` | Device tree blob |

## System Requirements

### Host System (192.168.101.100)
- Python 3 with `pexpect`, `pyserial`
- SSH access to boot control server (192.168.101.110)
- USB serial adapters (ttyACM0, ttyACM1)
- Kernel build environment

### Target Hardware (192.168.101.106)
- NVIDIA Jetson AGX Orin (Tegra234)
- UEFI firmware with extlinux support
- systemd with custom update.target/update.service
- SSH server, network connectivity
- Kernel image at `/boot/Image-6.17.0-tegra`

### Boot Control Server (192.168.101.110)
- USB relay board OR remote boot script
- SSH access from host

## Troubleshooting

### Test Stuck in Processing

**Symptom**: Request file in `processing/` directory for >10 minutes

**Causes**:
- Autopilot service crashed
- Board not responding to power cycle
- Network issues preventing boot

**Resolution**:
```bash
# Check autopilot service status
systemctl status autopilot

# Restart autopilot (moves processing back to pending)
systemctl restart autopilot

# Check board power
ssh 192.168.101.110 './boot.sh normal'
```

### No Results Generated

**Symptom**: Request completes but no logs in `results/`

**Causes**:
- Permission issues writing to results directory
- Disk full
- Log processing scripts failed

**Resolution**:
```bash
# Check disk space
df -h ${WORKSPACE}/autopilot/

# Check permissions
ls -la ${WORKSPACE}/autopilot/results/

# Check autopilot service logs
journalctl -u autopilot -n 100
```

### UART Not Responding

**Symptom**: Autopilot hangs waiting for UEFI prompt

**Causes**:
- USB serial adapter disconnected
- Wrong tty device
- Board not booting

**Resolution**:
```bash
# Check USB serial devices
ls -la /tmp/ttyACM*

# Test UART manually
screen /tmp/ttyACM0 115200

# Check board power LED
# Power cycle manually
ssh 192.168.101.110 './boot.sh normal'
```

### Kernel Download Timeout

**Symptom**: UpdateBootHarness times out after 180 seconds

**Causes**:
- Network connectivity issue
- SSH key authentication failed
- Firewall blocking SSH

**Resolution**:
```bash
# Test SSH from target to host (manual boot first)
ssh hlyytine@192.168.101.100 'echo test'

# Check network from target
ping 192.168.101.100

# Check SSH key authentication
ssh-copy-id hlyytine@192.168.101.100  # If needed
```

## Development Guide

### Adding New Log Filters

Create a new filter script in `${WORKSPACE}/autopilot/`:

```python
#!/usr/bin/env python3
import sys

for line in sys.stdin:
    # Filter logic here
    if "pattern" in line:
        sys.stdout.write(line)
```

Call it from `orin_kernel_autopilot.py`:
```python
with open(result_dir / 'input.log', "rb") as fin, \
     open(result_dir / 'output.log', "wb") as fout:
    subprocess.run(
        SCRIPT_DIR / 'my_filter.py',
        stdin=fin,
        stdout=fout,
        check=True
    )
```

### Adding New Boot Modes

Extend `BootHarness.py`:

```python
class MyCustomBootHarness(BootHarness):
    def __init__(self, board, tty, filename, hyp_tty, hyp_filename):
        super().__init__(board, tty, filename, hyp_tty, hyp_filename)
        self.boot_option = '1'  # Select extlinux option 1

    def run(self):
        super().run()  # Navigate UEFI menus

        # Custom wait logic
        self.child.expect(r'My custom pattern', timeout=60)
```

### Testing Changes Locally

```bash
# Test log filters directly
cat test_kernel.log | ./filter_nvhe_bug.py

# Test boot harness without full autopilot
python3 -c "
import BoardControl
import BootHarness

board = BoardControl.BoardControlRemote()
harness = BootHarness.UpdateBootHarness(
    board, '/tmp/ttyACM0', 'test.log',
    '/tmp/ttyACM1', 'test-hyp.log'
)
harness.run()
"
```

## Performance Metrics

**Typical Test Timeline**:
- Request detection: <1 second
- Update boot cycle: ~2 minutes
- Test boot cycle: ~2 minutes
- Log processing: ~10 seconds
- **Total**: ~5 minutes per test

**Bottlenecks**:
- UEFI boot time: ~30 seconds (firmware limitation)
- Kernel download: ~30 seconds (network speed)
- Boot menu navigation: ~20 seconds (UART timing)

**Throughput**:
- Sequential processing: 1 test / 5 minutes = 12 tests/hour
- Parallelization potential: 4 boards = 48 tests/hour

## Related Documentation

- **Main Project Docs**: [../../CLAUDE.md](../../CLAUDE.md)
- **Source Tree Docs**: [../../Linux_for_Tegra/source/CLAUDE.md](../../Linux_for_Tegra/source/CLAUDE.md)
- **Boot Issues**: [../docs/remaining_issues.md](../../docs/remaining_issues.md)

## Changelog

**2025-11-20**: Initial comprehensive documentation created
- overview.md: System overview and quick start
- boot-sequence.md: Boot process and menu system explained
- architecture.md: Technical implementation details
- extending-dtb-support.md: DTB upload implementation guide
- Updated main CLAUDE.md with references to autopilot docs

## Contributing

When modifying the autopilot system:

1. **Update documentation** alongside code changes
2. **Test thoroughly** on hardware before committing
3. **Update CHANGELOG** section in relevant docs
4. **Add troubleshooting entries** for new failure modes
5. **Update timing diagrams** if adding new phases

## Support

For questions or issues:
1. Check troubleshooting section above
2. Review relevant documentation file
3. Check autopilot service logs: `journalctl -u autopilot -f`
4. Inspect test results in `results/${TIMESTAMP}/`
5. File issue with full logs and reproduction steps
