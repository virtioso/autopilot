#!/usr/bin/env python3
"""Filter UART log to start from kernel boot.

Drops all lines before "Booting Linux on physical CPU", removing
bootloader output (MB1, MB2, UEFI, extlinux) to leave only kernel messages.
"""
import sys

KERNEL_START = b'Booting Linux on physical CPU'

found_kernel = False
for line in sys.stdin.buffer:
    if not found_kernel:
        if KERNEL_START in line:
            found_kernel = True
            sys.stdout.buffer.write(line)
    else:
        sys.stdout.buffer.write(line)
