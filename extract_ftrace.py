#!/usr/bin/env python3
"""
Extract and decompress ftrace binary data from a seL4 log.

Reads a log file, extracts the binary transfer section, decodes base64,
decompresses LZ4, and saves:
  - ftrace.bin: Raw decompressed ftrace entries (16-bit values)
  - ftrace.meta: JSON with header info and dictionary

Usage:
    extract_ftrace.py <logfile> <output_dir>
    extract_ftrace.py <logfile>  # Writes to same directory as input

Exit codes:
    0: Success (ftrace extracted)
    1: Error
    2: No binary transfer found (not an error, just no ftrace data)
"""

import base64
import json
import re
import sys
from pathlib import Path

# Use system python3-lz4 package
import lz4.block


def extract_ftrace(log_path: Path, output_dir: Path) -> bool:
    """
    Extract ftrace from log file.

    Returns True if ftrace was found and extracted, False otherwise.
    """
    log_content = log_path.read_text()

    # Find binary transfer section
    start_marker = "=== BINARY TRANSFER START ==="
    end_marker = "=== BINARY TRANSFER END ==="

    start_idx = log_content.find(start_marker)
    end_idx = log_content.find(end_marker)

    if start_idx == -1 or end_idx == -1:
        return False  # No binary transfer

    transfer_section = log_content[start_idx:end_idx + len(end_marker)]
    lines = transfer_section.split('\n')

    # Parse header, dictionary, and data
    header = {}
    dictionary = {}
    base64_lines = []
    block_data = []  # For v3 streaming format
    current_block = None
    tail_raw_entries = 0
    tail_base64_lines = []
    expected_checksum = 0

    state = "searching"

    for line in lines:
        line = line.strip()

        if state == "searching":
            if line == "=== BINARY TRANSFER START ===":
                state = "header"
            continue

        elif state == "header":
            if line == "===":
                state = "dictionary"
                continue

            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()

                if key in ("TYPE", "CHECKSUM"):
                    header[key] = value
                elif key in ("VERSION", "ENTRIES", "TOTAL_ENTRIES", "TOTAL_LOGGED",
                           "BLOCKS", "DICT_SIZE", "DATA_SIZE", "RAW_SIZE",
                           "COMPRESSED_SIZE", "TOTAL_COMPRESSED", "DUMP_REASON_CODE"):
                    try:
                        header[key] = int(value)
                    except ValueError:
                        header[key] = value
                elif key in ("OVERFLOW", "STORAGE_FULL"):
                    header[key] = value == "YES"
                elif key == "DUMP_REASON":
                    header[key] = value

        elif state == "dictionary":
            if line == "===":
                state = "data"
                continue

            if ':' in line:
                parts = line.split(':')
                if len(parts) == 2:
                    try:
                        idx = int(parts[0])
                        addr = int(parts[1], 16)
                        dictionary[idx] = addr
                    except ValueError:
                        pass

        elif state == "data":
            if line == "===":
                # End of data section
                if current_block is not None:
                    block_data.append(current_block)
                    current_block = None
                state = "footer"
                continue

            # Check for v3 BLOCK: header
            if line.startswith("BLOCK:"):
                if current_block is not None:
                    block_data.append(current_block)
                parts = line.split(':')
                if len(parts) == 4:
                    try:
                        current_block = {
                            'compressed_size': int(parts[2]),
                            'uncompressed_size': int(parts[3]),
                            'base64_lines': []
                        }
                    except ValueError:
                        current_block = None
                continue

            if line.startswith("TAIL_RAW_ENTRIES:"):
                if current_block is not None:
                    block_data.append(current_block)
                    current_block = None
                try:
                    tail_raw_entries = int(line.split(':', 1)[1])
                except ValueError:
                    tail_raw_entries = 0
                continue

            # Base64 line
            if line and re.match(r'^[A-Za-z0-9+/=]+$', line):
                if current_block is not None:
                    current_block['base64_lines'].append(line)
                elif tail_raw_entries > 0:
                    tail_base64_lines.append(line)
                else:
                    base64_lines.append(line)

        elif state == "footer":
            if line.startswith("CHECKSUM:"):
                try:
                    expected_checksum = int(line.split(':')[1].strip(), 16)
                    header['expected_checksum'] = expected_checksum
                except ValueError:
                    pass

    # Strict schema enforcement for new dump format.
    if 'DUMP_REASON' not in header or 'DUMP_REASON_CODE' not in header:
        print("Error: Unsupported legacy ftrace dump format (missing DUMP_REASON fields)", file=sys.stderr)
        return False

    # Determine format and decompress
    is_stream = header.get('TYPE') == 'FTRACE_STREAM' or header.get('VERSION', 0) >= 3

    all_raw_data = []

    if is_stream and block_data:
        # V3 streaming format with multiple blocks
        for block in block_data:
            try:
                all_b64 = ''.join(block['base64_lines'])
                data_only = all_b64.replace('=', '')

                # Fix padding
                remainder = len(data_only) % 4
                if remainder == 1:
                    data_only = data_only[:-1]  # Truncate invalid char
                    remainder = 0
                if remainder == 2:
                    data_only += '=='
                elif remainder == 3:
                    data_only += '='

                compressed = base64.b64decode(data_only)
                raw = lz4.block.decompress(compressed,
                                           uncompressed_size=block['uncompressed_size'])
                all_raw_data.append(raw)
            except Exception as e:
                print(f"Warning: Block decompression failed: {e}", file=sys.stderr)
                continue

        if tail_raw_entries > 0 and tail_base64_lines:
            try:
                tail_b64 = ''.join(tail_base64_lines)
                tail_data = base64.b64decode(tail_b64)
                expected_tail_size = tail_raw_entries * 2
                if len(tail_data) < expected_tail_size:
                    print(
                        f"Error: tail raw payload too short ({len(tail_data)} < {expected_tail_size})",
                        file=sys.stderr
                    )
                    return False
                all_raw_data.append(tail_data[:expected_tail_size])
            except Exception as e:
                print(f"Error: Failed to decode tail raw payload: {e}", file=sys.stderr)
                return False
    else:
        # V2 single block format
        try:
            all_b64 = ''.join(base64_lines)
            data_only = all_b64.replace('=', '')

            # Fix padding
            remainder = len(data_only) % 4
            if remainder == 1:
                data_only = data_only[:-1]
                remainder = 0
            if remainder == 2:
                data_only += '=='
            elif remainder == 3:
                data_only += '='

            compressed = base64.b64decode(data_only)

            raw_size = header.get('RAW_SIZE', 0)
            if raw_size > 0:
                raw = lz4.block.decompress(compressed, uncompressed_size=raw_size)
            else:
                # Not compressed (v1 format)
                raw = compressed

            all_raw_data.append(raw)
        except Exception as e:
            print(f"Error: Decompression failed: {e}", file=sys.stderr)
            return False

    if not all_raw_data:
        print("Error: No data extracted", file=sys.stderr)
        return False

    # Combine all raw data
    raw_data = b''.join(all_raw_data)

    # Write outputs
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write raw binary
    bin_path = output_dir / 'ftrace.bin'
    bin_path.write_bytes(raw_data)

    # Write metadata
    meta = {
        'header': header,
        'dictionary': {str(k): v for k, v in dictionary.items()},  # JSON keys must be strings
        'raw_size': len(raw_data),
        'entry_count': len(raw_data) // 2  # 16-bit entries
    }
    meta_path = output_dir / 'ftrace.meta'
    meta_path.write_text(json.dumps(meta, indent=2))

    print(f"Extracted ftrace: {len(raw_data)} bytes, {len(raw_data)//2} entries", file=sys.stderr)

    # Convert to indexed format using Rust tool (if available)
    idx_path = output_dir / 'ftrace.idx'
    indexer_paths = [
        Path('/home/hlyytine/tii-sel4/kernel/tools/ftrace-index-rs'),
        Path('/home/hlyytine/tii-sel4/kernel/tools/ftrace-index/target/release/ftrace-index'),
    ]

    indexer = None
    for p in indexer_paths:
        if p.exists():
            indexer = p
            break

    if indexer:
        import subprocess
        try:
            result = subprocess.run([
                str(indexer),
                '--binary', str(bin_path),
                '--meta', str(meta_path),
                '--output', str(idx_path),
                '--build-index'
            ], capture_output=True, text=True, timeout=60)

            if result.returncode == 0:
                print(f"Created indexed ftrace: {idx_path}", file=sys.stderr)
            else:
                print(f"Warning: Indexer failed: {result.stderr}", file=sys.stderr)
        except subprocess.TimeoutExpired:
            print("Warning: Indexer timed out", file=sys.stderr)
        except Exception as e:
            print(f"Warning: Could not run indexer: {e}", file=sys.stderr)
    else:
        print("Note: Rust indexer not found, skipping indexed format", file=sys.stderr)

    return True


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <logfile> [output_dir]", file=sys.stderr)
        return 1

    log_path = Path(sys.argv[1])
    if not log_path.exists():
        print(f"Error: {log_path} not found", file=sys.stderr)
        return 1

    if len(sys.argv) >= 3:
        output_dir = Path(sys.argv[2])
    else:
        output_dir = log_path.parent

    try:
        if extract_ftrace(log_path, output_dir):
            return 0
        else:
            # No binary transfer - not an error
            return 2
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
