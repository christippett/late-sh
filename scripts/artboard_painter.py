#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
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

Takes an input image, renders via chafa, maps colors to late.sh's PAINT_PALETTE
with contrast preservation, and generates optimized keystroke streams or connects via SSH.
"""

import asyncio
import json
import logging
import math
import re
import sys
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import chafa
import typer
from chafa import SymbolMap
from chafa.loader import Loader

logger = logging.getLogger(__name__)


@dataclass
class LateHost:
    host: str
    port: int = 2222
    username: str = "drawer"
    identity_file: Path | None = None

    def __str__(self):
        return f"{self.username}@{self.host}:{self.port}"


def char_display_width(ch: str) -> int:
    """Return display column width of a character on the terminal canvas (1 or 2)."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


# late-ssh/src/app/artboard/state.rs: PAINT_PALETTE (16 colors)
# Index 0..15
PAINT_PALETTE: list[tuple[int, int, int]] = [
    (255, 110, 64),  # 0: Coral / Orange
    (255, 236, 96),  # 1: Bright Yellow (default)
    (255, 214, 102),  # 2: Golden Amber
    (145, 226, 88),  # 3: Lime Green
    (188, 255, 128),  # 4: Pastel Lime
    (72, 220, 170),  # 5: Mint / Teal
    (86, 245, 214),  # 6: Aqua Cyan
    (84, 196, 255),  # 7: Sky Blue
    (96, 225, 255),  # 8: Ice Blue
    (128, 163, 255),  # 9: Periwinkle
    (164, 146, 255),  # 10: Lavender
    (192, 132, 255),  # 11: Lilac Purple
    (224, 116, 255),  # 12: Magenta
    (255, 124, 196),  # 13: Hot Pink
    (255, 142, 158),  # 14: Salmon Pink
    (238, 242, 255),  # 15: Off-White
]

CANVAS_WIDTH = 384
CANVAS_HEIGHT = 192

# Standard 16 ANSI colors for SGR 30-37 / 90-97
ANSI_16_COLORS: list[tuple[int, int, int]] = [
    (0, 0, 0),  # 30: Black
    (205, 0, 0),  # 31: Red
    (0, 205, 0),  # 32: Green
    (205, 205, 0),  # 33: Yellow
    (0, 0, 238),  # 34: Blue
    (205, 0, 205),  # 35: Magenta
    (0, 205, 205),  # 36: Cyan
    (229, 229, 229),  # 37: White
    (127, 127, 127),  # 90: Bright Black (Gray)
    (255, 0, 0),  # 91: Bright Red
    (0, 255, 0),  # 92: Bright Green
    (255, 255, 0),  # 93: Bright Yellow
    (92, 92, 255),  # 94: Bright Blue
    (255, 0, 255),  # 95: Bright Magenta
    (0, 255, 255),  # 96: Bright Cyan
    (255, 255, 255),  # 97: Bright White
]


def rgb_to_cielab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """Convert sRGB (0-255) to CIELAB (L*, a*, b*)."""
    r, g, b = [c / 255.0 for c in rgb]

    # Gamma correction to linear RGB
    r = r / 12.92 if r <= 0.04045 else ((r + 0.055) / 1.055) ** 2.4
    g = g / 12.92 if g <= 0.04045 else ((g + 0.055) / 1.055) ** 2.4
    b = b / 12.92 if b <= 0.04045 else ((b + 0.055) / 1.055) ** 2.4

    # Convert to XYZ with standard D65 illuminant
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041

    # Normalize for D65 (Xn, Yn, Zn) = (0.95047, 1.00000, 1.08883)
    x /= 0.95047
    y /= 1.00000
    z /= 1.08883

    def f(t: float) -> float:
        delta = 6.0 / 29.0
        return (
            t ** (1.0 / 3.0) if t > delta**3 else (t / (3.0 * delta**2)) + (4.0 / 29.0)
        )

    fx, fy, fz = f(x), f(y), f(z)
    l_star = 116.0 * fy - 16.0
    a_star = 500.0 * (fx - fy)
    b_star = 200.0 * (fy - fz)
    return l_star, a_star, b_star


