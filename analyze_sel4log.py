#!/usr/bin/env python3
"""
sel4log analyzer - RAS error statistics and symbolization for seL4 test logs.

Usage:
    ./analyze_sel4log.py <sel4.log> [--kernel <kernel.elf>] [--app <sel4test-driver>]
    ./analyze_sel4log.py <sel4.log> --json-only
"""

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Default binary paths for symbolization
DEFAULT_KERNEL = "/home/hlyytine/tii-sel4/orinagx_sel4test/kernel/kernel.elf"
DEFAULT_APP = "/home/hlyytine/tii-sel4/orinagx_sel4test/apps/sel4test-driver/sel4test-driver"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze sel4.log files for RAS errors and test statistics"
    )
    parser.add_argument("logfile", help="Path to sel4.log file")
    parser.add_argument("--kernel", default=DEFAULT_KERNEL,
                        help=f"Path to kernel.elf (default: {DEFAULT_KERNEL})")
    parser.add_argument("--app", default=DEFAULT_APP,
                        help=f"Path to sel4test-driver binary (default: {DEFAULT_APP})")
    parser.add_argument("--json-only", action="store_true",
                        help="Output JSON only, no text summary")
    parser.add_argument("--no-symbolize", action="store_true",
                        help="Skip addr2line symbolization")
    return parser.parse_args()


def decode_ras_addr(addr_int):
    """Decode RAS ADDR field bits.

    Bit 63: NS (Non-Secure) flag
    Bits 62-0: Physical address
    """
    ns_bit = (addr_int >> 63) & 1
    raw = addr_int & 0x7FFFFFFFFFFFFFFF
    return {
        'raw_hex': hex(addr_int),
        'physical': hex(raw),
        'physical_int': raw,
        'ns_bit': ns_bit,
        'below_dram': raw < 0x80000000,
        'is_zero': raw == 0,
    }


