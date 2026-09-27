import codecs
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import threading

from .parser import Vt100Screen

# Mouse-tracking DECSET sequences the app enables for its own UI. In --watch
# mode the bot mirrors the app's raw VT100 straight to the viewer's terminal;
# blindly forwarding these would put the viewer's terminal into mouse-reporting
# mode, so every cursor motion generates escape sequences that leak back into
# the display. Strip them to keep the viewer read-only.
_MOUSE_TRACKING_ON = (
    b'\x1b[?1000h',
    b'\x1b[?1002h',
    b'\x1b[?1003h',
    b'\x1b[?1006h',
)
_MAX_MOUSE_SEQ = max(len(s) for s in _MOUSE_TRACKING_ON)


def _strip_mouse_tracking(data, pending):
    """Remove mouse-tracking-enable sequences from ``data``.

    ``pending`` holds a trailing prefix from the previous call so a sequence
    split across two reads is still stripped.
    """
    buf = pending + data
    for seq in _MOUSE_TRACKING_ON:
        buf = buf.replace(seq, b'')
    for n in range(_MAX_MOUSE_SEQ - 1, 0, -1):
        if len(buf) >= n and any(seq.startswith(buf[-n:]) for seq in _MOUSE_TRACKING_ON):
            return buf[:-n], buf[-n:]
    return buf, b''


class LiveBotClient:
    """
    A reusable SSH client for autonomous bots. It maintains a background thread
    that continuously reads from the PTY, enabling real-time VT100 parsing 
    and optional live-streaming of the ANSI output to the user's terminal.
    """
    def __init__(self, watch=False, width=120, height=40, host='late'):
        self.host = host
        self.master, self.slave = pty.openpty()
        self.watch = watch
        self.width = width
        self.height = height
        
        # Turn off ECHO and ICANON so we don't interfere with the raw stream
        attrs = termios.tcgetattr(self.slave)
        attrs[3] = attrs[3] & ~termios.ECHO & ~termios.ICANON
        termios.tcsetattr(self.slave, termios.TCSANOW, attrs)

        winsize = struct.pack("HHHH", self.height, self.width, 0, 0)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, winsize)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, winsize)

        self.proc = None
        self.output = bytearray()
        self.running = False
        self.reader_thread = None
        self._decoder = codecs.getincrementaldecoder('utf-8')('ignore')
        self._screen = Vt100Screen(width, height)
        self._consumed = 0
        self._mirror_pending = b''
        self._saved_termios = None

    def log(self, msg):
        """Prints logs only if we aren't overriding stdout with --watch."""
        if not self.watch:
            print(msg)

    def connect(self):
        # We rely on the user's ~/.ssh/config containing the `late` host, 
        # so it inherently inherits their identity and bypasses onboarding!
        cmd = ['ssh', self.host]
        self.log(f"Executing: {' '.join(cmd)}")
        self.proc = subprocess.Popen(cmd, stdin=self.slave, stdout=self.slave, stderr=self.slave)
        self.running = True

        if self.watch:
            # Make the viewer read-only: stop the terminal echoing user input,
            # disable any pre-enabled mouse tracking, then clear the screen.
            if os.isatty(0):
                try:
                    saved = termios.tcgetattr(0)
                    raw = list(saved)
                    raw[3] = raw[3] & ~termios.ECHO & ~termios.ICANON
                    termios.tcsetattr(0, termios.TCSANOW, raw)
                    self._saved_termios = saved
                except OSError:
                    self._saved_termios = None
            sys.stdout.write('\x1b[?1006l\x1b[?1003l\x1b[?1000l\x1b[2J\x1b[H')
            sys.stdout.flush()

        # Start a background reader thread
        self.reader_thread = threading.Thread(target=self._reader)
        self.reader_thread.daemon = True
        self.reader_thread.start()

    def send_keys(self, keys: str):
        if self.master:
            os.write(self.master, keys.encode('utf-8'))
            
    def send_raw(self, data: bytes):
        if self.master:
            os.write(self.master, data)

    def _reader(self):
        while self.running and self.proc.poll() is None:
            try:
                r, _, _ = select.select([self.master], [], [], 0.05)
                if not r:
                    continue
                data = os.read(self.master, 4096)
                if not data:
                    break
                self.output.extend(data)

                if self.watch:
                    # Mirror the raw VT100 to the user's terminal, with
                    # mouse-tracking-enable sequences stripped so the viewer
                    # never becomes a mouse reporter.
                    filtered, self._mirror_pending = _strip_mouse_tracking(
                        data, self._mirror_pending
                    )
                    sys.stdout.buffer.write(filtered)
                    sys.stdout.buffer.flush()
            except OSError:
                # The PTY was closed by close(): stop reading cleanly.
                break

    def get_screen(self):
        end = len(self.output)
        raw = bytes(self.output[self._consumed:end])
        self._consumed = end
        self._screen.feed(self._decoder.decode(raw))
        return self._screen.lines()
        
    def cleanup_terminal(self):
        """
        Resets terminal attributes, restores cursor visibility, disables mouse
        tracking / alternate screen buffers, and clears leftover ANSI characters.
        """
        reset_sequences = (
            '\x1b[0m'          # Reset all text styling/attributes
            '\x1b[39m\x1b[49m' # Reset default foreground and background colors
            '\x1b[?1000l'      # Disable standard mouse click tracking
            '\x1b[?1002l'      # Disable mouse button-event tracking
            '\x1b[?1003l'      # Disable all-motion mouse tracking
            '\x1b[?1006l'      # Disable SGR extended mouse mode
            '\x1b[?2004l'      # Disable bracketed paste mode
            '\x1b[?25h'        # Show/restore cursor visibility
            '\x1b[?1049l'      # Exit alternate screen buffer
            '\x1b[2J'          # Clear screen
            '\x1b[H'           # Move cursor to top-left (home)
        )
        sys.stdout.write(reset_sequences)
        sys.stdout.flush()
        if self._saved_termios is not None:
            try:
                termios.tcsetattr(0, termios.TCSANOW, self._saved_termios)
            except OSError:
                pass
            self._saved_termios = None

    def close(self):
        self.running = False
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=0.5)
            except Exception:
                pass
        for fd in (self.master, self.slave):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if self.reader_thread is not None:
            self.reader_thread.join(timeout=1.0)
        if self.watch:
            self.cleanup_terminal()

    def is_alive(self):
        return self.running and self.proc and self.proc.poll() is None
