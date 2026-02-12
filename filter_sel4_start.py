#!/usr/bin/env python3
"""
Filter seL4 log with robust start detection.

Usage: filter_sel4_start.py [binary_name] < input.log > output.log

Primary start marker is 'FS3:\\> <binary>' when the EFI shell launches the
binary. If that marker is absent (for example, network boot path), the filter
falls back to the first ELF/seL4 boot marker.
"""
import sys
import re

binary_name = sys.argv[1] if len(sys.argv) > 1 else 'sel4test.efi'

# Strip ANSI escape codes for pattern matching
ansi_escape = re.compile(r'\x1b\[[0-9;]*m')

# Match the FS3:\> prompt followed by the binary name.
fs3_pattern = re.compile(rf'FS3:\\>\s*{re.escape(binary_name)}', re.IGNORECASE)
# Fallback marker for non-UEFI-shell launch paths.
sel4_pattern = re.compile(r'(ELF-loader|seL4)', re.IGNORECASE)

found_start = False
for raw_line in sys.stdin.buffer:
    # Raw console captures can contain invalid UTF-8 bytes.
    line = raw_line.decode("utf-8", errors="ignore")
    # Strip ANSI codes for matching, but preserve original line for output
    clean_line = ansi_escape.sub('', line)
    if found_start:
        # Output with ANSI codes stripped for clean logs
        sys.stdout.write(clean_line)
        continue

    if fs3_pattern.search(clean_line):
        found_start = True
        continue

    if sel4_pattern.search(clean_line):
        found_start = True
        # For fallback mode, include the first detected seL4 line onward.
        sys.stdout.write(clean_line)
