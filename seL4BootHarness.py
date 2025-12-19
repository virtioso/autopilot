"""
seL4 Boot Harness - Classes for seL4 EFI binary testing on Orin AGX.

SeL4UploadHarness: Boot to stock Linux, upload EFI binary via SCP
SeL4UploadOnlyHarness: Upload EFI binary when already at stock Linux (no reboot)
SeL4RunHarness: Navigate UEFI menus and run EFI binary, capture output
"""

import re
import subprocess
import time
from pexpect import TIMEOUT, EOF

# ANSI escape sequence pattern for stripping color codes
ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

def strip_ansi(text):
    """Strip ANSI escape sequences from text."""
    return ANSI_ESCAPE.sub('', text)

import BootHarness
from BootHarness import BootHarness as BaseBootHarness, debug_print


# Shared constants
TARGET_IP = '192.168.101.112'
TARGET_USER = 'root'
TARGET_PATH = '/boot/efi'


def scp_upload(binary_path, binary_name):
    """Upload binary to target via SCP."""
    debug_print(f'Uploading {binary_name} via SCP')
    subprocess.run([
        'scp', '-o', 'StrictHostKeyChecking=no',
        binary_path,
        f'{TARGET_USER}@{TARGET_IP}:{TARGET_PATH}/{binary_name}'
    ], check=True, capture_output=True, text=True)


def ssh_reboot():
    """Reboot target via SSH."""
    debug_print('Rebooting target via SSH')
    subprocess.run([
        'ssh', '-o', 'StrictHostKeyChecking=no',
        f'{TARGET_USER}@{TARGET_IP}',
        'reboot'
    ])  # Don't check=True, reboot may close connection before exit


class SeL4UploadHarness(BaseBootHarness):
    """Boot to stock Linux, upload EFI binary via SCP, then reboot."""

    def __init__(self, board, tty, filename, binary_path, binary_name):
        super().__init__(board, tty, filename, hyp_tty=None, hyp_filename=None)
        self.binary_path = binary_path
        self.binary_name = binary_name
        self.boot_option = '1'  # Stock Jetson Linux

    def run(self):
        super().run()  # Boot board, extlinux, send '1'

        debug_print('Waiting for shell prompt')
        idx = self.child.expect([
            r'ubuntu@tegra-ubuntu:~\$',
            TIMEOUT,
            EOF
        ], timeout=120)

        if idx != 0:
            raise RuntimeError('Failed to reach shell prompt for upload')

        scp_upload(self.binary_path, self.binary_name)
        ssh_reboot()
        self.stop()


class SeL4UploadOnlyHarness:
    """Upload EFI binary when already at stock Linux (skip boot, just SCP + reboot)."""

    def __init__(self, binary_path, binary_name):
        self.binary_path = binary_path
        self.binary_name = binary_name

    def run(self):
        scp_upload(self.binary_path, self.binary_name)
        ssh_reboot()


class SeL4RunHarness(BaseBootHarness):
    """Navigate UEFI and run EFI binary, capture output."""

    def __init__(self, board, tty, filename, binary_name):
        super().__init__(board, tty, filename, hyp_tty=None, hyp_filename=None)
        self.binary_name = binary_name

    def boot(self):
        pass  # Board already rebooting from upload phase

    def run(self):
        # Wait for UEFI "Enter to continue boot"
        debug_print('Waiting for UEFI prompt')
        idx = self.child.expect([
            r'Enter to continue boot\.',
            r'Press ESCAPE for boot options',
            TIMEOUT,
            EOF
        ], timeout=60)

        if idx == 0 or idx == 1:
            time.sleep(1)
            debug_print('Sending ESC to enter UEFI menu')
            self.child.send('\x1b')  # ESC
        else:
            raise RuntimeError(f'Failed to get UEFI prompt (idx={idx})')

        # Wait for UEFI menu "Select Entry"
        debug_print('Waiting for UEFI Select Entry')
        idx = self.child.expect([
            r'Select Entry',
            TIMEOUT,
            EOF
        ], timeout=30)

        if idx != 0:
            raise RuntimeError('Failed to get UEFI Select Entry menu')

        time.sleep(1)
        debug_print('Navigating to Boot Manager (down, down, enter)')
        self.child.send('\x1b[B')  # Down arrow
        time.sleep(0.3)
        self.child.send('\x1b[B')  # Down arrow
        time.sleep(0.3)
        self.child.send('\r')      # Enter

        # Wait for Boot Manager "Esc=Exit"
        debug_print('Waiting for Boot Manager menu')
        idx = self.child.expect([
            r'Esc=Exit',
            TIMEOUT,
            EOF
        ], timeout=30)

        if idx != 0:
            raise RuntimeError('Failed to get Boot Manager menu')

        time.sleep(1)
        debug_print('Selecting UEFI Shell (up, enter)')
        self.child.send('\x1b[A')  # Up arrow
        time.sleep(0.3)
        self.child.send('\r')      # Enter

        # Wait for Shell prompt, handling startup.nsh delay
        debug_print('Waiting for UEFI Shell prompt')
        while True:
            idx = self.child.expect([
                r'Shell>',
                r'Press ESC in \d+ seconds',  # startup.nsh prompt
                TIMEOUT,
                EOF
            ], timeout=30)

            if idx == 0:  # Got Shell> prompt
                break
            elif idx == 1:  # startup.nsh prompt - send space to skip
                debug_print('Skipping startup.nsh delay')
                self.child.send(' ')
                # Continue loop to wait for Shell>
            else:
                raise RuntimeError('Failed to get Shell prompt')

        debug_print('Switching to fs3:')
        self.child.send('fs3:\r')

        # Wait for FS3 prompt
        idx = self.child.expect([
            r'FS3:\\>',
            TIMEOUT,
            EOF
        ], timeout=10)

        if idx != 0:
            raise RuntimeError('Failed to switch to fs3:')

        debug_print(f'Running {self.binary_name}')
        self.child.send(f'{self.binary_name}\r')

        # Check for immediate error (binary not found)
        idx = self.child.expect([
            r'is not recognized as an internal or external command',
            r'.+',  # Any other output (likely seL4 starting)
            TIMEOUT,
        ], timeout=2)

        if idx == 0:
            raise RuntimeError(f'Binary not found on target: {self.binary_name}')

        # Capture output until quiescent (30 seconds no output) or binary transfer complete
        debug_print('Capturing seL4 output (30s quiescent timeout or binary transfer end)')
        self._capture_until_quiescent(timeout=30)

        self.stop()

    def _capture_until_quiescent(self, timeout=30):
        """Read output until no data for `timeout` seconds or binary transfer ends."""
        BINARY_END_MARKER = '=== BINARY TRANSFER END ==='

        while True:
            idx = self.child.expect([
                r'.+',      # Any output
                TIMEOUT,
                EOF
            ], timeout=timeout)

            if idx == 0:  # Got output
                # Check if the binary transfer end marker is in recent output
                if hasattr(self.child, 'after') and self.child.after:
                    recent = self.child.after
                    if isinstance(recent, bytes):
                        recent = recent.decode('utf-8', errors='replace')
                    if BINARY_END_MARKER in recent:
                        debug_print('Binary transfer complete, capture done')
                        break
                # Continue capturing
            elif idx == 1:  # Timeout - quiescent
                debug_print('Output quiescent, capture complete')
                break
            elif idx == 2:  # EOF
                debug_print('EOF reached')
                break
