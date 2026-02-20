import subprocess
import usbrelay_py
import time
from config import get_boot_control_host

class BoardControlLocal(object):
    def __init__(self):
        count = usbrelay_py.board_count()

        boards = usbrelay_py.board_details()
        self.board = boards[0]

    def set_recovery(self, value):
        usbrelay_py.board_control(self.board[0], 1, value)

    def set_reset(self, value):
        usbrelay_py.board_control(self.board[0], 2, value)

    def assert_reset_line(self):
        self.set_reset(True)

    def deassert_reset_line(self):
        self.set_reset(False)

    def boot(self, recovery):
        self.set_recovery(recovery)
        time.sleep(0.1)
        self.assert_reset_line()
        time.sleep(0.1)
        self.deassert_reset_line()
        time.sleep(0.5)
        self.set_recovery(False)

class BoardControlRemote(object):
    def __init__(self):
        self.host = get_boot_control_host()

    def boot(self, recovery):
        mode = "normal"
        if recovery:
            mode = "recovery"
        result = subprocess.run(
            ["ssh", self.host, "./boot.sh", mode],
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_reset_line(self):
        raise NotImplementedError("remote boot controller does not expose reset line assert")

    def deassert_reset_line(self):
        raise NotImplementedError("remote boot controller does not expose reset line deassert")
