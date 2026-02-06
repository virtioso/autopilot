#!/usr/bin/env python3
"""Filter VM console output from ttyACM1.

Usage: filter_vm_console.py < input.log > output.log

Filters ttyACM1 output to keep Linux kernel and userspace output,
stripping any noise before Linux boot.
"""

import re
import sys

# VM console start markers (any of these indicates Linux boot started)
VM_START_MARKERS = [
    b"Linux version",
    b"Booting Linux",
    b"[    0.000000]",  # First kernel log
]

# ANSI escape sequence pattern
ANSI_ESCAPE = re.compile(rb'\x1b\[[0-9;]*[a-zA-Z]')


def filter_vm_log(input_stream, output_stream):
    """
    Filter VM console output.

    Rules:
    1. Skip everything before Linux boot markers
    2. Keep all output after start marker
    3. Strip ANSI escape sequences
    """
    started = False

    for line in input_stream:
        # Check for start markers
        if not started:
            for marker in VM_START_MARKERS:
                if marker in line:
                    started = True
                    break

        if started:
            # Strip ANSI escapes
            clean_line = ANSI_ESCAPE.sub(b'', line)
            output_stream.write(clean_line)


if __name__ == "__main__":
    # Read from stdin, write to stdout (binary mode)
    filter_vm_log(sys.stdin.buffer, sys.stdout.buffer)
