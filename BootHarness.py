import BoardControl
import sys, re
import serial
import time
import threading
from pexpect import fdpexpect, EOF, TIMEOUT

class Tee:
    def __init__(self, *streams): self.streams = streams
    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()
    def flush(self):
        for s in self.streams: s.flush()

def debug_print(s):
    print('\x1b[41;1m%s\x1b[30;0m' % s)

def log_port(dev, fname, stop_event, baud=115200):
    ser = serial.Serial(dev, baudrate=baud, timeout=0.1)
    with ser, open(fname, 'ab', buffering=0) as f:
        while not stop_event.is_set():
            data = ser.read(1024)
            if not data:
                continue
            f.write(data)

class BootHarness(object):
    def __init__(self, board, tty, filename, hyp_tty, hyp_filename):
        self.board = board
        self.boot_option = '0'

        self.stop_evt = threading.Event()

        self.t = threading.Thread(
            target=log_port,
            args=(hyp_tty, hyp_filename, self.stop_evt),
            daemon=True
        )
        self.t.start()

        self.ser = serial.Serial(tty, 115200, timeout=0.2)
        self.child = fdpexpect.fdspawn(self.ser, timeout=10, encoding='utf-8', codec_errors='ignore')

        # Lokitus sekä ruudulle että tiedostoon:
        self.logfile = open(filename, 'w', buffering=1, encoding='utf-8')
        self.child.logfile_read = Tee(sys.stdout, self.logfile)   # kaikki laudan tulosteet
        self.child.logfile_send = Tee(sys.stdout, self.logfile)   # myös lähetetyt komennot, jos haluat näkyviin

    def boot(self):
        self.board.boot(False)

    def run(self):
        # Press ESC to enter UEFI menu
        # Retry logic in case board falls through to HTTP boot

        max_boot_retries = 3
        for boot_attempt in range(max_boot_retries):
            debug_print(f'Booting board in normal mode (attempt {boot_attempt + 1}/{max_boot_retries})')
            self.boot()

            debug_print('Waiting for first UEFI prompt')
            idx = self.child.expect([
                r'ESC   to enter Setup.',       # 0: Normal UEFI prompt
                r'Start HTTP Boot',             # 1: Fell through to network boot
                TIMEOUT,                        # 2: Timeout
                EOF                             # 3: EOF
            ], timeout=60)

            debug_print('---done---')

            if idx == 0:
                # Successfully got UEFI prompt
                time.sleep(2)
                debug_print('Sending ESC to board')
                self.child.send('\x1b')
                break
            elif idx == 1:
                # Fell through to HTTP boot, need to retry
                debug_print('Detected HTTP Boot fallthrough, rebooting...')
                time.sleep(2)
                if boot_attempt < max_boot_retries - 1:
                    continue  # Retry the boot
                else:
                    debug_print('Max boot retries reached, giving up')
                    exit(1)
            else:
                # Timeout or EOF
                debug_print(f'Unexpected error (idx={idx})')
                exit(1)

        # Select Boot Manager

        debug_print('Waiting for UEFI menu')
        idx = self.child.expect([
            r'Select Entry',
            TIMEOUT,
            EOF
        ], timeout=60)

        debug_print('---done---')
        time.sleep(1)

        if idx == 0:
            # Two arrow downs and enter to select Boot Manager
            self.child.send('\x1b[B')
            time.sleep(0.3)
            self.child.send('\x1b[B')
            time.sleep(0.3)
            self.child.send('\r')
        else:
            debug_print('Unexpected error')
            exit(1)

        # Select eMMC boot

        debug_print('Waiting for Boot Manager menu')
        idx = self.child.expect([
            r'Select Entry',
            TIMEOUT,
            EOF
        ], timeout=60)

        debug_print('---done---')
        time.sleep(2)

        if idx == 0:
            # Three arrow downs and enter to select eMMC boot
            self.child.send('\x1b[B')
            time.sleep(0.3)
            self.child.send('\x1b[B')
            time.sleep(0.3)
            self.child.send('\x1b[B')
            time.sleep(0.3)
            self.child.send('\r')
        else:
            debug_print('Unexpected error')
            exit(1)

        # Press 1 to select Linux 6.17

        debug_print('Waiting for extlinux menu')
        idx = self.child.expect([
            r'Press any other key to boot default',
            TIMEOUT,
            EOF
        ], timeout=60)

        debug_print('---done---')
        time.sleep(2)

        if idx == 0:
            # Select Linux 6.17
            self.child.send(self.boot_option)
        else:
            debug_print('Unexpected error')
            exit(1)

    def stop(self):
        self.stop_evt.set()
        self.t.join(timeout=1.0)


