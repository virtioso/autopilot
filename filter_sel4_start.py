#!/usr/bin/env python3
"""
Filter seL4 log: strip everything up to and including 'FS3:\\> <binary>' line.

Usage: filter_sel4_start.py [binary_name] < input.log > output.log

The filter removes all bootloader and UEFI menu output, keeping only
the seL4 binary output that appears after the EFI shell command.
"""
import sys
import re

binary_name = sys.argv[1] if len(sys.argv) > 1 else 'sel4test.efi'

# Strip ANSI escape codes for pattern matching
ansi_escape = re.compile(r'\x1b\[[0-9;]*m')

# Match the FS3:\> prompt followed by the binary name
# Case insensitive, handles extra whitespace
pattern = re.compile(rf'FS3:\\>\s*{re.escape(binary_name)}', re.IGNORECASE)

found_start = False
for line in sys.stdin:
    # Strip ANSI codes for matching, but preserve original line for output
    clean_line = ansi_escape.sub('', line)
    if not found_start:
        if pattern.search(clean_line):
            found_start = True
        continue
    # Output with ANSI codes stripped for clean logs
    sys.stdout.write(clean_line)
