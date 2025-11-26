#!/usr/bin/env python3
import sys

# Match multiple panic patterns (EL2 hypervisor panics and EL1 kernel panics in KVM code)
PATTERNS = [
    "nVHE hyp BUG",                 # EL2 hypervisor panics
    "kernel BUG at arch/arm64/kvm/", # Kernel panics in KVM code
    "Kernel panic",                  # Generic kernel panics
]

printing = False

# Use binary mode with error handling to avoid Unicode decode errors
sys.stdin.reconfigure(encoding='utf-8', errors='replace')

for line in sys.stdin:
    if not printing:
        # Check if any pattern matches
        if any(pattern in line for pattern in PATTERNS):
            printing = True
        else:
            continue  # vielä ei tulosteta mitään

    # Kun printing == True, tulostetaan nykyinen ja kaikki seuraavat rivit
    sys.stdout.write(line)
