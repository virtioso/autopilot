import BoardControl
import sys, re
import serial
import time
import threading
import subprocess
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
    def __init__(self, board, tty, filename, hyp_tty=None, hyp_filename=None):
        self.board = board
        self.boot_option = '0'

        # Hyp logging is optional - if hyp_tty is provided, we manage it
        # If not, caller is expected to manage hyp logging externally
        self.stop_evt = None
        self.t = None
        if hyp_tty and hyp_filename:
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
        debug_print('Booting board in normal mode')
        self.boot()

        debug_print('Waiting for extlinux menu')
        idx = self.child.expect([
            r'Press any other key to boot default',
            TIMEOUT,
            EOF
        ], timeout=60)

        debug_print('---done---')

        if idx == 0:
            time.sleep(1)
            self.child.send(self.boot_option)
        else:
            debug_print(f'Unexpected error (idx={idx})')
            exit(1)

    def stop(self):
        if self.stop_evt and self.t:
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
        bash_prompt_seen = False
        timeout_total = 300  # Total timeout for detection

        while timeout_total > 0:
            idx = self.child.expect([
                r'Kernel panic',                           # 0: Kernel panic
                r'Unexpected global fault',                # 1: SMMU global fault
                r'callbacks suppressed',                   # 2: SMMU callback suppression
                r'Press \[ENTER\] to start bash',          # 3: Emergency bash prompt
                r'nvgpu.*HS ucode boot failed',            # 4: nvgpu ACR failure
                TIMEOUT,                                   # 5: Timeout
                EOF                                        # 6: EOF
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
                # Emergency bash prompt - send enter but DON'T break
                # The prompt might be for a different TTY, keep listening
                debug_print('Detected emergency bash prompt, sending ENTER')
                self.child.send('\r')
                bash_prompt_seen = True
                # Continue loop - wait for board to respond or show different output
                continue
            elif idx == 4:
                # nvgpu ACR boot failure
                debug_print('Detected nvgpu HS ucode boot failure')
                self.fault_type = 'nvgpu_acr_fail'
                time.sleep(1)
                break
            elif idx == 5:
                # Timeout - check what state we're in
                timeout_total -= 5
                if bash_prompt_seen:
                    # We sent enter and got no more output - board responded
                    debug_print('Board responded to bash prompt')
                    self.fault_type = 'bash_prompt'
                    break
                elif smmu_fault_count > 0:
                    debug_print(f'Timeout reached with {smmu_fault_count} SMMU faults detected')
                    self.fault_type = 'smmu_fault'
                    break
                elif timeout_total <= 0:
                    debug_print('No panic or SMMU faults detected within timeout')
                    self.fault_type = 'timeout'
                    break
            elif idx == 6:
                debug_print('EOF reached')
                if smmu_fault_count > 0:
                    self.fault_type = 'smmu_fault'
                elif bash_prompt_seen:
                    self.fault_type = 'bash_prompt'
                else:
                    self.fault_type = 'eof'
                break

        if self.fault_type == 'panic':
            debug_print('Collecting remaining panic output...')
        elif self.fault_type == 'smmu_fault':
            debug_print(f'Collected {smmu_fault_count} SMMU fault instances')
        elif self.fault_type == 'bash_prompt':
            debug_print('Boot failed - dropped to emergency bash shell')
        elif self.fault_type == 'nvgpu_acr_fail':
            debug_print('GPU ACR initialization failed')
        else:
            debug_print(f'Unexpected condition: {self.fault_type}')

        # let us timeout until no more log from kernel
        idx = self.child.expect([
            r'you will not find this string',
            TIMEOUT,
            EOF
        ], timeout=3)

        self.stop()

class ReadyBootHarness(BootHarness):
    """Boot to vanilla Jetson Linux and wait for SSH ready state."""
    TARGET_PROMPT = r'ubuntu@tegra-ubuntu:~\$'

    def __init__(self, board, tty, filename, hyp_tty=None, hyp_filename=None):
        super().__init__(board, tty, filename, hyp_tty, hyp_filename)
        self.boot_option = '1'  # Vanilla Jetson Linux

    def run(self):
        super().run()  # Boot board, wait extlinux, send '1'

        debug_print('Waiting for shell prompt (SSH ready)')
        idx = self.child.expect([
            self.TARGET_PROMPT,
            TIMEOUT,
            EOF
        ], timeout=120)

        if idx != 0:
            debug_print('Failed to reach ready state')
            raise RuntimeError('Board failed to reach ready state')

        debug_print('Board is ready (SSH accessible)')
        self.stop()


class UpdateBootHarness(BootHarness):
    """Upload kernel via SCP and reboot. Assumes board is already in ready state."""
    TARGET_IP = '192.168.101.112'
    TARGET_USER = 'root'

    def __init__(self, board, tty, filename, hyp_tty, hyp_filename, kernel_image_path, kernel_version):
        super().__init__(board, tty, filename, hyp_tty, hyp_filename)
        self.kernel_image_path = kernel_image_path
        self.kernel_version = kernel_version

    def boot(self):
        pass  # Board already in ready state

    def run(self):
        # DON'T call super().run() - board is already booted

        target_path = f'/boot/Image-{self.kernel_version}'
        debug_print(f'Uploading kernel via SCP to {target_path}')
        subprocess.run([
            'scp', '-o', 'StrictHostKeyChecking=no',
            self.kernel_image_path,
            f'{self.TARGET_USER}@{self.TARGET_IP}:{target_path}'
        ], check=True)

        debug_print('Rebooting target via SSH')
        subprocess.run([
            'ssh', '-o', 'StrictHostKeyChecking=no',
            f'{self.TARGET_USER}@{self.TARGET_IP}',
            'reboot'
        ])  # Don't check=True, reboot may close connection before exit

        self.stop()
