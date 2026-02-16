# seL4 Autopilot Testing

Extends the pKVM autopilot framework to support seL4 EFI binary testing on NVIDIA Orin AGX.

## Overview

The `orin_kernel_autopilot.py` daemon executes tests based on the `profile` value
specified in each request, where `profile` is the root chain name. Chains are
loaded from `/home/hlyytine/autopilot/chains/<profile>.json`. There is no
request `type` routing.

seL4 EFI test flow (chain-defined):
1. Boot to stock Jetson Linux
2. Upload EFI binary via SCP to `/efiboot/{target_binary_name}`
3. Reboot and execute EFI boot command at UEFI prompt via `boot_efi`
4. Run the binary and capture output according to the chain
5. Optional post-processing (filters) to produce chain-defined log files
6. Recover to stock Linux (if defined in profile)

All logs live under `results/<timestamp>/console/` and are defined by chain steps.

## Quick Start

### Using the Client Library (Python)

```python
from sel4_client import submit_sel4_efi_test, wait_for_result, get_logs

# Submit a test
timestamp = submit_sel4_efi_test(
    binary_path='/home/hlyytine/tii-sel4/orinagx_sel4test/images/sel4test-driver-image-arm-orinagx',
    binary_name='sel4test.efi',
    description='sel4test with MMU debug markers',
    profile='sel4test'
)

# Wait for completion (up to 10 minutes)
result = wait_for_result(timestamp)

if result['status'] == 'completed':
    logs = get_logs(timestamp)
    print(logs)
```

### Using the Command Line

```bash
# Submit a test
./sel4_client.py submit /path/to/binary.efi --name mytest.efi --desc "Test description" --profile sel4test

# Submit and wait for result
./sel4_client.py submit /path/to/binary.efi --profile sel4test --wait

# Check status
./sel4_client.py status 20251212-143022

# List console logs
./sel4_client.py logs 20251212-143022

# Show console log contents
./sel4_client.py logs 20251212-143022 --show

# List requests
./sel4_client.py list --pending --completed --failed
```

### Manual Request (JSON file)

Create a `.request` file in `requests/pending/`:

```json
{
    "profile": "sel4test",
    "binary_path": "/absolute/path/to/sel4test.efi",
    "binary_name": "sel4test.efi",
    "description": "optional description",
    "build_config": {"arm_hyp": true, "platform": "orinagx"}
}
```

Defaults (AUTOPILOT_DIR/TTYs and queue names) are defined in `config.py` (SSOT).

## Directory Structure

```
~/tii-sel4/autopilot/
├── requests/
│   ├── pending/          # New requests go here
│   ├── processing/       # Currently running test
│   ├── completed/        # Successful tests
│   └── failed/           # Failed tests
├── results/
│   └── <timestamp>/
│       ├── console/      # Chain-defined log outputs
│       ├── upload.log    # Upload phase log (if defined by profile)
│       └── recovery.log  # Recovery phase log (if defined by profile)
├── binaries/             # Staging area for EFI binaries
├── seL4BootHarness.py    # seL4 boot harness classes
├── filter_sel4_start.py  # Log filter script
└── sel4_client.py        # Client library
```

## Output Files

After test completion, `results/<timestamp>/console/` contains **only** the logs
defined by the chain (raw UART captures and any filtered logs created by
`analyze_logs`). There are no fixed filenames enforced by code.

## Client Library API

```python
from sel4_client import (
    submit_sel4_efi_test,  # Submit a test, returns timestamp
    get_status,            # Check status: pending/processing/completed/failed
    wait_for_result,       # Block until completion
    get_logs,              # List console logs for a request
    get_request_info,      # Get original request metadata
    list_pending,          # List pending timestamps
    list_completed,        # List completed timestamps
    list_failed,           # List failed timestamps
)
```

## UEFI Command Dispatch

The `boot_efi` step is the canonical UEFI command mechanism:

1. Wait for UEFI prompt patterns
2. `mode=extlinux` sends `fs3:\\EFI\\BOOT\\BOOTAA64.EFI`
3. `mode=test_efi` sends `fs2:\\efiboot\\{target_binary_name}`
4. Chain continues with chain-defined output matching/capture

## Troubleshooting

**Test hangs during upload**: Check SSH connectivity to 192.168.101.112

**UEFI navigation fails**: The UEFI menu structure may have changed. Check the raw console log defined by the chain.

**Binary not found on target**: Verify `/efiboot/` exists and is writable on the target.

**Recovery fails**: Board may need manual power cycle. Check any recovery log defined by the chain.
