#!/usr/bin/env python3
import sys

PATTERN = "hyp"
printing = False

# Use binary mode with error handling to avoid Unicode decode errors
sys.stdin.reconfigure(encoding='utf-8', errors='replace')

for line in sys.stdin:
    if not printing:
        if PATTERN in line:
            printing = True
        else:
            continue  # vielä ei tulosteta mitään

    # Kun printing == True, tulostetaan nykyinen ja kaikki seuraavat rivit
    sys.stdout.write(line)
