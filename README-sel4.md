# seL4 Autopilot Testing

Extends the pKVM autopilot framework to support seL4 EFI binary testing on NVIDIA Orin AGX.

## Overview

The unified `orin_kernel_autopilot.py` daemon handles both Linux kernel tests and seL4 EFI binary tests based on the request `type` field.

seL4 test flow:
1. Boot to stock Jetson Linux
2. Upload EFI binary via SCP to `/boot/efi/`
3. Reboot and navigate UEFI menus to EFI Shell
4. Run the binary and capture output until 30s quiescent
5. Filter log to strip bootloader/UEFI output
6. Recover to stock Linux for next test

## Quick Start

### Using the Client Library (Python)

```python
from sel4_client import submit_sel4_test, wait_for_result, get_sel4_log

# Submit a test
timestamp = submit_sel4_test(
    binary_path='/home/hlyytine/tii-sel4/orinagx_sel4test/images/sel4test-driver-image-arm-orinagx',
    binary_name='sel4test.efi',
    description='sel4test with MMU debug markers'
)

# Wait for completion (up to 10 minutes)
result = wait_for_result(timestamp)

if result['status'] == 'completed':
    log = get_sel4_log(timestamp)
    print(log)
```

### Multi-Run Tests (Python)

For stress testing or detecting intermittent failures, run the same binary multiple times:

```python
from sel4_client import submit_multi_run_test, wait_for_result, get_multi_run_logs

# Submit a 5-iteration test
timestamp = submit_multi_run_test(
    binary_path='/path/to/sel4test.efi',
    run_count=5,
    test_type='sel4',  # or 'linux' for kernel tests
    description='Stress test for boot reliability'
)

# Wait for completion (timeout scales with run_count)
result = wait_for_result(timestamp, timeout=300 * 5)

if result['status'] == 'completed':
    logs = get_multi_run_logs(timestamp)
    print(f"Completed: {logs['summary']['completed_runs']}/{logs['summary']['total_runs']}")
    for run in logs['runs']:
        print(f"Run {run['run_number']}: {len(run.get('sel4_log', ''))} bytes")
```

### Using the Command Line

```bash
# Submit a test
./sel4_client.py submit /path/to/binary.efi --name mytest.efi --desc "Test description"

# Submit and wait for result
./sel4_client.py submit /path/to/binary.efi --wait

# Check status
./sel4_client.py status 20251212-143022

# Get output
./sel4_client.py log 20251212-143022

# Get raw output (includes bootloader/UEFI)
./sel4_client.py log 20251212-143022 --raw

# List requests
./sel4_client.py list --pending --completed --failed
```

### Multi-Run Tests (Command Line)

```bash
# Submit a 5-run seL4 test
./sel4_client.py submit-multi /path/to/binary.efi --runs 5 --type sel4 --wait

# Submit a 10-run Linux kernel test
./sel4_client.py submit-multi /path/to/Image --runs 10 --type linux --wait

# Get summary of multi-run results
./sel4_client.py logs-multi 20251212-143022 --summary

# Get all logs from multi-run test
./sel4_client.py logs-multi 20251212-143022
```

### Manual Request (JSON file)

Create a `.request` file in `requests/pending/`:

```json
{
    "type": "sel4",
    "binary_path": "/path/to/sel4test.efi",
    "binary_name": "sel4test.efi",
    "description": "optional description"
}
```

For multi-run tests, add `multi_run` and `run_count`:

```json
{
    "type": "sel4",
    "binary_path": "/path/to/sel4test.efi",
    "binary_name": "sel4test.efi",
    "description": "stress test",
    "multi_run": true,
    "run_count": 5
}
```

## Directory Structure

```
~/pkvm/autopilot/
├── requests/
│   ├── pending/          # New requests go here
│   ├── processing/       # Currently running test
│   ├── completed/        # Successful tests
│   └── failed/           # Failed tests
├── results/
│   └── <timestamp>/
│       ├── sel4.log      # Filtered seL4 output (single-run)
│       ├── uart-raw.log  # Raw UART capture (single-run)
│       ├── upload.log    # Upload phase log
│       └── recovery.log  # Recovery phase log
├── binaries/             # Staging area for EFI binaries
├── seL4BootHarness.py    # seL4 boot harness classes
├── filter_sel4_start.py  # Log filter script
└── sel4_client.py        # Client library
```

### Multi-Run Results Structure