def delta_e_cielab(
    lab1: tuple[float, float, float], lab2: tuple[float, float, float]
) -> float:
    """Euclidean distance in CIELAB (CIE76 delta E)."""
    return math.sqrt(
        (lab1[0] - lab2[0]) ** 2 + (lab1[1] - lab2[1]) ** 2 + (lab1[2] - lab2[2]) ** 2
    )


# Precompute CIELAB values for PAINT_PALETTE
PALETTE_LAB: list[tuple[float, float, float]] = [
    rgb_to_cielab(c) for c in PAINT_PALETTE
]


class Cell:
    def __init__(self, char: str = " ", color: tuple[int, int, int] | None = None):
        self.char = char
        self.color = color  # (R, G, B) or None
        self.palette_idx: int = 1  # Default to yellow (index 1)


def parse_chafa_ansi(text: str) -> list[list[Cell]]:
    """
    Parse Chafa's terminal ANSI output into a 2D grid of Cell objects.
    Extracts character glyphs and 24-bit / 16-color ANSI foreground escapes.
    Strips cursor controls and background escapes.
    """
    lines = text.split("\n")
    grid: list[list[Cell]] = []

    # Matches any ANSI escape sequence: \x1b[ ... <final_char>
    escape_regex = re.compile(r"\x1b(\[[0-9;?]*[a-zA-Z]|\([B0-9]|\)[B0-9]|.)")
    sgr_regex = re.compile(r"^\x1b\[([0-9;]*)m$")

    for raw_line in lines:
        # Ignore terminal control lines (e.g. cursor hide/show) that contain no content
        clean_text = escape_regex.sub("", raw_line)
        if not clean_text:
            continue
        row: list[Cell] = []
        pos = 0
        current_color: tuple[int, int, int] | None = None

        while pos < len(raw_line):
            if raw_line[pos] == "\x1b":
                esc_match = escape_regex.match(raw_line, pos)
                if esc_match:
                    full_esc = esc_match.group(0)
                    pos = esc_match.end()
                    sgr_m = sgr_regex.match(full_esc)
                    if sgr_m:
                        seq = sgr_m.group(1)
                        if not seq or seq == "0" or seq == "39":
                            current_color = None
                        else:
                            parts = [int(p) for p in seq.split(";") if p.isdigit()]
                            i = 0
                            while i < len(parts):
                                code = parts[i]
                                if code == 0 or code == 39:
                                    current_color = None
                                    i += 1
                                elif code == 38:
                                    # Extended color: 38;2;R;G;B or 38;5;Idx
                                    if i + 1 < len(parts):
                                        mode = parts[i + 1]
                                        if mode == 2 and i + 4 < len(
                                            parts
                                        ):  # 24-bit RGB
                                            current_color = (
                                                parts[i + 2],
                                                parts[i + 3],
                                                parts[i + 4],
                                            )
                                            i += 5
                                        elif mode == 5 and i + 2 < len(
                                            parts
                                        ):  # 256-color
                                            c_idx = parts[i + 2]
                                            if c_idx < 16:
                                                current_color = ANSI_16_COLORS[c_idx]
                                            elif c_idx < 232:
                                                # 6x6x6 color cube
                                                ci = c_idx - 16
                                                b = (ci % 6) * 51
                                                g = ((ci // 6) % 6) * 51
                                                r = (ci // 36) * 51
                                                current_color = (r, g, b)
                                            else:
                                                # Grayscale ramp
                                                gray = (c_idx - 232) * 10 + 8
                                                current_color = (gray, gray, gray)
                                            i += 3
                                        else:
                                            i += 2
                                    else:
                                        i += 1
                                elif 30 <= code <= 37:
                                    current_color = ANSI_16_COLORS[code - 30]
                                    i += 1
                                elif 90 <= code <= 97:
                                    current_color = ANSI_16_COLORS[code - 90 + 8]
                                    i += 1
                                else:
                                    i += 1
                    continue
                else:
                    # Stray escape byte
                    pos += 1
                    continue

            char = raw_line[pos]
            row.append(Cell(char=char, color=current_color))
            pos += 1
        grid.append(row)

    # Trim trailing all-space rows if any
    while grid and all(c.char == " " for c in grid[-1]):
        grid.pop()
    return grid


def extract_owner_from_tui_text(text: str) -> str | None:
    """
    Extract the current Owner value from the Artboard Info overlay TUI screen.
    Returns the username if populated, or None if unpopulated ('?') or missing.
    """
    # Strip ANSI escape sequences
    clean = re.sub(r"\x1b(\[[0-9;?]*[a-zA-Z]|\([B0-9]|\)[B0-9]|.)", "", text)
    matches = re.findall(r"Owner\s+([^\s│\r\n]+)", clean)
    if matches:
        val = matches[-1].strip()
        if val and val != "?":
            return val
    return None


def fetch_artboard_occupied_cells(host: str) -> set[tuple[int, int]]:
    """
    Fetch occupied canvas coordinates (x, y) from the artboard web gallery endpoint.
    Returns a set of (x, y) tuples corresponding to cells already populated by users.
    """
    urls = [
        f"https://{host}/gallery"
        if "late.sh" in host
        else f"http://{host}:3000/gallery",
        f"http://{host}/gallery",
        "https://late.sh/gallery",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "late-art-drawer"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            m = re.search(
                r'<script id="snapshot-data"[^>]*>(.*?)</script>', html, re.DOTALL
            )
            if m:
                data = json.loads(m.group(1))
                cells = data.get("cells", [])
                occupied = set()
                for c in cells:
                    x, y, ch, w = c[0], c[1], c[2], c[3]
                    for dx in range(w):
                        occupied.add((x + dx, y))
                if occupied:
                    return occupied
        except ValueError:
            logger.warning(f"Failed to fetch occupied cells from {url}, trying next...")
            continue
    return set()


def generate_ansi_preview(grid: list[list[Cell]], palette_mode: bool = False) -> str:
    """Render the grid to an ANSI string using either exact 24-bit RGB or PAINT_PALETTE colors."""
    lines: list[str] = []
    current_color: tuple[int, int, int] | None = None
    current_idx: int = -1

    for row in grid:
        line_parts: list[str] = []
        for cell in row:
            if cell.char == " ":
                line_parts.append(" ")
                continue
            if palette_mode:
                if cell.palette_idx != current_idx:
                    current_idx = cell.palette_idx
                    r, g, b = PAINT_PALETTE[current_idx]
                    line_parts.append(f"\x1b[38;2;{r};{g};{b}m")
            else:
                color = (
                    cell.color
                    if cell.color is not None
                    else PAINT_PALETTE[cell.palette_idx]
                )
                if color != current_color:
                    current_color = color
                    line_parts.append(f"\x1b[38;2;{color[0]};{color[1]};{color[2]}m")
            line_parts.append(cell.char)
        line_parts.append("\x1b[0m")
        current_color = None
        current_idx = -1
        lines.append("".join(line_parts))

    return "\n".join(lines)


def optimize_drawing_stream(
    grid: list[list[Cell]],
    origin_x: int = 0,
    origin_y: int = 0,
    initial_color: tuple[int, int, int] | None = None,
    occupied_cells: set[tuple[int, int]] | None = None,
) -> tuple[bytes, dict[str, int]]:
    """
    Generate the minimal byte sequence to draw grid onto late.sh Artboard.
    Tracks visual cursor column (accounting for wide characters) and active color state.
    Color setting:
        Ctrl+K (0x0B) opens hex color picker, emits 6 uppercase hex digits, Enter (\r) applies.
      - Palette mode (optional fallback):
        Ctrl+U (0x15) = cycle_paint_color(-1)
        Ctrl+Y (0x19) = cycle_paint_color(+1)
    Navigation:
      Arrow keys: Up (\x1b[A), Down (\x1b[B), Right (\x1b[C), Left (\x1b[D)
    Painting:
      Emits contiguous same-colored character runs wrapped in bracketed paste
      (\\x1b[200~...\\x1b[201~). Automatically tracks display width (e.g. wide
      glyphs occupy 2 columns) so cursor navigation stays 100% synchronized with
      the artboard canvas engine.
    """
    stream = bytearray()
    stats = {
        "chars_typed": 0,
        "color_changes": 0,
        "nav_moves": 0,
        "total_bytes": 0,
    }

    curr_x = origin_x
    curr_y = origin_y
    curr_rgb_color = initial_color if initial_color is not None else PAINT_PALETTE[1]
    curr_palette_idx = 1

    def move_to(target_x: int, target_y: int):
        nonlocal curr_x, curr_y
        dy = target_y - curr_y
        dx = target_x - curr_x

        if dy > 0:
            stream.extend(b"\x1b[B" * dy)
            stats["nav_moves"] += dy
        elif dy < 0:
            stream.extend(b"\x1b[A" * (-dy))
            stats["nav_moves"] += -dy

        if dx > 0:
            stream.extend(b"\x1b[C" * dx)
            stats["nav_moves"] += dx
        elif dx < 0:
            stream.extend(b"\x1b[D" * (-dx))
            stats["nav_moves"] += -dx

        curr_x = target_x
        curr_y = target_y

    def set_palette_color(target_color: int):
        nonlocal curr_palette_idx
        if curr_palette_idx == target_color:
            return
        delta = (target_color - curr_palette_idx) % 16
        # Find shortest cycle path (forward with Ctrl+Y or backward with Ctrl+U)
        if delta <= 8:
            # Forward: delta times Ctrl+Y (0x19)
            stream.extend(b"\x19" * delta)
            stats["color_changes"] += delta
        else:
            # Backward: (16 - delta) times Ctrl+U (0x15)
            back = 16 - delta
            stream.extend(b"\x15" * back)
            stats["color_changes"] += back
        curr_palette_idx = target_color

    def set_hex_color(rgb: tuple[int, int, int]):
        nonlocal curr_rgb_color
        if curr_rgb_color == rgb:
            return
        # Ctrl+K (0x0B) opens hex color picker
        # 6 hex digits typed into picker
        # Enter (\r) applies working color to active paint color
        hex_str = "{:02X}{:02X}{:02X}".format(*rgb)
        stream.extend(b"\x0b" + hex_str.encode("ascii") + b"\r")
        stats["color_changes"] += 1
        curr_rgb_color = rgb

    height = len(grid)
    for y in range(height):
        row = grid[y]
        if not row:
            continue
        col = 0
        idx = 0
        while idx < len(row):
            cell = row[idx]
            w = char_display_width(cell.char)
            tx = origin_x + col
            ty = origin_y + y

            # In overlay mode, skip writing to cells that are already populated on the canvas
            is_occupied = False
            if occupied_cells is not None:
                is_occupied = any((tx + dx, ty) in occupied_cells for dx in range(w))

            if cell.char == " " or is_occupied:
                col += w
                idx += 1
                continue

            start_col = col
            run_chars = []
            run_visual_width = 0

            target_rgb = (
                cell.color
                if cell.color is not None
                else PAINT_PALETTE[cell.palette_idx]
            )
            while idx < len(row):
                c = row[idx]
                cw = char_display_width(c.char)
                ctx = origin_x + col
                c_occ = occupied_cells is not None and any(
                    (ctx + dx, ty) in occupied_cells for dx in range(cw)
                )
                c_rgb = c.color if c.color is not None else PAINT_PALETTE[c.palette_idx]
                if c.char == " " or c_occ or c_rgb != target_rgb:
                    break
                run_chars.append(c.char)
                run_visual_width += cw
                col += cw
                idx += 1
            set_hex_color(target_rgb)
            tx = origin_x + start_col
            ty = origin_y + y

            if curr_x != tx or curr_y != ty:
                move_to(tx, ty)

            # Emit bracketed paste for run: \x1b[200~<text>\x1b[201~
            run_text = "".join(run_chars)
            run_bytes = run_text.encode("utf-8")
            stream.extend(b"\x1b[200~" + run_bytes + b"\x1b[201~")
            stats["chars_typed"] += len(run_chars)
            curr_x += run_visual_width
            # Simulate the server's clamp logic in paste_cursor_end:
            # x: cursor.x.min(width.saturating_sub(1))
            if curr_x >= CANVAS_WIDTH:
                curr_x = CANVAS_WIDTH - 1

    stats["total_bytes"] = len(stream)
    return bytes(stream), stats


def parse_origin(origin: str) -> tuple[int, int]:
    """Parse and validate comma-separated 'x,y' canvas coordinates."""
    coords = [int(v.strip()) for v in origin.split(",")]
    if len(coords) != 2:
        raise ValueError(f"Origin must be 'x,y', got {origin!r}")
    x, y = coords[0], coords[1]
    if x < 0 or x >= CANVAS_WIDTH or y < 0 or y >= CANVAS_HEIGHT:
        raise ValueError(
            f"Origin ({x}, {y}) out of bounds (canvas is {CANVAS_WIDTH}x{CANVAS_HEIGHT})."
        )
    return x, y


def render_image_to_grid(
    image_path: str, size: str | None = None, symbol_map: SymbolMap | None = None
):
    """
    Render an image file via chafa into an ANSI cell grid using chafa.py, mapping colors as requested.
    """

    # Simple parse of expected flags we passed in main (this ignores complex manual flags,
    # but we only ever pass size and symbols from the CLI anyway)
    # The ladder rule: we don't need a full argparse, we just need to set the config up.
    config = chafa.CanvasConfig()
    # config.cell_width = 16
    # config.cell_height = 30
    config.width = CANVAS_WIDTH
    config.height = CANVAS_HEIGHT
    config.pixel_mode = chafa.PixelMode.CHAFA_PIXEL_MODE_SYMBOLS
    config.canvas_mode = chafa.CanvasMode.CHAFA_CANVAS_MODE_TRUECOLOR
    config.fg_only = True
    if symbol_map is not None:
        config.set_symbol_map(symbol_map)

    # Override width/height if --size was passed
    if size:
        w, h = re.split(r"[,xX]", size)
        config.width = int(w)
        config.height = int(h)

    image = Loader(image_path)
    config.calc_canvas_geometry(image.width, image.height, 16 / 30)
    canvas = chafa.Canvas(config)
    canvas.draw_all_pixels(
        image.pixel_type,
        image.get_pixels(),
        image.width,
        image.height,
        image.rowstride,
    )

    # Generating ANSI output and parsing it with parse_chafa_ansi
    ansi_output = canvas.print().decode("utf-8")
    ansi_output = ansi_output.replace("x1b", "\x1b")
    grid = parse_chafa_ansi(ansi_output)

    # Trim trailing all-space rows if any
    while grid and all(c.char == " " for c in grid[-1]):
        grid.pop()

    return grid


def prepare_drawing_stream(
    image_path: str,
    origin_x: int = 10,
    origin_y: int = 10,
    occupied_cells: set[tuple[int, int]] | None = None,
    initial_color: tuple[int, int, int] | None = None,
) -> tuple[list[list[Cell]], bytes, dict[str, int]]:
    """
    Process image and generate optimized drawing stream with stats.
    Returns (grid, drawing_stream, stats).
    """
    grid = render_image_to_grid(image_path)
    drawing_stream, stats = optimize_drawing_stream(
        grid,
        origin_x=origin_x,
        origin_y=origin_y,
        initial_color=initial_color,
        occupied_cells=occupied_cells,
    )
    return grid, drawing_stream, stats


async def execute_ssh_drawing(
    grid: list[list[Cell]],
    origin_x: int,
    origin_y: int,
    remote: LateHost,
    overlay: bool = False,
    occupied_cells: set[tuple[int, int]] | None = None,
) -> None:
    """Connect to late.sh via SSH and stream the drawing commands."""

    try:
        import asyncssh
    except ImportError:
        sys.stderr.write("Error: asyncssh not installed. Run via `uv run`.\n")
        sys.exit(1)

    height = len(grid)
    print(f"[*] Connecting to {remote}...")
    client_keys = None

    if remote.identity_file and remote.identity_file.exists(follow_symlinks=True):
        client_keys = [asyncssh.read_private_key(remote.identity_file)]

    conn = await asyncssh.connect(
        remote.host,
        port=remote.port,
        username=remote.username,
        client_keys=client_keys,
        agent_path=None,
        known_hosts=None,
        encoding=None,
    )
    proc = await conn.create_process(
        term_type="xterm-256color",
        # The artboard canvas is 384x192. A small terminal forces the artboard
        # viewport (~cols-26 x rows-2) to scroll constantly as the cursor moves,
        # and every scroll re-emits most of the viewport as SSH output. That
        # output backlog is what exceeds the server's 32MB budget and drops
        # input (the row-misalignment bug). Size the terminal to the server's
        # 500x200 clamp so the viewport covers the whole canvas and never scrolls.
        term_size=(500, 200),
        encoding=None,
    )
    raw_buffer = bytearray()

    async def reader():
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                # Prevent infinite memory growth while keeping enough history for sync
                if len(raw_buffer) < 100_000:
                    await asyncio.sleep(0.05)
                    raw_buffer.extend(chunk)
                else:
                    del raw_buffer[:-10_000]
                    raw_buffer.extend(chunk)

        except Exception:
            pass

    reader_task = asyncio.create_task(reader())

    # Wait briefly for splash screen to initialize
    await asyncio.sleep(0.5)
    # Dismiss splash screen with Esc
    print("[*] Dismissing splash screen (Esc)...")
    proc.stdin.write(b"\x1b")
    await proc.stdin.drain()
    await asyncio.sleep(0.2)
    # Navigate to Screen::Artboard (Tab 4)
    print("[*] Navigating to Screen::Artboard (Tab 4)...")
    proc.stdin.write(b"4")
    await proc.stdin.drain()
    await asyncio.sleep(0.3)
    # Enter interactive edit mode ('i')
    print("[*] Entering interactive edit mode ('i')...")
    proc.stdin.write(b"i")
    await proc.stdin.drain()
    await asyncio.sleep(0.2)

    def detect_active_color() -> tuple[int, int, int]:
        text = raw_buffer.decode("utf-8", errors="replace")
        m = re.findall(r"Color\s+(#[0-9A-Fa-f]{6})", text)
        if m:
            hex_str = m[-1][1:]  # strip leading '#'
            try:
                r = int(hex_str[0:2], 16)
                g = int(hex_str[2:4], 16)
                b = int(hex_str[4:6], 16)
                return (r, g, b)
            except ValueError:
                pass
        return PAINT_PALETTE[1]  # Default fallback

    active_color = detect_active_color()
    print(
        f"[*] Detected active paint color: #{active_color[0]:02X}{active_color[1]:02X}{active_color[2]:02X}."
    )

    final_occupied_cells = occupied_cells
    if overlay and not occupied_cells:
        print(
            "[*] Web snapshot empty or unavailable. Probing canvas cells via TUI Owner field..."
        )
        tui_occupied = set()
        probe_curr_x, probe_curr_y = 0, 0
        proc.stdin.write(b"\x1b[A" * 200 + b"\x1b[D" * 400)
        await proc.stdin.drain()
        await asyncio.sleep(0.2)

        for gy in range(height):
            row = grid[gy]
            for gx in range(len(row)):
                cell = row[gx]
                if cell.char == " ":
                    continue
                tx = origin_x + gx
                ty = origin_y + gy
                dx = tx - probe_curr_x
                dy = ty - probe_curr_y
                nav = bytearray()
                if dy > 0:
                    nav.extend(b"\x1b[B" * dy)
                elif dy < 0:
                    nav.extend(b"\x1b[A" * (-dy))
                if dx > 0:
                    nav.extend(b"\x1b[C" * dx)
                elif dx < 0:
                    nav.extend(b"\x1b[D" * (-dx))
                proc.stdin.write(bytes(nav))
                await proc.stdin.drain()
                probe_curr_x, probe_curr_y = tx, ty
                await asyncio.sleep(0.04)
                owner = extract_owner_from_tui_text(
                    raw_buffer.decode("utf-8", errors="replace")
                )
                if owner is not None:
                    tui_occupied.add((tx, ty))

        print(f"[*] TUI probe detected {len(tui_occupied)} occupied cells.")
        final_occupied_cells = tui_occupied

    actual_stream, _actual_stats = optimize_drawing_stream(
        grid,
        origin_x=origin_x,
        origin_y=origin_y,
        initial_color=active_color,
        occupied_cells=final_occupied_cells,
    )

    print(
        f"[*] Resetting cursor to (0, 0) and navigating to ({origin_x}, {origin_y})..."
    )

    init_nav = (
        (b"\x1b[A" * 200)
        + (b"\x1b[D" * 400)
        + (b"\x1b[B" * origin_y)
        + (b"\x1b[C" * origin_x)
    )
    proc.stdin.write(init_nav)
    await proc.stdin.drain()
    await asyncio.sleep(0.1)
    print(f"[*] Streaming {len(actual_stream)} bytes of drawing operations...")
    tokens: list[bytes] = []
    idx_stream = 0
    while idx_stream < len(actual_stream):
        if actual_stream[idx_stream : idx_stream + 6] == b"\x1b[200~":
            end_marker = actual_stream.find(b"\x1b[201~", idx_stream + 6)
            if end_marker != -1:
                tokens.append(bytes(actual_stream[idx_stream : end_marker + 6]))
                idx_stream = end_marker + 6
                continue
        if actual_stream[idx_stream : idx_stream + 6] == b"\x1b[1;2C":
            tokens.append(bytes(actual_stream[idx_stream : idx_stream + 6]))
            idx_stream += 6
        elif actual_stream[idx_stream : idx_stream + 3] in (
            b"\x1b[A",
            b"\x1b[B",
            b"\x1b[C",
            b"\x1b[D",
        ):
            tokens.append(bytes(actual_stream[idx_stream : idx_stream + 3]))
            idx_stream += 3
        elif (
            actual_stream[idx_stream] == 0x0B
            and idx_stream + 8 <= len(actual_stream)
            and actual_stream[idx_stream + 7] == 0x0D
        ):
            tokens.append(bytes(actual_stream[idx_stream : idx_stream + 8]))
            idx_stream += 8
        else:
            tokens.append(bytes(actual_stream[idx_stream : idx_stream + 1]))
            idx_stream += 1

    current_chunk = bytearray()
    for tok in tokens:
        if len(current_chunk) + len(tok) > 1024:
            proc.stdin.write(bytes(current_chunk))
            await proc.stdin.drain()
            await asyncio.sleep(0.005)
            current_chunk = bytearray()
        current_chunk.extend(tok)
    if current_chunk:
        proc.stdin.write(bytes(current_chunk))
        await proc.stdin.drain()
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.2)

    print("[*] Drawing finished! Exiting edit mode (Esc)...")
    proc.stdin.write(b"\x1b")
    await asyncio.sleep(1.0)

    reader_task.cancel()
    proc.close()
    conn.close()
    print("[*] Done!")


def draw_art(
    image: str,
    remote: LateHost,
    origin: str = "10,10",
    dry_run: bool = False,
    symbol_map: SymbolMap | None = None,
    size: str | None = None,
    preview: bool = False,
    direct_color: bool = True,
    overlay: bool = False,
    debug: bool = False,
    output_bytes: str | None = None,
) -> None:
    """Core drawing workflow decoupled from CLI presentation."""

    try:
        origin_x, origin_y = parse_origin(origin)
    except ValueError as e:
        print(f"Error: {e}\n")
        raise typer.Exit(code=1)

    print("[*] Rendering image to grid...")
    grid = render_image_to_grid(image, size, symbol_map)
    height = len(grid)
    width = max(len(row) for row in grid) if height > 0 else 0
    print(f"[*] Parsed grid dimensions: {width} columns x {height} rows.")

    if origin_x + width > CANVAS_WIDTH or origin_y + height > CANVAS_HEIGHT:
        print(
            f"[!] Warning: Artwork extends past canvas bounds ({origin_x + width}x{origin_y + height} vs {CANVAS_WIDTH}x{CANVAS_HEIGHT})."
        )

    occupied_cells: set[tuple[int, int]] | None = None
    if overlay:
        print(
            f"[*] Overlay mode active: fetching existing artboard snapshot from {remote.host}..."
        )
        occupied_cells = fetch_artboard_occupied_cells(remote.host)
        print(
            f"[*] Found {len(occupied_cells)} occupied cells to preserve on artboard."
        )

    if debug:
        for y in range(height):
            row_str = f"{y:03d}"
            for i, ch in enumerate(row_str):
                if i < len(grid[y]):
                    grid[y][i].char = ch
                    grid[y][i].color = (255, 255, 255)
                    if hasattr(grid[y][i], "palette_idx"):
                        grid[y][i].palette_idx = 15  # Off-White

    if dry_run:
        title = "24-bit RGB Direct Hex"
        print(f"\n--- Mapped Artwork Preview ({title}) ---")
        print(generate_ansi_preview(grid))
        print("------------------------------------------------------\n")

    print("[*] Optimizing keystroke / byte drawing stream...")
    drawing_stream, stats = optimize_drawing_stream(
        grid,
        origin_x=origin_x,
        origin_y=origin_y,
        occupied_cells=occupied_cells,
    )

    print("[*] Drawing Statistics:")
    print(f"    - Characters typed:  {stats['chars_typed']}")
    print(f"    - Color cycle steps: {stats['color_changes']}")
    print(f"    - Navigation steps:  {stats['nav_moves']}")
    print(f"    - Total bytes:       {stats['total_bytes']}")

    if output_bytes:
        with open(output_bytes, "wb") as f:
            f.write(drawing_stream)
        print(f"[*] Byte stream saved to {output_bytes}.")

    if dry_run:
        print("[*] Dry run complete. No SSH connection attempted.")
        return

    asyncio.run(
        execute_ssh_drawing(
            grid=grid,
            origin_x=origin_x,
            origin_y=origin_y,
            remote=remote,
            overlay=overlay,
            occupied_cells=occupied_cells,
        )
    )


app = typer.Typer(
    name="artboard_painter",
    help="Automated / assisted drawing tool for late.sh communal artboard (Tab 4).",
    add_completion=False,
)


@app.command()
def main(
    image: str = typer.Argument(..., help="Path to input image file."),
    origin: str = typer.Option(
        "10,10",
        help="Target (x,y) starting coordinates on the 384x192 canvas (e.g. '10,10').",
    ),
    size: str | None = typer.Option(
        None, help="Set maximum image dimensions in columns and rows."
    ),
    symbols: str = typer.Option(
        "block,border",
        help="Specify character symbols to employ in final output.",
    ),
    dry_run: bool = typer.Option(
        False,
        help="Perform offline preview and keystroke generation without connecting to SSH.",
    ),
    preview: bool = typer.Option(
        False,
        help="Output the ANSI color-mapped preview to terminal stdout.",
    ),
    palette_mode: bool = typer.Option(
        False,
        help="Use legacy 16-color PAINT_PALETTE quantization instead of 24-bit arbitrary hex colors.",
    ),
    direct_color: bool = typer.Option(
        True,
        help="Use 24-bit arbitrary hex colors via Ctrl+K picker (default: True).",
    ),
    danger_mode: bool = typer.Option(
        False,
        help="Bypass production safety gate and allow running against live late.sh instance.",
    ),
    overlay: bool = typer.Option(
        False,
        help="Only draw into blank/unused cells without overwriting existing populated cells.",
    ),
    output_bytes: str | None = typer.Option(
        None,
        help="Optional file path to dump the raw byte sequence stream.",
    ),
    debug: bool = typer.Option(
        False,
        help="Render row numbers (e.g. 000, 001) in white at columns 0-2 for alignment tracking.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        help="SSH host of late.sh instance (test server only!).",
    ),
    port: int = typer.Option(
        2222,
        help="SSH port (default: 2222).",
    ),
    username: str = typer.Option(
        "drawer",
        help="SSH username for drawing session.",
    ),
    identity: Annotated[
        Path | None,
        typer.Option(envvar="LATE_SSH_IDENTITY", help="Path to SSH private key file."),
    ] = None,
) -> None:
    """Draw artwork onto late.sh artboard."""

    # STRICT PROD SAFETY RULE:
    # Reject connections to live production late.sh unless explicitly bypassed with --danger-mode
    remote = LateHost(host=host, port=port, username=username, identity_file=identity)
    if not dry_run and not danger_mode:
        lowered_host = remote.host.lower()
        if "late.sh" in lowered_host or lowered_host == "159.203.111.45":
            print(
                "[SAFETY ERROR] Direct execution against production late.sh is strictly forbidden!"
                "Only local / mock test instances are permitted (pass --danger-mode to override)."
            )
            raise typer.Exit(code=1)

    symbol_map = chafa.SymbolMap()
    symbol_map.apply_selectors(symbols)
    draw_art(
        image=image,
        remote=remote,
        origin=origin,
        dry_run=dry_run,
        size=size,
        symbol_map=symbol_map,
        preview=preview,
        direct_color=direct_color,
        overlay=overlay,
        debug=debug,
        output_bytes=output_bytes,
    )


if __name__ == "__main__":
    app()
