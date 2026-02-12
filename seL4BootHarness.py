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
from config import get_target_ip, get_target_user


# Shared constants
TARGET_IP = get_target_ip()
TARGET_USER = get_target_user()
TARGET_PATH = '/boot/efi'


def cleanup_old_binaries():
    """Remove ordinary files from the EFI partition root."""
    debug_print('Cleaning up /boot/efi (ordinary files only)')
    try:
        subprocess.run([
            'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10',
            f'{TARGET_USER}@{TARGET_IP}',
            'find /boot/efi -maxdepth 1 -type f -print -delete'
        ], check=True, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        debug_print('Warning: cleanup timed out, continuing anyway')
    except subprocess.CalledProcessError as e:
        debug_print(f'Warning: cleanup failed: {e}, continuing anyway')


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

        cleanup_old_binaries()
        scp_upload(self.binary_path, self.binary_name)
        ssh_reboot()
        self.stop()


class SeL4UploadOnlyHarness:
    """Upload EFI binary when already at stock Linux (skip boot, just SCP + reboot)."""

    def __init__(self, binary_path, binary_name):
        self.binary_path = binary_path
        self.binary_name = binary_name

    def run(self):
        cleanup_old_binaries()
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
        self._navigate_and_start()

        # Capture output until quiescent (30 seconds no output) or binary transfer complete
        debug_print('Capturing seL4 output (30s quiescent timeout or binary transfer end)')
        self._capture_until_quiescent(timeout=30)

        self.stop()

    def _navigate_and_start(self):
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


class SeL4RunInteractiveHarness(SeL4RunHarness):
    """Navigate UEFI and run EFI binary, then return immediately for interactive use."""

    def run(self):
        self._navigate_and_start()
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


class VMMinimalRunHarness(BaseBootHarness):
    """
    Run a vm_minimal capdl-loader binary with dual UART capture.

    Captures ttyACM0 (seL4/capdl-loader) and ttyACM1 (VM console).
    Waits for sel4_boot_timeout for capdl-loader to finish booting,
    then waits for vm_quiescence_timeout of inactivity on ttyACM1.
    """

    def __init__(self, board, tty, filename, vm_tty, vm_filename, binary_name,
                 sel4_boot_timeout=60, vm_quiescence_timeout=5):
        super().__init__(board, tty, filename, hyp_tty=None, hyp_filename=None)
        self.binary_name = binary_name
        self.vm_tty = vm_tty
        self.vm_filename = vm_filename
        self.sel4_boot_timeout = sel4_boot_timeout
        self.vm_quiescence_timeout = vm_quiescence_timeout
        self.vm_stop_event = None
        self.vm_log_thread = None

    def boot(self):
        pass  # Board already rebooting from upload phase

    def run(self):
        import os
        import threading

        # Start VM console capture (ttyACM1) in background
        self.vm_stop_event = threading.Event()
        self.vm_log_thread = threading.Thread(
            target=BootHarness.log_port,
            args=(self.vm_tty, self.vm_filename, self.vm_stop_event),
            daemon=True
        )
        self.vm_log_thread.start()

        # Navigate UEFI and start binary (same as SeL4RunHarness)
        self._navigate_uefi_and_start()

        # Record file size AFTER binary starts - ignore any prior buffered data
        try:
            self.vm_baseline_size = os.path.getsize(self.vm_filename)
        except FileNotFoundError:
            self.vm_baseline_size = 0
        debug_print(f'VM console baseline size: {self.vm_baseline_size} bytes (ignored)')

        # Now wait for VM console (ttyACM1) to be quiescent
        debug_print(f'Waiting for VM console quiescence ({self.vm_quiescence_timeout}s)...')
        self._wait_for_vm_quiescence()

        # Stop VM console capture
        self.vm_stop_event.set()
        self.vm_log_thread.join(timeout=2.0)

        self.stop()

    def _navigate_uefi_and_start(self):
        """Navigate UEFI menus and start the binary."""
        import time

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

    def _wait_for_vm_quiescence(self):
        """Wait for BINARY TRANSFER END marker on ttyACM0, while capturing both consoles."""
        import os
        import re
        import time

        # Pattern to detect Linux kernel boot messages (e.g., "[    0.000000] Booting Linux")
        LINUX_BOOT_PATTERN = re.compile(rb'\[\s*\d+\.\d+\]')
        # Marker indicating binary transfer is complete (appears on ttyACM0)
        BINARY_END_MARKER = '=== BINARY TRANSFER END ==='

        # Phase 1: Wait for Linux kernel to start booting on VM console
        # This phase ends when we see Linux kernel messages OR timeout expires
        # Only look at NEW data after baseline (ignore buffered data from before binary started)
        debug_print(f'Phase 1: Waiting up to {self.sel4_boot_timeout}s for Linux kernel boot on VM console...')
        boot_start_time = time.time()
        vm_started = False
        last_checked_size = self.vm_baseline_size  # Start from baseline, not 0

        while time.time() - boot_start_time < self.sel4_boot_timeout:
            # Continue capturing ttyACM0 output
            try:
                self.child.expect([r'.+', TIMEOUT], timeout=1)
            except:
                pass

            # Check if VM console has NEW Linux kernel boot messages (after baseline)
            try:
                current_size = os.path.getsize(self.vm_filename)
                if current_size > last_checked_size:
                    # Read new content and check for Linux kernel messages
                    with open(self.vm_filename, 'rb') as f:
                        f.seek(last_checked_size)
                        new_content = f.read()
                    last_checked_size = current_size

                    if LINUX_BOOT_PATTERN.search(new_content):
                        debug_print('Linux kernel boot detected on VM console')
                        vm_started = True
                        break
            except FileNotFoundError:
                pass

        if not vm_started:
            debug_print(f'sel4_boot_timeout ({self.sel4_boot_timeout}s) expired, no Linux boot detected')

        # Phase 2: Wait for BINARY TRANSFER END marker on ttyACM0 (no timeout)
        debug_print('Phase 2: Waiting for BINARY TRANSFER END marker on ttyACM0...')

        while True:
            # Capture ttyACM0 output and check for end marker
            try:
                idx = self.child.expect([r'.+', TIMEOUT], timeout=1)
                if idx == 0:  # Got output
                    # Check if the binary transfer end marker is in recent output
                    if hasattr(self.child, 'after') and self.child.after:
                        recent = self.child.after
                        if isinstance(recent, bytes):
                            recent = recent.decode('utf-8', errors='replace')
                        if BINARY_END_MARKER in recent:
                            debug_print('BINARY TRANSFER END marker detected, capture complete')
                            break
            except:
                pass
