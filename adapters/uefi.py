"""
UEFI boot oracles: UEFIShellRunOracle and ExtlinuxBootOracle.

UEFIShellRunOracle
------------------
Navigate from UEFI firmware to the UEFI Shell, then run an EFI binary.

Full interaction sequence (matches _step_uefi_shell_run in old chain_runtime.py):
  1. Wait for UEFI firmware interrupt prompt
  2. Send ESC × 3 to enter UEFI menu
  3. Wait for menu ("Select Entry" or "Please select boot device")
  4. Navigate to UEFI Shell
  5. Wait for Shell> prompt (with startup.nsh countdown handling)
  6. Switch to the target filesystem (e.g. "fs2:")
  7. Wait for filesystem prompt
  8. Run the binary
  9. Optionally wait for a success pattern

ExtlinuxBootOracle
------------------
Wait for an extlinux/L4TLauncher boot menu, then select an entry by number.
This is used after a prior step has launched a bootloader EFI binary that
displays the menu (e.g. BOOTAA64.EFI or L4TLauncher).
"""

from __future__ import annotations

import asyncio
import re
import structlog

from engine.oracle import Error, Matched, StreamContext, Verdict

log = structlog.get_logger()

# UEFI firmware interrupt prompts — any of these triggers the ESC sequence.
_UEFI_INTERRUPT_PATTERNS: list[bytes] = [
    rb"Enter to continue boot\.",
    rb"Press ESCAPE for boot options",
    rb"Press ESC to enter Setup",
    rb"ESC\s+to enter Setup",
    rb"F11\s+to enter Boot Manager Menu",
]

_MAX_BUF = 65536


class UEFIShellRunOracle:
    """
    Navigate UEFI firmware menus to the UEFI Shell, then run an EFI binary.

    Parameters
    ----------
    stream: str
        Name of the registered BiStream (e.g. "tty0").
    binary: str
        Binary name relative to the target filesystem root
        (e.g. "efiboot\\mytest.efi" or "EFI\\BOOT\\BOOTAA64.EFI").
    fs: str
        EFI filesystem identifier to switch to (default "fs2").
    success_pattern: str | None
        If set, wait for this regex after running the binary.
        If None, return Matched("ok") immediately after sending the command.
    prompt_timeout_s: float
        Seconds to wait for the UEFI interrupt prompt.
    select_timeout_s: float
        Seconds to wait for the UEFI selection menu.
    boot_manager_timeout_s: float
        Seconds to wait for the Boot Manager sub-menu.
    shell_timeout_s: float
        Seconds to wait for the Shell> prompt.
    fs_timeout_s: float
        Seconds to wait for the filesystem prompt after switching.
    """

    def __init__(
        self,
        stream: str,
        binary: str,
        fs: str = "fs2",
        success_pattern: str | None = None,
        prompt_timeout_s: float = 60.0,
        select_timeout_s: float = 30.0,
        boot_manager_timeout_s: float = 30.0,
        shell_timeout_s: float = 30.0,
        fs_timeout_s: float = 10.0,
    ) -> None:
        self._stream = stream
        self._binary = binary.encode() if isinstance(binary, str) else binary
        fs_str = fs if fs.endswith(":") else f"{fs}:"
        self._fs = fs_str.upper().encode()
        self._success = re.compile(success_pattern.encode()) if success_pattern else None
        self._prompt_timeout = prompt_timeout_s
        self._select_timeout = select_timeout_s
        self._boot_manager_timeout = boot_manager_timeout_s
        self._shell_timeout = shell_timeout_s
        self._fs_timeout = fs_timeout_s

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        bio = ctx.streams[self._stream]

        # 1. Wait for UEFI interrupt prompt.
        idx = await _wait_any(bio, _UEFI_INTERRUPT_PATTERNS, self._prompt_timeout)
        if idx < 0:
            log.warning("uefi.no_interrupt_prompt", stream=self._stream)
            return Error("uefi_no_interrupt_prompt"), ctx

        log.debug("uefi.interrupt_prompt_seen", idx=idx, stream=self._stream)

        # 2. Send ESC × 3 with short pauses.
        for _ in range(3):
            await bio.write(b"\x1b")
            await asyncio.sleep(0.15)

        # 3. Wait for selection menu.
        menu_patterns = [rb"Select Entry", rb"Please select boot device"]
        idx = await _wait_any(bio, menu_patterns, self._select_timeout)
        if idx < 0:
            # Fallback: try F11 (Boot Manager hotkey on some UEFI builds).
            await bio.write(b"\x1b[23~")
            idx = await _wait_any(bio, menu_patterns, self._select_timeout)
            if idx < 0:
                log.warning("uefi.no_menu", stream=self._stream)
                return Error("uefi_no_menu"), ctx

        log.debug("uefi.menu_seen", menu_idx=idx, stream=self._stream)
        await asyncio.sleep(0.2)

        # 4. Navigate to UEFI Shell.
        if idx == 0:
            # "Select Entry" menu → Boot Manager → UEFI Shell (last entry).
            await bio.write(b"\x1b[B")   # Down
            await asyncio.sleep(0.3)
            await bio.write(b"\x1b[B")   # Down
            await asyncio.sleep(0.3)
            await bio.write(b"\r")        # Enter → Boot Manager

            bm_idx = await _wait_any(
                bio, [rb"Esc=Exit", rb"ESC to exit"], self._boot_manager_timeout
            )
            if bm_idx < 0:
                log.warning("uefi.no_boot_manager", stream=self._stream)
                return Error("uefi_no_boot_manager"), ctx

            await asyncio.sleep(1.0)
            await bio.write(b"\x1b[A")   # Up → UEFI Shell entry
            await asyncio.sleep(0.3)
            await bio.write(b"\r")        # Enter

        else:
            # "Please select boot device" menu → UEFI Shell is ~6 entries down.
            for _ in range(6):
                await bio.write(b"\x1b[B")
                await asyncio.sleep(0.2)
            await bio.write(b"\r")

        # 5. Wait for Shell> (handle startup.nsh countdown by sending space).
        while True:
            idx = await _wait_any(
                bio, [rb"Shell>", rb"Press ESC in \d+ seconds"], self._shell_timeout
            )
            if idx == 0:
                break  # Shell prompt received
            if idx == 1:
                await bio.write(b" ")  # dismiss startup.nsh countdown
                continue
            log.warning("uefi.no_shell_prompt", stream=self._stream)
            return Error("uefi_no_shell_prompt"), ctx

        log.debug("uefi.shell_ready", stream=self._stream)

        # 6. Switch to target filesystem.
        await bio.write(self._fs + b"\r")
        fs_prompt = re.compile(re.escape(self._fs) + rb"\\>", re.IGNORECASE)
        ok = await _wait_pattern(bio, fs_prompt, self._fs_timeout)
        if not ok:
            log.warning("uefi.fs_switch_failed", fs=self._fs, stream=self._stream)
            return Error("uefi_fs_switch_failed"), ctx

        # 7. Run binary.
        await bio.write(self._binary + b"\r")
        log.info("uefi.binary_launched", binary=self._binary, stream=self._stream)

        # 8. Optionally wait for success pattern.
        if self._success is not None:
            ok = await _wait_pattern(bio, self._success, timeout)
            if not ok:
                return Error("uefi_binary_no_success"), ctx

        return Matched("ok"), ctx


