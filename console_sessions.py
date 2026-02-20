import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

import serial

from tty_match import normalize_tty_text

DEFAULT_BAUD = 115200
LOG_BUFFER_LIMIT = 65536
CODE_DIR = Path(__file__).resolve().parent
PROFILES_DIR = CODE_DIR / "profiles"


@dataclass
class ConsoleProfile:
    name: str
    baud: int
    login_prompt: Optional[str]
    username: Optional[str]
    password_prompt: Optional[str]
    password: Optional[str]
    post_login_prompt: Optional[str]
    shell_prompt: Optional[str]
    ready_prompt: Optional[str]


def load_profile(profiles_dir: Path, profile_name: str) -> ConsoleProfile:
    profile_path = profiles_dir / f"{profile_name}.json"
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile not found: {profile_path}")

    data = json.loads(profile_path.read_text())
    login = data.get("login", {})
    shell = data.get("shell", {})
    ready = data.get("ready", {})

    return ConsoleProfile(
        name=data.get("name", profile_name),
        baud=int(data.get("baud", DEFAULT_BAUD)),
        login_prompt=login.get("prompt"),
        username=login.get("username"),
        password_prompt=login.get("password_prompt"),
        password=login.get("password"),
        post_login_prompt=login.get("post_login_prompt"),
        shell_prompt=shell.get("prompt"),
        ready_prompt=ready.get("prompt"),
    )


class ConsoleSession:
    def __init__(self, session_id: str, name: str, port: str, profile: ConsoleProfile,
                 log_dir: Path, runtime_dir: Path):
        self.session_id = session_id
        self.name = name
        self.port = port
        self.profile = profile
        self.log_dir = log_dir
        self.runtime_dir = runtime_dir

        self.log_path = log_dir / f"{name}.log"
        self.events_path = log_dir / f"{name}.jsonl"

        self.cmd_dir = runtime_dir / "cmd"
        self.resp_dir = runtime_dir / "resp"
        self.cmd_dir.mkdir(parents=True, exist_ok=True)
        self.resp_dir.mkdir(parents=True, exist_ok=True)

        self._ser = None
        self._reader_thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def open(self) -> None:
        self._ser = serial.Serial(self.port, baudrate=self.profile.baud, timeout=0.1)
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def close(self) -> None:
        self._stop_event.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass

    def _reader_loop(self) -> None:
        with open(self.log_path, "ab", buffering=0) as log_f, \
             open(self.events_path, "a", encoding="utf-8", buffering=1) as evt_f:
            while not self._stop_event.is_set():
                try:
                    data = self._ser.read(1024)
                except Exception:
                    time.sleep(0.1)
                    continue

                if not data:
                    continue

                log_f.write(data)
                text = data.decode("utf-8", errors="replace")
                evt_f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "dir": "rx",
                    "data": text
                }) + "\n")

    def _write_event(self, text: str) -> None:
        with open(self.events_path, "a", encoding="utf-8", buffering=1) as evt_f:
            evt_f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "dir": "tx",
                "data": text
            }) + "\n")

    def _send(self, text: str) -> None:
        if not self._ser:
            raise RuntimeError("Serial port not open")
        self._ser.write(text.encode("utf-8", errors="replace"))
        self._write_event(text)

    def get_offset(self) -> int:
        try:
            return self.log_path.stat().st_size
        except FileNotFoundError:
            return 0

    def read_output(self, offset: int, max_bytes: int) -> tuple[str, int]:
        if not self.log_path.exists():
            return "", offset
        with open(self.log_path, "rb") as f:
            f.seek(offset)
            data = f.read(max_bytes)
        new_offset = offset + len(data)
        return data.decode("utf-8", errors="replace"), new_offset

    def wait_for_prompt(self, prompt_regex: str, start_offset: int, timeout_s: int) -> tuple[str, int, bool]:
        compiled = re.compile(prompt_regex, re.MULTILINE)
        buffer = ""
        offset = start_offset
        deadline = time.time() + timeout_s

        while time.time() < deadline:
            chunk, offset = self.read_output(offset, 4096)
            if chunk:
                buffer += chunk
                if len(buffer) > LOG_BUFFER_LIMIT:
                    buffer = buffer[-LOG_BUFFER_LIMIT:]
                if compiled.search(normalize_tty_text(buffer)):
                    return buffer, offset, True
            else:
                time.sleep(0.1)

        return buffer, offset, False

    def perform_login(self, timeout_s: int = 60) -> bool:
        if not self.profile.login_prompt:
            return True

        start_offset = self.get_offset()
        _, offset, matched = self.wait_for_prompt(self.profile.login_prompt, start_offset, timeout_s)
        if not matched:
            return False

        if self.profile.username:
            self._send(self.profile.username + "\n")
        if self.profile.password_prompt:
            _, offset, matched = self.wait_for_prompt(self.profile.password_prompt, offset, timeout_s)
            if not matched:
                return False
            self._send((self.profile.password or "") + "\n")

        if self.profile.post_login_prompt:
            _, _, matched = self.wait_for_prompt(self.profile.post_login_prompt, offset, timeout_s)
            return matched

        return True

    def send_command(self, command: str, append_newline: bool, wait_for_prompt: bool,
                     prompt_regex: Optional[str], timeout_s: int) -> dict:
        with self._lock:
            start_offset = self.get_offset()
            text = command + ("\n" if append_newline else "")
            self._send(text)

            if wait_for_prompt:
                prompt = prompt_regex or self.profile.shell_prompt
                if not prompt:
                    return {
                        "output": "",
                        "new_offset": start_offset,
                        "matched": False,
                        "error": "No prompt regex available"
                    }
                output, new_offset, matched = self.wait_for_prompt(prompt, start_offset, timeout_s)
                return {
                    "output": output,
                    "new_offset": new_offset,
                    "matched": matched
                }

            return {
                "output": "",
                "new_offset": self.get_offset(),
                "matched": True
            }


class ConsoleManager:
    def __init__(self, autopilot_dir: Path):
        self.autopilot_dir = autopilot_dir
        self.profiles_dir = PROFILES_DIR
        self.runtime_dir = autopilot_dir / "runtime" / "console"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.sessions = {}

    def create_session(self, request_id: str, name: str, port: str, profile_name: str,
                       log_dir: Path) -> ConsoleSession:
        session_id = f"{request_id}-{name}"
        runtime_dir = self.runtime_dir / session_id
        runtime_dir.mkdir(parents=True, exist_ok=True)

        profile = load_profile(self.profiles_dir, profile_name)
        session = ConsoleSession(session_id, name, port, profile, log_dir, runtime_dir)
        session.open()
        self.sessions[session_id] = session

        meta_path = runtime_dir / "meta.json"
        meta_path.write_text(json.dumps({
            "session_id": session_id,
            "name": name,
            "port": port,
            "profile": profile_name,
            "log_path": str(session.log_path),
            "events_path": str(session.events_path)
        }, indent=2))
        return session

    def close_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session:
            session.close()

    def get_session(self, session_id: str) -> Optional[ConsoleSession]:
        return self.sessions.get(session_id)

    def list_sessions(self) -> List["ConsoleSession"]:
        return list(self.sessions.values())
