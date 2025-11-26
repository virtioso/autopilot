import subprocess
import usbrelay_py
import time

class BoardControlLocal(object):
    def __init__(self):
        count = usbrelay_py.board_count()

        boards = usbrelay_py.board_details()
        self.board = boards[0]

    def set_recovery(self, value):
        usbrelay_py.board_control(self.board[0], 1, value)

    def set_reset(self, value):
        usbrelay_py.board_control(self.board[0], 2, value)

    def boot(self, recovery):
        self.set_recovery(recovery)
        time.sleep(0.1)
        self.set_reset(True)
        time.sleep(0.1)
        self.set_reset(False)
        time.sleep(0.5)
        self.set_recovery(False)

class BoardControlRemote(object):
    def __init__(self):
        pass

    def boot(self, recovery):
        mode = "normal"
        if recovery:
            mode = "recovery"
        result = subprocess.run(
            ["ssh", "192.168.101.110", "./boot.sh", mode],
            capture_output=True,
            text=True,
            check=False,
        )