class ExtlinuxBootOracle:
    """
    Wait for an extlinux/L4TLauncher boot menu and select an entry.

    This oracle is used after a bootloader binary has been launched (via
    UEFIShellRunOracle or manually) and the board displays a numbered menu.

    Parameters
    ----------
    stream: str
        Name of the registered BiStream.
    entry: int
        Menu entry number to select (1-based, as displayed on screen).
    menu_pattern: str
        Regex that matches the first numbered entry line of the menu.
        Default matches L4TLauncher's menu format.
    interrupt_pattern: str | None
        If set, wait for this pattern and send interrupt_key before waiting
        for the menu. Useful when U-Boot/UEFI shows a countdown first.
    interrupt_key: bytes
        Key to send when interrupt_pattern is matched (default: space).
    interrupt_timeout_s: float
        Seconds to wait for interrupt_pattern (if set).
    menu_timeout_s: float
        Seconds to wait for the boot menu.
    """

    def __init__(
        self,
        stream: str,
        entry: int = 1,
        menu_pattern: str = r"^\s*1\.\s+",
        interrupt_pattern: str | None = None,
        interrupt_key: bytes = b" ",
        interrupt_timeout_s: float = 30.0,
        menu_timeout_s: float = 60.0,
    ) -> None:
        self._stream = stream
        self._entry = entry
        self._menu_re = re.compile(menu_pattern.encode(), re.MULTILINE)
        self._interrupt_re = (
            re.compile(interrupt_pattern.encode()) if interrupt_pattern else None
        )
        self._interrupt_key = interrupt_key
        self._interrupt_timeout = interrupt_timeout_s
        self._menu_timeout = menu_timeout_s

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        bio = ctx.streams[self._stream]

        # Optional interrupt phase.
        if self._interrupt_re is not None:
            ok = await _wait_pattern(bio, self._interrupt_re, self._interrupt_timeout)
            if not ok:
                log.warning("extlinux.no_interrupt_prompt", stream=self._stream)
                return Error("extlinux_no_interrupt_prompt"), ctx
            await bio.write(self._interrupt_key)

        # Wait for numbered menu.
        ok = await _wait_pattern(bio, self._menu_re, self._menu_timeout)
        if not ok:
            log.warning("extlinux.no_menu", stream=self._stream)
            return Error("extlinux_no_menu"), ctx

        # Send entry number followed by newline.
        log.info("extlinux.selecting_entry", entry=self._entry, stream=self._stream)
        await bio.write(f"{self._entry}\n".encode())
        return Matched("ok"), ctx


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _wait_any(bio: object, patterns: list[bytes], timeout: float) -> int:
    """
    Read from bio until one of the byte patterns matches; return its index.
    Returns -1 on timeout or EOF.
    Patterns are compiled as re.MULTILINE regexes.
    """
    compiled = [re.compile(p, re.MULTILINE) for p in patterns]
    buf = bytearray()
    deadline = asyncio.get_event_loop().time() + timeout

    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return -1
        try:
            chunk = await asyncio.wait_for(bio.read(4096), timeout=min(remaining, 1.0))
        except asyncio.TimeoutError:
            continue
        if not chunk:
            return -1  # EOF
        buf.extend(chunk)
        if len(buf) > _MAX_BUF:
            del buf[: len(buf) - _MAX_BUF]
        for i, pattern in enumerate(compiled):
            if pattern.search(buf):
                return i


async def _wait_pattern(bio: object, pattern: re.Pattern, timeout: float) -> bool:
    """Read from bio until pattern matches; return True on match, False on timeout/EOF."""
    buf = bytearray()
    deadline = asyncio.get_event_loop().time() + timeout

    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return False
        try:
            chunk = await asyncio.wait_for(bio.read(4096), timeout=min(remaining, 1.0))
        except asyncio.TimeoutError:
            continue
        if not chunk:
            return False
        buf.extend(chunk)
        if len(buf) > _MAX_BUF:
            del buf[: len(buf) - _MAX_BUF]
        if pattern.search(buf):
            return True
