#!/usr/bin/env python3
"""Filter UART log to start from MB1 boot indication.

Drops all lines before the first MB1 boot message, which looks like:
[0000.063] I> MB1 (version: 1.4.0.4-t234-54845784-e89ea9bc)

This removes stray output from previous boots that may still be in the UART buffer.
"""
import sys
import re

MB1_PATTERN = re.compile(rb'\[\d+\.\d+\]\s+I>\s+MB1')

found_mb1 = False
for line in sys.stdin.buffer:
    if not found_mb1:
        if MB1_PATTERN.search(line):
            found_mb1 = True
            sys.stdout.buffer.write(line)
    else:
        sys.stdout.buffer.write(line)
