import re
from typing import List, Tuple


class AnsiCsiStripper:
    """Stateful stream sanitizer that strips ANSI CSI sequences."""

    def __init__(self):
        self._pending = bytearray()

    def sanitize(self, data: bytes) -> bytes:
        if not data and not self._pending:
            return b""

        buf = bytes(self._pending) + data
        self._pending.clear()

        out = bytearray()
        i = 0
        n = len(buf)
        while i < n:
            b = buf[i]
            if b != 0x1B:
                out.append(b)
                i += 1
                continue

            if i + 1 >= n:
                self._pending.extend(buf[i:])
                break

            if buf[i + 1] != 0x5B:  # '['
                out.append(b)
                i += 1
                continue

            j = i + 2
            while j < n:
                if 0x40 <= buf[j] <= 0x7E:
                    i = j + 1
                    break
                j += 1
            else:
                self._pending.extend(buf[i:])
                break

        return bytes(out)

    def flush(self) -> bytes:
        tail = bytes(self._pending)
        self._pending.clear()
        return tail


def normalize_tty_text(text: str) -> str:
    """Strip ANSI escapes and remove CR/LF for robust tty matching."""
    return _ANSI_ESCAPE_TEXT.sub("", text).replace("\r", "").replace("\n", "")


def normalize_tty_bytes(data: bytes) -> Tuple[bytes, List[int]]:
    """
    Normalize tty bytes for regex matching.

    Returns:
    - normalized bytes (ANSI escapes and CR/LF removed)
    - mapping where map[i] is the source raw byte index for normalized[i]
    """
    out = bytearray()
    index_map: List[int] = []
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0x1B:
            consumed = _consume_ansi_escape(data, i)
            if consumed > 0:
                i += consumed
                continue
        if b in (0x0D, 0x0A):
            i += 1
            continue
        out.append(b)
        index_map.append(i)
        i += 1
    return bytes(out), index_map


def to_raw_offset(normalized_index: int, index_map: List[int], default_raw: int) -> int:
    if not index_map:
        return default_raw
    if normalized_index <= 0:
        return default_raw + index_map[0]
    if normalized_index >= len(index_map):
        return default_raw + index_map[-1]
    return default_raw + index_map[normalized_index]


def _consume_ansi_escape(data: bytes, start: int) -> int:
    """
    Consume a CSI ANSI escape sequence from bytes if present.

    Supports: ESC [ ... <final-byte @-~>
    """
    n = len(data)
    if start + 1 >= n or data[start + 1] != 0x5B:  # '['
        return 0
    i = start + 2
    while i < n:
        b = data[i]
        if 0x40 <= b <= 0x7E:
            return (i - start) + 1
        i += 1
    return 0


_ANSI_ESCAPE_TEXT = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
