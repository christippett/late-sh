import fcntl
import os
import pty
import select
import struct
import subprocess
import termios
import time

from .parser import parse_vt100


class LateClient:
    """
    A reusable client that manages an interactive SSH session to late.sh,
    sends commands, and returns the rendered terminal output.
    """

    def __init__(self, host="late.sh", port=22, user=None, width=120, height=80):
        self.host = host
        self.port = port
        self.user = user
        self.width = width
        self.height = height
        self.master = None
        self.slave = None
        self.proc = None
        self.output = b""

    def connect(self):
        self.master, self.slave = pty.openpty()

        # Force a predictable terminal layout
        winsize = struct.pack("HHHH", self.height, self.width, 0, 0)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, winsize)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, winsize)

        cmd = ["ssh", "-p", str(self.port), "-o", "StrictHostKeyChecking=no", "-q"]

        # Prevent SSH option injection if user/host starts with a dash
        cmd.append("--")

        if self.user:
            cmd.append(f"{self.user}@{self.host}")
        else:
            cmd.append(self.host)

        self.proc = subprocess.Popen(
            cmd, stdin=self.slave, stdout=self.slave, stderr=self.slave
        )
        # Give the SSH connection and splash screen time to load
        time.sleep(1.5)

    def send_keys(self, keys: str):
        if self.master:
            os.write(self.master, keys.encode("utf-8"))

    def read_raw(self, timeout=2.0) -> str:
        """
        Wait for a timeout period and consume the output buffer
        """
        time.sleep(timeout)
        out = b""
        while True:
            r, _, _ = select.select([self.master], [], [], 0.5)
            if r:
                try:
                    data = os.read(self.master, 4096)
                    if not data:
                        break
                    out += data
                except OSError:
                    break
            else:
                break
        self.output += out
        return out.decode("utf-8", errors="ignore")

    def get_screen(self) -> list[str]:
        """
        Parse all accumulated output and return the final VT100 rendered screen lines
        """
        text = self.output.decode("utf-8", errors="ignore")
        return parse_vt100(text, width=self.width, height=self.height)

    def close(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None
        if self.slave is not None:
            try:
                os.close(self.slave)
            except OSError:
                pass
            self.slave = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
