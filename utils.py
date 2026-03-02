import hashlib
from datetime import datetime
from pathlib import Path

import socket
import threading
import time
import logging
import atexit
import subprocess
import os

from ci_core import TimeoutException
from ci_core import BaochipCIRunner

logger = logging.getLogger(__name__)

def get_file_md5(filepath):
    md5_hash = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()

def get_file_mtime(filepath):
    mtime = Path(filepath).stat().st_mtime
    return datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')

SOCKET_PATH = "/tmp/serial_bridge.sock"
BRIDGE_BINARY = "/home/bunnie/code/testjig/crial_helper"

def _ensure_bridge(port: str):
    if os.path.exists(SOCKET_PATH):
        try:
            test = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            test.connect(SOCKET_PATH)
            test.close()
            return  # already up
        except OSError:
            os.unlink(SOCKET_PATH)  # stale socket

    subprocess.Popen(
        [BRIDGE_BINARY, port],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if os.path.exists(SOCKET_PATH):
            try:
                test = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                test.connect(SOCKET_PATH)
                test.close()
                return  # up and accepting
            except OSError:
                pass
        time.sleep(0.05)

    raise RuntimeError("serial_bridge failed to start within 5s")


class SerialLogger:
    KBD_DELAY = 0.15

    def __init__(self, port: str, logfile: str, baudrate: int = 1_000_000):
        _ensure_bridge(port)

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(SOCKET_PATH)

        self.filename = logfile
        self.running = True

        self._send_lock = threading.Lock()
        self._buf_lock = threading.Lock()
        self._log_buf: list[str] = []
        self._cap_buf: list[str] | None = None

        atexit.register(self.cleanup)
        self._reader_thread = threading.Thread(target=self._read_serial, daemon=True)
        self._reader_thread.start()

    def _read_serial(self):
        buf = b''
        while self.running:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    logging.error("Bridge disconnected")
                    self.running = False
                    break

                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    decoded = (line + b'\n').decode('utf-8', errors='replace') \
                                            .replace('\r\n', '\n').replace('\r', '\n')
                    with self._buf_lock:
                        self._log_buf.append(decoded)
                        if self._cap_buf is not None:
                            self._cap_buf.append(decoded)

            except OSError as e:
                if self.running:
                    logging.error(f"Socket read error: {e}")
                self.running = False
                break

    def send_command(self, command: str, timeout: float = 1.0, expect_response: bool = True) -> str:
        with self._send_lock:
            if expect_response:
                with self._buf_lock:
                    self._cap_buf = []

            payload = b'\x01' + f"\r{command}\r".encode('utf-8')
            self._sock.send(payload)

            if not expect_response:
                return ""

            send_time = len(payload) * self.KBD_DELAY
            time.sleep(send_time + timeout)

            with self._buf_lock:
                result = ''.join(self._cap_buf)
                self._cap_buf = None

            return result.strip()

    def clear_log(self):
        with self._buf_lock:
            self._log_buf.clear()

    def get_log(self) -> str:
        with self._buf_lock:
            return ''.join(self._log_buf)

    def commit_log(self):
        with self._buf_lock:
            data = ''.join(self._log_buf)
        with open(self.filename, 'w') as f:
            f.write(data)

    def wait_for_output(self, target_string: str, timeout: float = 5.0, instances: int = 1, log: bool = True) -> int:
        start_time = time.time()
        while time.time() - start_time < timeout:
            content = self.get_log()
            count = content.count(target_string)
            if count >= instances:
                if log:
                    logging.info(f"'{target_string}' found {count} times")
                return count
            time.sleep(0.25)
        raise TimeoutError(f"'{target_string}' not found within {timeout}s")

    def cleanup(self):
        self.running = False
        self._reader_thread.join(timeout=2)
        self._sock.close()