```
results/<timestamp>/
├── run_1/
│   ├── sel4.log          # Filtered output for run 1
│   ├── uart-raw.log      # Raw UART for run 1
│   ├── upload.log        # Upload log (first run only)
│   └── error.txt         # Error message (if run failed)
├── run_2/
│   ├── sel4.log
│   └── uart-raw.log
├── ...
├── run_N/
├── summary.json          # {"total_runs": N, "completed_runs": M, "failed_runs": F}
└── recovery.log          # Final recovery boot log
```

## Hardware Setup

| Component | Value |
|-----------|-------|
| Host PC | 192.168.101.100 |
| Target (Orin AGX) | 192.168.101.112 |
| Main UART | `/dev/ttyACM0` |
| Power control | USB relay via BoardControlLocal |

## Request Format

```json
{
    "type": "sel4",
    "binary_path": "/absolute/path/to/binary.efi",
    "binary_name": "name-on-target.efi",
    "description": "optional test description",
    "multi_run": false,
    "run_count": 1
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `type` | No | `"sel4"` or `"linux"` (default: `"linux"`) |
| `binary_path` | Yes | Absolute path to the EFI binary |
| `binary_name` | No | Filename to use on target (default: `"sel4test.efi"`) |
| `description` | No | Human-readable description |
| `multi_run` | No | Enable multi-run mode (default: `false`) |
| `run_count` | No | Number of boot iterations for multi-run (default: `1`) |

### Multi-Run Reboot Strategy

| Test Type | Run 1 | Runs 2-N |
|-----------|-------|----------|
| seL4 | Upload via SCP, SSH reboot | Hardware reboot (BoardControlLocal) |
| Linux (success) | Upload via SCP, SSH reboot | SSH reboot if reachable |
| Linux (failure) | Upload via SCP, SSH reboot | Hardware reboot fallback |

## Output Files

After test completion, `results/<timestamp>/` contains:

| File | Description |
|------|-------------|
| `sel4.log` | Filtered seL4 output (bootloader/UEFI stripped) |
| `uart-raw.log` | Complete UART capture |
| `upload.log` | Boot to Linux + SCP upload log |
| `recovery.log` | Recovery boot log |

## Client Library API

```python
from sel4_client import (
    # Single-run tests
    submit_sel4_test,      # Submit a single test, returns timestamp
    get_status,            # Check status: pending/processing/completed/failed
    wait_for_result,       # Block until completion
    get_sel4_log,          # Get filtered seL4 output
    get_raw_log,           # Get raw UART output
    get_request_info,      # Get original request metadata
    list_pending,          # List pending timestamps
    list_completed,        # List completed timestamps
    list_failed,           # List failed timestamps

    # Multi-run tests
    submit_multi_run_test, # Submit multi-run test, returns timestamp
    get_multi_run_logs,    # Get logs from all runs as dict
)
```

### Multi-Run API

```python
# Submit multi-run test
timestamp = submit_multi_run_test(
    binary_path='/path/to/binary.efi',
    run_count=5,              # Number of boot iterations
    binary_name='test.efi',   # Name on target
    test_type='sel4',         # 'sel4' or 'linux'
    description='Stress test'
)

# Get all logs
logs = get_multi_run_logs(timestamp)
# Returns:
# {
#     'runs': [
#         {'run_number': 1, 'sel4_log': '...', 'raw_log': '...'},
#         {'run_number': 2, 'sel4_log': '...', 'error': '...'},  # if failed
#         ...
#     ],
#     'summary': {'total_runs': 5, 'completed_runs': 4, 'failed_runs': 1}
# }
```

## UEFI Navigation Sequence

The `SeL4RunHarness` navigates UEFI menus automatically:

1. Wait for `"Enter to continue boot."` → send ESC
2. Wait for `"Select Entry"` → Down, Down, Enter (Boot Manager)
3. Wait for `"Esc=Exit"` → Up, Enter (UEFI Shell)
4. Wait for `"Shell>"` → send `fs3:`
5. Wait for `"FS3:\>"` → send binary name
6. Capture output until 5 seconds with no output

## Troubleshooting

**Test hangs during upload**: Check SSH connectivity to 192.168.101.112

**UEFI navigation fails**: The UEFI menu structure may have changed. Check `uart-raw.log` for actual menu output.

**Binary not found on target**: Verify `/boot/efi/` is mounted and writable on the target.

**Recovery fails**: Board may need manual power cycle. Check `recovery.log`.