class PanicBootHarness(BootHarness):
    def __init__(self, board, tty, filename, hyp_tty, hyp_filename):
        super().__init__(board, tty, filename, hyp_tty, hyp_filename)
        self.boot_option = '2'
        self.fault_type = None  # Track what type of fault we detected

    def boot(self):
        pass

    def run(self):
        super().run()

        # Now we wait until kernel panics OR SMMU faults occur

#        logfile = open(self.filename, 'w', buffering=1, encoding='utf-8')
#        self.child.logfile_read = Tee(sys.stdout, logfile)   # kernel log buffer

        debug_print('Waiting for kernel panic or SMMU faults')

        # First, try to detect either panic or SMMU faults
        smmu_fault_count = 0
        timeout_total = 60  # Total timeout for detection

        while timeout_total > 0:
            idx = self.child.expect([
                r'Kernel panic',                           # 0: Kernel panic
                r'Unexpected global fault',                # 1: SMMU global fault
                r'callbacks suppressed',                   # 2: SMMU callback suppression
                TIMEOUT,                                   # 3: Timeout
                EOF                                        # 4: EOF
            ], timeout=5)

            if idx == 0:
                # Kernel panic detected
                debug_print('Detected kernel panic')
                self.fault_type = 'panic'
                time.sleep(1)
                break
            elif idx == 1 or idx == 2:
                # SMMU fault detected
                smmu_fault_count += 1
                timeout_total -= 1
                if smmu_fault_count >= 5:
                    debug_print(f'Detected {smmu_fault_count} SMMU faults, stopping collection')
                    self.fault_type = 'smmu_fault'
                    # Wait a bit more to collect additional fault info
                    time.sleep(3)
                    break
            elif idx == 3:
                # Timeout - check if we have any SMMU faults
                timeout_total -= 5
                if smmu_fault_count > 0:
                    debug_print(f'Timeout reached with {smmu_fault_count} SMMU faults detected')
                    self.fault_type = 'smmu_fault'
                    break
                elif timeout_total <= 0:
                    debug_print('No panic or SMMU faults detected within timeout')
                    self.fault_type = 'timeout'
                    break
            elif idx == 4:
                debug_print('EOF reached')
                if smmu_fault_count > 0:
                    self.fault_type = 'smmu_fault'
                else:
                    self.fault_type = 'eof'
                break

        if self.fault_type == 'panic':
            debug_print('Collecting remaining panic output...')
        elif self.fault_type == 'smmu_fault':
            debug_print(f'Collected {smmu_fault_count} SMMU fault instances')
        else:
            debug_print(f'Unexpected condition: {self.fault_type}')

        # let us timeout until no more log from kernel
        idx = self.child.expect([
            r'you will not find this string',
            TIMEOUT,
            EOF
        ], timeout=3)

        self.stop()

class UpdateBootHarness(BootHarness):
    def __init__(self, board, tty, filename, hyp_tty, hyp_filename):
        super().__init__(board, tty, filename, hyp_tty, hyp_filename)

    def run(self):
        super().run()

        # Now we wait until kernel panics

#        logfile = open(self.filename, 'w', buffering=1, encoding='utf-8')
#        self.child.logfile_read = Tee(sys.stdout, logfile)   # kernel log buffer

        debug_print('Waiting for rebooting message')
        idx = self.child.expect([
            r'Rebooting system',
            TIMEOUT,
            EOF
        ], timeout=180)

        time.sleep(1)
        if idx == 0:
            debug_print('Cool, system rebooted itself')
        else:
            debug_print('Unexpected error')
            exit(1)

        self.stop()
