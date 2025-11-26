#!/usr/bin/env python3
"""
SMMU Fault Filter - Extracts and summarizes SMMU fault information from kernel log
"""
import sys
import re
from collections import defaultdict

# Patterns to detect
PATTERNS = [
    r'arm-smmu.*Unexpected global fault',
    r'arm-smmu.*GFSR',
    r'nvidia_smmu_global_fault_inst.*callbacks suppressed',
]

# For tracking statistics
fault_count = 0
gfsr_values = defaultdict(int)
gfsynr1_values = defaultdict(int)
suppression_count = 0

print("=" * 80)
print("SMMU FAULT ANALYSIS")
print("=" * 80)
print()

# Use binary mode with error handling to avoid Unicode decode errors
sys.stdin.reconfigure(encoding='utf-8', errors='replace')

for line in sys.stdin:
    # Check if line matches any SMMU fault pattern
    is_smmu_line = any(re.search(pattern, line, re.IGNORECASE) for pattern in PATTERNS)

    if is_smmu_line:
        # Print the line
        sys.stdout.write(line)

        # Track "Unexpected global fault" occurrences
        if 'Unexpected global fault' in line:
            fault_count += 1

        # Extract GFSR values
        gfsr_match = re.search(r'GFSR\s+0x([0-9a-fA-F]+)', line)
        if gfsr_match:
            gfsr = gfsr_match.group(1)
            gfsr_values[gfsr] += 1

        # Extract GFSYNR1 values (contains Stream ID)
        gfsynr1_match = re.search(r'GFSYNR1\s+0x([0-9a-fA-F]+)', line)
        if gfsynr1_match:
            gfsynr1 = gfsynr1_match.group(1)
            gfsynr1_values[gfsynr1] += 1

        # Track callback suppression
        if 'callbacks suppressed' in line:
            suppression_match = re.search(r'(\d+)\s+callbacks suppressed', line)
            if suppression_match:
                suppression_count += int(suppression_match.group(1))

# Print summary at the end
print()
print("=" * 80)
print("SMMU FAULT SUMMARY")
print("=" * 80)
print(f"Total 'Unexpected global fault' messages: {fault_count}")
print(f"Total callbacks suppressed: {suppression_count}")
print()

if gfsr_values:
    print("GFSR (Global Fault Status Register) values:")
    for gfsr, count in sorted(gfsr_values.items(), key=lambda x: x[1], reverse=True):
        print(f"  0x{gfsr}: {count} occurrences")
        # Decode GFSR bits
        gfsr_int = int(gfsr, 16)
        if gfsr_int & 0x80000000:
            print(f"    - Bit 31: Multi (multiple faults)")
        if gfsr_int & 0x00000002:
            print(f"    - Bit 1: External abort on translation table walk")
    print()

if gfsynr1_values:
    print("GFSYNR1 values (contains Stream ID in bits 0-15):")
    for gfsynr1, count in sorted(gfsynr1_values.items(), key=lambda x: x[1], reverse=True):
        gfsynr1_int = int(gfsynr1, 16)
        stream_id = gfsynr1_int & 0xFFFF
        print(f"  0x{gfsynr1}: {count} occurrences (Stream ID: 0x{stream_id:x})")
    print()

print("=" * 80)
print("ANALYSIS HINTS")
print("=" * 80)
print("- GFSR 0x80000002 typically indicates multiple external aborts on translation")
print("- GFSYNR1 contains the Stream ID of the device causing the fault")
print("- High callback suppression indicates sustained fault storms")
print("- Different Stream IDs suggest multiple devices experiencing faults")
print("=" * 80)
