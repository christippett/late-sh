#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "asyncssh>=2.14.0",
#     "typer>=0.9.0",
#     "shellingham",
#     "chafa.py>=1.2.0",
#     "pillow>=10.0.0",
# ]
# ///
"""
artboard_painter.py - Automated / assisted drawing tool for late.sh communal artboard (Tab 4).

Takes an input image, renders via native chafa.py Canvas, generates optimized keystroke streams,
and connects via SSH to paint onto the artboard canvas.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, AnyStr, NamedTuple, Self

import asyncssh
import chafa
import typer
from asyncssh import Error, SSHClientConnection, SSHClientProcess
from chafa.canvas import Canvas
from chafa.loader import Loader

logger = logging.getLogger(__name__)

CANVAS_WIDTH = 384
CANVAS_HEIGHT = 192
FONT_WIDTH = 16
FONT_HEIGHT = 30

ANSI_STRIP_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class Color(NamedTuple):
    r: int
    g: int
    b: int

    def __str__(self) -> str:
        return f"#{self.r:02X}{self.g:02X}{self.b:02X}"

    @classmethod
    def from_hex(cls, value: str) -> Color:
        """Parse and validate a hex color string (e.g. '#FFAABB')."""
        if not re.match(r"^#([0-9A-Fa-f]{6})$", value):
            raise ValueError(f"Invalid hex color format: {value!r}. Expected '#RRGGBB'.")
        r = int(value[1:3], 16)
        g = int(value[3:5], 16)
        b = int(value[5:7], 16)
        return cls(r, g, b)


@dataclass(frozen=True)
class Coords:
    x: int
    y: int

    def __str__(self) -> str:
        return f"{self.x},{self.y}"

    @classmethod
    def from_string(cls, value: str) -> Coords:
        """Parse and validate comma-separated 'x,y' canvas coordinates."""
        parts = [v.strip() for v in value.split(",")]
        if len(parts) != 2:
            raise ValueError(f"Coords must be 'x,y', got {value!r}")
        try:
            x, y = int(parts[0]), int(parts[1])
        except ValueError as err:
            raise ValueError(f"Invalid integer coordinates in {value!r}") from err

        if not (0 <= x < CANVAS_WIDTH and 0 <= y < CANVAS_HEIGHT):
            raise ValueError(f"Coords ({x}, {y}) out of bounds (canvas is {CANVAS_WIDTH}x{CANVAS_HEIGHT}).")
        return cls(x, y)

    @classmethod
    def from_input(cls, value: Coords | str | tuple[int, int]) -> Coords:
        """Coerce Coords instance, 'x,y' string, or (x, y) tuple into a validated Coords."""
        if isinstance(value, cls):
            if not (0 <= value.x < CANVAS_WIDTH and 0 <= value.y < CANVAS_HEIGHT):
                raise ValueError(f"Coords ({value.x}, {value.y}) out of bounds ({CANVAS_WIDTH}x{CANVAS_HEIGHT}).")
            return value
        if isinstance(value, str):
            return cls.from_string(value)
        if isinstance(value, (tuple, list)) and len(value) == 2:
            x, y = int(value[0]), int(value[1])
            if not (0 <= x < CANVAS_WIDTH and 0 <= y < CANVAS_HEIGHT):
                raise ValueError(f"Coords ({x}, {y}) out of bounds ({CANVAS_WIDTH}x{CANVAS_HEIGHT}).")
            return cls(x, y)
        raise TypeError(f"Cannot convert {type(value).__name__} to Coords: {value!r}")


@dataclass
class RemoteHost:
    host: str
    port: int | None = None
    username: str | None = None
    identity_file: Path | None = None

    def __str__(self) -> str:
        res = self.host
        if self.username:
            res = f"{self.username}@{res}"
        if self.port is not None:
            res = f"{res}:{self.port}"
        return res

    @classmethod
    def from_string(cls, value: str) -> RemoteHost:
        scheme = re.compile(r"^(?:ssh://)?(?:(?P<username>[^@]+)@)?(?P<host>[^:]+)(?::(?P<port>\d+))?$")
        if m := scheme.match(value.strip()):
            d = m.groupdict()
            return cls(
                host=d["host"],
                username=d["username"],
                port=int(d["port"]) if d["port"] is not None else None,
            )
        raise ValueError(f"Invalid remote host format: {value!r}. Expected '[user@]host[:port]'.")

    @classmethod
    def from_input(cls, value: RemoteHost | str) -> RemoteHost:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls.from_string(value)
        raise TypeError(f"Cannot convert {type(value).__name__} to RemoteHost: {value!r}")

    def is_prod(self) -> bool:
        return "late.sh" in self.host or self.host == "159.203.111.45"

    async def connect(self) -> SSHClientConnection:
        kwargs: dict = {}
        if self.port is not None:
            kwargs["port"] = self.port
        if self.username is not None:
            kwargs["username"] = self.username
        if self.identity_file is not None and self.identity_file.exists(follow_symlinks=True):
            kwargs["client_keys"] = [asyncssh.read_private_key(self.identity_file)]
        return await asyncssh.connect(self.host, **kwargs)


class KC(bytes, Enum):
    NUL = b"\x00"
    ESC = b"\x1b"
    ENTER = b"\r"
    CTRL_C = b"\x03"
    CTRL_D = b"\x04"
    CTRL_L = b"\x0c"
    CTRL_U = b"\x15"
    CTRL_W = b"\x17"
    CTRL_K = b"\x0b"
    UP = b"\x1b[A"
    DOWN = b"\x1b[B"
    RIGHT = b"\x1b[C"
    LEFT = b"\x1b[D"
    SHIFT_RIGHT = b"\x1b[1;2C"
    PASTE_START = b"\x1b[200~"
    PASTE_END = b"\x1b[201~"


class PaintPalette(Enum):
    ORANGE = Color(255, 110, 64)
    YELLOW = Color(255, 236, 96)
    AMBER = Color(255, 214, 102)
    LIME = Color(145, 226, 88)
    PASTEL_LIME = Color(188, 255, 128)
    TEAL = Color(72, 220, 170)
    CYAN = Color(86, 245, 214)
    SKY_BLUE = Color(84, 196, 255)
    ICE_BLUE = Color(96, 225, 255)
    PERIWINKLE = Color(128, 163, 255)
    LAVENDER = Color(164, 146, 255)
    LILAC = Color(192, 132, 255)
    MAGENTA = Color(224, 116, 255)
    HOT_PINK = Color(255, 124, 196)
    SALMON_PINK = Color(255, 142, 158)
    OFF_WHITE = Color(238, 242, 255)


DEFAULT_REMOTE = RemoteHost(host="late-dev")
DEFAULT_ORIGIN = Coords(x=0, y=0)
DEFAULT_COLOR = Color(255, 255, 255)


class ArtboardClient:
    """Manages SSH session, PTY process, navigation, and canvas stream painting."""

    conn: SSHClientConnection
    proc: SSHClientProcess

    def __init__(self, remote: RemoteHost):
        self.remote = remote
        self.raw_buffer = bytearray()
        self.reader_task: asyncio.Task | None = None
        self.cursor = Coords(0, 0)

    async def __aenter__(self) -> Self:
        self.conn = await self.remote.connect()
        self.proc = await self.conn.create_process(
            term_type="xterm-256color",
            term_size=(500, 200),
            encoding=None,
        )

        async def _reader():
            try:
                while self.proc is not None:
                    chunk = await self.proc.stdout.read(4096)
                    if not chunk:
                        break
                    if len(self.raw_buffer) < 100_000:
                        self.raw_buffer.extend(chunk)
                    else:
                        del self.raw_buffer[:-10_000]
                        self.raw_buffer.extend(chunk)
            except (Error, OSError):
                pass

        self.reader_task = asyncio.create_task(_reader())
        await self.initialize_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.write(KC.ESC, 0.5)  # Send ESC to exit interactive mode
        if self.reader_task is not None:
            self.reader_task.cancel()
        if self.proc is not None:
            self.proc.close()
        if self.conn is not None:
            self.conn.close()

    async def write(self, data: AnyStr, cooldown: float = 0.005) -> None:
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()
        await asyncio.sleep(cooldown)

    def get_clean_buffer(self) -> str:
        return ANSI_STRIP_RE.sub("", self.raw_buffer.decode("utf-8", errors="replace"))

    async def wait_for_pattern(self, pattern: str, timeout: float = 5.0) -> bool:
        regex = re.compile(pattern)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            clean = self.get_clean_buffer()
            if regex.search(clean):
                return True
            await asyncio.sleep(0.05)
        return False

    async def initialize_session(self) -> None:
        # 1. Dismiss splash screen
        print("[*] Dismissing splash screen (Esc)...")
        await self.write(KC.ESC, 0.2)

        # 2. Switch to Tab 4 (Artboard)
        print("[*] Navigating to Screen::Artboard (Tab 4)...")
        await self.write(b"4")
        await self.wait_for_pattern(r"Mode|Artboard", timeout=3.0)

        # 3. Enter interactive edit mode ('i')
        print("[*] Entering interactive edit mode ('i')...")
        await self.write(b"i")
        await self.wait_for_pattern(r"Mode\s+active|active", timeout=3.0)

    def detect_active_color(self) -> Color:
        clean = self.get_clean_buffer()
        if m := re.findall(r"Color\s+(#[0-9A-Fa-f]{6})", clean):
            return Color.from_hex(m[-1])
        return DEFAULT_COLOR

    async def reset_cursor_to_origin(self, origin: Coords) -> None:
        print(f"[*] Resetting cursor to (0, 0) and navigating to ({origin.x}, {origin.y})...")
        init_nav = [KC.UP * 200, KC.LEFT * 400, KC.DOWN * origin.y, KC.RIGHT * origin.x]
        await self.write(b"".join(init_nav))
        self.cursor = origin

    async def stream_tokens(self, tokens: Sequence[bytes], chunk_size: int = 1024) -> None:
        """Stream atomic byte tokens in batched chunks over SSH."""
        current_chunk = bytearray()
        for tok in tokens:
            if len(current_chunk) + len(tok) > chunk_size:
                await self.write(bytes(current_chunk))
                current_chunk.clear()
            current_chunk.extend(tok)
        if current_chunk:
            await self.write(bytes(current_chunk))

    def _get_display_width(self, ch: str) -> int:
        """Return display column width of a character on the terminal canvas (1 or 2)."""
        return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1

    def _generate_drawing_tokens(
        self,
        canvas: chafa.Canvas,
        origin: Coords,
        initial_color: Color = DEFAULT_COLOR,
        debug: bool = False,
    ) -> list[bytes]:
        """Generate atomic byte tokens to draw a native chafa.Canvas onto late.sh Artboard."""
        tokens: list[bytes] = []
        curr_x = origin.x
        curr_y = origin.y
        curr_rgb_color = initial_color if initial_color is not None else DEFAULT_COLOR

        def move_to(target_x: int, target_y: int):
            nonlocal curr_x, curr_y
            dy = target_y - curr_y
            dx = target_x - curr_x

            if dy > 0:
                tokens.append(KC.DOWN * dy)
            elif dy < 0:
                tokens.append(KC.UP * (-dy))

            if dx > 0:
                tokens.append(KC.RIGHT * dx)
            elif dx < 0:
                tokens.append(KC.LEFT * (-dx))

            curr_x = target_x
            curr_y = target_y

        def set_hex_color(rgb: tuple[int, int, int]):
            nonlocal curr_rgb_color
            if curr_rgb_color == rgb:
                return
            hex_str = f"{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
            tokens.append(KC.CTRL_K + hex_str.encode("ascii") + KC.ENTER)
            curr_rgb_color = rgb

        for y, row_gen in enumerate(canvas[:]):
            row = list(row_gen)
            if debug:
                row_str = f"{y:03d}"
                for i, ch in enumerate(row_str):
                    if i < len(row):
                        row[i].char = ch
                        row[i].fg_color = DEFAULT_COLOR

            col = 0
            idx = 0
            while idx < len(row):
                insp = row[idx]
                ch = insp.char

                # Double-width continuation cell: skip column offset update as origin cell handled it
                if ch.encode() == KC.NUL:
                    idx += 1
                    continue

                w = self._get_display_width(ch)
                if ch == " " or insp.fg_color is None:
                    col += w
                    idx += 1
                    continue

                start_col = col
                run_chars = []
                run_visual_width = 0
                target_rgb = insp.fg_color

                while idx < len(row):
                    c = row[idx]
                    if c.char.encode() == KC.NUL:
                        idx += 1
                        continue
                    cw = self._get_display_width(c.char)
                    if c.char == " " or c.fg_color != target_rgb:
                        break
                    run_chars.append(c.char)
                    run_visual_width += cw
                    col += cw
                    idx += 1

                tx = origin.x + start_col
                ty = origin.y + y

                if curr_x != tx or curr_y != ty:
                    move_to(tx, ty)

                set_hex_color(target_rgb)

                # Emit bracketed paste for run: \x1b[200~<text>\x1b[201~
                run_text = "".join(run_chars)
                run_bytes = run_text.encode("utf-8")
                tokens.append(KC.PASTE_START + run_bytes + KC.PASTE_END)
                curr_x += run_visual_width
                if curr_x >= CANVAS_WIDTH:
                    curr_x = CANVAS_WIDTH - 1

        return tokens

    async def draw_canvas(self, canvas: chafa.Canvas, origin: Coords, debug: bool = False) -> None:
        """High-level client method to detect remote color, optimize canvas commands, and stream drawing."""
        active_color = self.detect_active_color()
        print(f"[*] Detected active paint color: {active_color}.")

        tokens = self._generate_drawing_tokens(
            canvas,
            origin=origin,
            initial_color=active_color,
            debug=debug,
        )

        await self.reset_cursor_to_origin(origin)
        total_bytes = sum(len(t) for t in tokens)
        print(f"[*] Streaming {total_bytes} bytes ({len(tokens)} operations) of drawing operations...")
        await self.stream_tokens(tokens)


class Chafa:
    def __init__(self, image_path: Path, size: str | None = None, symbols: str | None = None):
        image = Loader(str(image_path))

        config = chafa.CanvasConfig()
        config.pixel_mode = chafa.PixelMode.CHAFA_PIXEL_MODE_SYMBOLS
        config.canvas_mode = chafa.CanvasMode.CHAFA_CANVAS_MODE_TRUECOLOR
        config.cell_width = FONT_WIDTH
        config.cell_height = FONT_HEIGHT
        config.fg_only = True

        symbol_map = chafa.SymbolMap()
        symbol_map.apply_selectors(symbols or "all")
        config.set_symbol_map(symbol_map)

        if size:
            w, h = re.split(r"[,xX]", size)
            config.width = int(w)
            config.height = int(h)

        config.calc_canvas_geometry(image.width, image.height, FONT_WIDTH / FONT_HEIGHT)

        self.image = image
        self.config = config

    def get_canvas(self) -> Canvas:
        """
        Render an image file directly into a native chafa.Canvas.
        """

        canvas = chafa.Canvas(self.config)
        canvas.draw_all_pixels(
            self.image.pixel_type,
            self.image.get_pixels(),
            self.image.width,
            self.image.height,
            self.image.rowstride,
        )
        return canvas

    def preview(self, debug=False):
        canvas = self.get_canvas()
        if debug:
            for y, row_gen in enumerate(canvas[:]):
                row_str = f"{y:03d}"
                for i, ch in enumerate(row_str):
                    if i < self.config.width:
                        canvas[y, i].char = ch
                        canvas[y, i].fg_color = DEFAULT_COLOR
        output = canvas.print()
        print(output.decode())


app = typer.Typer(
    name="artboard_painter",
    help="Automated / assisted drawing tool for late.sh communal artboard (Tab 4).",
    add_completion=False,
)


@app.command()
def main(
    image: Annotated[Path, typer.Argument(help="Path to input image file.", exists=True)],
    origin: Annotated[
        Coords,
        typer.Option(parser=Coords.from_input, help="Starting coordinates (e.g. '10,10')."),
    ] = DEFAULT_ORIGIN,
    remote: Annotated[RemoteHost, typer.Option(parser=RemoteHost.from_input)] = DEFAULT_REMOTE,
    size: str | None = typer.Option(None, help="Set maximum image dimensions in columns and rows."),
    symbols: str = typer.Option(
        "block,border",
        help="Specify character symbols to employ in final output.",
    ),
    preview: bool = typer.Option(
        False,
        help="Output the ANSI color-mapped preview to terminal stdout.",
    ),
    danger_mode: bool = typer.Option(
        False,
        help="Bypass production safety gate and allow running against live late.sh instance.",
    ),
    debug: bool = typer.Option(
        False,
        help="Render row numbers (e.g. 000, 001) in white at columns 0-2 for alignment tracking.",
    ),
    identity: Annotated[
        Path | None,
        typer.Option(envvar="LATE_SSH_IDENTITY", help="Path to SSH private key file."),
    ] = None,
) -> None:
    """Draw artwork onto late.sh artboard."""
    if not image.exists():
        print("Image not found.")
        raise typer.Exit(code=2)

    if identity and identity.exists(follow_symlinks=True):
        remote.identity_file = identity

    if remote.is_prod() and not danger_mode:
        print(
            "[SAFETY ERROR] Direct execution against production late.sh is strictly forbidden! "
            "(pass --danger-mode to override)."
        )
        raise typer.Exit(code=1)

    async def _ssh(canvas):
        print(f"[*] Connecting to {remote}...")
        async with ArtboardClient(remote) as client:
            await client.draw_canvas(canvas, origin=origin, debug=debug)
        print("[*] Done!")

    painter = Chafa(image_path=image, size=size, symbols=symbols)
    canvas = painter.get_canvas()
    if preview:
        painter.preview(debug)
        return

    asyncio.run(_ssh(canvas))


if __name__ == "__main__":
    app()