def symbolize_address(addr, el, kernel_elf, app_elf):
    """Use aarch64-linux-gnu-addr2line to symbolize an address."""
    if el == 2:
        elf = kernel_elf
    else:  # EL=0 userspace
        elf = app_elf

    if not Path(elf).exists():
        return {'function': '??', 'location': f'(no {Path(elf).name})'}

    try:
        result = subprocess.run(
            ['aarch64-linux-gnu-addr2line', '-f', '-e', elf, hex(addr)],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split('\n')
        func = lines[0] if lines else '??'
        loc = lines[1] if len(lines) > 1 else '??'
        return {'function': func, 'location': loc}
    except Exception as e:
        return {'function': '??', 'location': f'(error: {e})'}


def parse_log(logfile):
    """Parse sel4.log and extract test results and RAS errors."""
    stats = {
        'file': str(logfile),
        'timestamp': datetime.now().isoformat(),
        'iterations': 0,
        'total_iterations': 0,
        'tests': [],
        'test_results': defaultdict(lambda: {'passed': 0, 'failed': 0}),
        'ras_errors': [],
        'errors_by_test': defaultdict(int),
        'errors_by_iteration': defaultdict(int),
        'scc_count': 0,
        'aci_count': 0,
        'unique_addrs': defaultdict(int),
        'elr_addresses': defaultdict(lambda: {'count': 0, 'el': None}),
    }

    current_test = None
    current_iteration = 0
    current_elr = None
    current_spsr_el = None
    in_ras_block = False

    # Regex patterns
    re_iteration = re.compile(r'=== Stress iteration (\d+)/(\d+) ===')
    re_test_start = re.compile(r'Starting test \d+: ([A-Z][A-Z0-9_]+)')
    re_test_result = re.compile(r'Test (\S+) (passed|failed)')
    re_elr = re.compile(r'ELR_EL3.*:\s*(0x[0-9a-fA-F]+)')
    re_spsr = re.compile(r'SPSR_EL3.*EL=(\d)')
    re_ras_error = re.compile(r'RAS Uncorrectable Error in (SCC|ACI)')
    re_addr = re.compile(r'ADDR\s*=\s*(0x[0-9a-fA-F]+)')
    re_status = re.compile(r'Status\s*=\s*(0x[0-9a-fA-F]+)')
    re_serr = re.compile(r'SERR\s*=\s*([^:]+)')

    with open(logfile, 'r') as f:
        for line in f:
            # Track iterations
            m = re_iteration.search(line)
            if m:
                current_iteration = int(m.group(1))
                stats['total_iterations'] = int(m.group(2))
                stats['iterations'] = max(stats['iterations'], current_iteration)
                continue

            # Track test starts
            m = re_test_start.search(line)
            if m:
                current_test = m.group(1)
                if current_test not in stats['tests']:
                    stats['tests'].append(current_test)
                continue

            # Track test results
            m = re_test_result.search(line)
            if m:
                test_name = m.group(1)
                result = m.group(2)
                stats['test_results'][test_name][result] += 1
                continue

            # Track ELR_EL3 (PC at interrupt)
            m = re_elr.search(line)
            if m:
                current_elr = int(m.group(1), 16)
                continue

            # Track SPSR_EL3 for exception level
            m = re_spsr.search(line)
            if m:
                current_spsr_el = int(m.group(1))
                continue

            # Track RAS errors
            m = re_ras_error.search(line)
            if m:
                error_type = m.group(1)
                if error_type == 'SCC':
                    stats['scc_count'] += 1
                else:
                    stats['aci_count'] += 1

                # Count by test and iteration
                if current_test:
                    stats['errors_by_test'][current_test] += 1
                stats['errors_by_iteration'][current_iteration] += 1

                # Record ELR if we have one
                if current_elr is not None:
                    key = current_elr
                    stats['elr_addresses'][key]['count'] += 1
                    if current_spsr_el is not None:
                        stats['elr_addresses'][key]['el'] = current_spsr_el

                in_ras_block = True
                continue

            # Extract ADDR from RAS error block
            if in_ras_block:
                m = re_addr.search(line)
                if m:
                    addr = int(m.group(1), 16)
                    stats['unique_addrs'][addr] += 1

                # End of RAS block
                if '******' in line and 'RAS' not in line:
                    in_ras_block = False
                    current_elr = None
                    current_spsr_el = None

    return stats


def generate_text_summary(stats, kernel_elf, app_elf, do_symbolize=True):
    """Generate human-readable text summary."""
    lines = []
    lines.append("=" * 60)
    lines.append("sel4.log Analysis")
    lines.append("=" * 60)
    lines.append(f"File: {stats['file']}")
    lines.append("")

    # Test summary
    lines.append("TEST SUMMARY")
    lines.append("-" * 40)
    total_iterations = stats['iterations'] or stats['total_iterations'] or 1
    tests_per_iter = len(stats['tests'])
    total_runs = sum(r['passed'] + r['failed'] for r in stats['test_results'].values())
    total_passed = sum(r['passed'] for r in stats['test_results'].values())
    total_failed = sum(r['failed'] for r in stats['test_results'].values())

    lines.append(f"  Total iterations: {total_iterations}")
    lines.append(f"  Tests per iteration: {tests_per_iter}")
    lines.append(f"  Total test runs: {total_runs}")
    if total_runs > 0:
        lines.append(f"  Passed: {total_passed} ({100*total_passed/total_runs:.1f}%)")
        lines.append(f"  Failed: {total_failed} ({100*total_failed/total_runs:.1f}%)")

    # Tests with errors
    tests_with_errors = [t for t, c in stats['errors_by_test'].items() if c > 0]
    lines.append(f"  Tests with RAS errors: {len(tests_with_errors)}/{tests_per_iter}")
    if tests_with_errors:
        lines.append(f"    ({', '.join(tests_with_errors)})")

    # Clean iterations
    clean_iters = sum(1 for i in range(1, total_iterations + 1)
                      if stats['errors_by_iteration'].get(i, 0) == 0)
    lines.append(f"  Clean iterations: {clean_iters}/{total_iterations}")
    lines.append("")

    # RAS error summary
    lines.append("RAS ERROR SUMMARY")
    lines.append("-" * 40)
    lines.append(f"  SCC errors: {stats['scc_count']}")
    lines.append(f"  ACI errors: {stats['aci_count']}")
    total_errors = stats['scc_count'] + stats['aci_count']
    if total_iterations > 0:
        lines.append(f"  Error rate: {total_errors/total_iterations:.2f}/iteration")
    lines.append("")

    # Errors by test
    if stats['errors_by_test']:
        lines.append("ERRORS BY TEST")
        lines.append("-" * 40)
        total_by_test = sum(stats['errors_by_test'].values())
        # Sort by error count descending
        sorted_tests = sorted(stats['errors_by_test'].items(),
                             key=lambda x: x[1], reverse=True)
        max_name_len = max(len(t) for t, _ in sorted_tests) if sorted_tests else 20
        for test_name, count in sorted_tests:
            pct = 100 * count / total_by_test if total_by_test > 0 else 0
            lines.append(f"  {test_name:{max_name_len}}: {count:5} ({pct:5.1f}%)")
        # Show tests with 0 errors
        for test in stats['tests']:
            if test not in stats['errors_by_test']:
                lines.append(f"  {test:{max_name_len}}: {0:5} ({0:5.1f}%)")
        lines.append("")

    # Unique error addresses
    if stats['unique_addrs']:
        lines.append("UNIQUE ERROR ADDRESSES (ADDR field)")
        lines.append("-" * 40)
        sorted_addrs = sorted(stats['unique_addrs'].items(),
                             key=lambda x: x[1], reverse=True)[:10]
        for addr, count in sorted_addrs:
            decoded = decode_ras_addr(addr)
            flags = []
            if decoded['below_dram']:
                flags.append("below DRAM")
            if decoded['is_zero']:
                flags.append("ZERO")
            flags.append(f"NS={decoded['ns_bit']}")
            lines.append(f"  {decoded['physical']}: {count:4}x ({', '.join(flags)})")
        if len(stats['unique_addrs']) > 10:
            lines.append(f"  ... and {len(stats['unique_addrs']) - 10} more unique addresses")
        lines.append("")

    # ELR addresses (PC at interrupt)
    if stats['elr_addresses'] and do_symbolize:
        lines.append("TOP ELR ADDRESSES (PC at RAS interrupt)")
        lines.append("-" * 40)
        sorted_elrs = sorted(stats['elr_addresses'].items(),
                            key=lambda x: x[1]['count'], reverse=True)[:15]
        for addr, info in sorted_elrs:
            el = info.get('el', '?')
            count = info['count']
            sym = symbolize_address(addr, el, kernel_elf, app_elf)
            el_str = f"EL{el}" if el is not None else "EL?"
            binary = "kernel" if el == 2 else "sel4test"
            func = sym['function']
            if func == '??' or func == '':
                func = f"0x{addr:x}"
            lines.append(f"  {hex(addr)} ({el_str}): {count:3}x - {func} [{binary}]")
        lines.append("")

    return '\n'.join(lines)


def generate_json_output(stats, kernel_elf, app_elf, do_symbolize=True):
    """Generate JSON output structure."""
    total_iterations = stats['iterations'] or stats['total_iterations'] or 1
    tests_per_iter = len(stats['tests'])
    total_runs = sum(r['passed'] + r['failed'] for r in stats['test_results'].values())
    total_passed = sum(r['passed'] for r in stats['test_results'].values())
    total_failed = sum(r['failed'] for r in stats['test_results'].values())

    tests_with_errors = [t for t, c in stats['errors_by_test'].items() if c > 0]
    clean_iters = sum(1 for i in range(1, total_iterations + 1)
                      if stats['errors_by_iteration'].get(i, 0) == 0)

    # Decode addresses
    decoded_addrs = []
    for addr, count in sorted(stats['unique_addrs'].items(),
                              key=lambda x: x[1], reverse=True):
        d = decode_ras_addr(addr)
        decoded_addrs.append({
            'addr': d['raw_hex'],
            'physical': d['physical'],
            'count': count,
            'ns': d['ns_bit'],
            'below_dram': d['below_dram'],
            'is_zero': d['is_zero'],
        })

    # Symbolize ELR addresses
    elr_analysis = []
    if do_symbolize:
        for addr, info in sorted(stats['elr_addresses'].items(),
                                 key=lambda x: x[1]['count'], reverse=True):
            el = info.get('el')
            sym = symbolize_address(addr, el, kernel_elf, app_elf)
            elr_analysis.append({
                'addr': hex(addr),
                'el': el,
                'count': info['count'],
                'function': sym['function'],
                'location': sym['location'],
            })

    # Build errors by iteration list
    errors_by_iter_list = [stats['errors_by_iteration'].get(i, 0)
                           for i in range(1, total_iterations + 1)]

    return {
        'file': stats['file'],
        'timestamp': stats['timestamp'],
        'summary': {
            'iterations': total_iterations,
            'tests_per_iter': tests_per_iter,
            'total_runs': total_runs,
            'passed': total_passed,
            'failed': total_failed,
            'tests_with_errors': tests_with_errors,
            'clean_iterations': clean_iters,
        },
        'ras_errors': {
            'scc_count': stats['scc_count'],
            'aci_count': stats['aci_count'],
            'total': stats['scc_count'] + stats['aci_count'],
            'by_test': dict(stats['errors_by_test']),
            'by_iteration': errors_by_iter_list,
        },
        'unique_addrs': decoded_addrs,
        'elr_analysis': elr_analysis,
        'test_results': {k: dict(v) for k, v in stats['test_results'].items()},
    }


def main():
    args = parse_args()

    logfile = Path(args.logfile)
    if not logfile.exists():
        print(f"Error: File not found: {logfile}", file=sys.stderr)
        sys.exit(1)

    # Parse the log
    stats = parse_log(logfile)

    # Generate outputs
    do_symbolize = not args.no_symbolize

    if not args.json_only:
        text_output = generate_text_summary(stats, args.kernel, args.app, do_symbolize)
        print(text_output)

    # Generate and write JSON
    json_output = generate_json_output(stats, args.kernel, args.app, do_symbolize)
    json_file = logfile.with_suffix('.analysis.json')
    with open(json_file, 'w') as f:
        json.dump(json_output, f, indent=2)

    if not args.json_only:
        print(f"JSON output written to: {json_file}")


if __name__ == '__main__':
    main()
