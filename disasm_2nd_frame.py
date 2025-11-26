#!/usr/bin/env python3
import sys
import re
import subprocess
import os

if len(sys.argv) != 2:
    print(f"Usage: {sys.argv[0]} <panic-log>", file=sys.stderr)
    sys.exit(1)

logfile = sys.argv[1]

# Regex to detect the start of a call trace (case-insensitive)
pat_call = re.compile(r'call trace', re.IGNORECASE)
# Regex to extract symbol from lines like:
# "[<ffff...>] some_symbol+0xNNN/0xMMM"
pat_sym = re.compile(r']\s+([0-9A-Za-z_\.]+)\+0x[0-9a-fA-F]+/0x[0-9a-fA-F]+')

seen_call = False
frame_syms = []

with open(logfile, "r", errors="replace") as f:
    for line in f:
        if not seen_call:
            # Wait until we encounter the first "call trace" block
            if pat_call.search(line):
                seen_call = True
            continue

        # Inside the call trace: try to parse a frame symbol
        m = pat_sym.search(line)
        if m:
            frame_syms.append(m.group(1))
            if len(frame_syms) == 2:
                # Second frame found (second from top in call trace)
                symbol = frame_syms[1]
                break

        # Optional: if there is some explicit "end of call trace" marker, we could stop here.
        # This is just a safety net; most kernels don't print such a line.
        if "call trace" in line.lower() and "end" in line.lower():
            break
    else:
        symbol = None

if symbol is None:
    print("Could not find second frame symbol in first call trace", file=sys.stderr)
    sys.exit(2)


print(f"# Found offending symbol: {symbol}", file=sys.stderr)
#print(f"# Disassembling (after prefix strip): {objdump_symbol}", file=sys.stderr)

# Run objdump; use WORKSPACE env var or fall back to default
workspace = os.environ.get('WORKSPACE', '/home/hlyytine/pkvm')
kvm_nvhe_path = f"{workspace}/Linux_for_Tegra/source/kernel/linux/arch/arm64/kvm/hyp/nvhe/kvm_nvhe.o"
subprocess.run(
    ["aarch64-linux-gnu-objdump", "-S", kvm_nvhe_path, f"--disassemble={symbol}"],
    check=False,
)
