#!/usr/bin/env python3
"""Filter capdl-loader output, removing bootloader/UEFI noise.

Usage: filter_capdl_start.py < input.log > output.log

Filters ttyACM0 output to keep only seL4/capdl-loader output,
stripping UEFI shell and bootloader noise.
"""

import re
import sys

# Start markers for capdl-loader output
CAPDL_START_MARKERS = [
    b"ELF-loader started",
    b"Bootstrapping kernel",
    b"Booting all finished",
    b"Loading kernel",
]

# ANSI escape sequence pattern
ANSI_ESCAPE = re.compile(rb'\x1b\[[0-9;]*[a-zA-Z]')


def filter_capdl_log(input_stream, output_stream):
    """
    Filter capdl-loader output.

    Rules:
    1. Skip everything before start markers
    2. Keep all output after start marker
    3. Strip ANSI escape sequences
    """
    started = False

    for line in input_stream:
        # Check for start markers
        if not started:
            for marker in CAPDL_START_MARKERS:
                if marker in line:
                    started = True
                    break

        if started:
            # Strip ANSI escapes
            clean_line = ANSI_ESCAPE.sub(b'', line)
            output_stream.write(clean_line)


if __name__ == "__main__":
    # Read from stdin, write to stdout (binary mode)
    filter_capdl_log(sys.stdin.buffer, sys.stdout.buffer)
