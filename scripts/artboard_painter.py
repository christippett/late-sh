#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "asyncssh>=2.14.0",
#     "typer>=0.9.0",
#     "shellingham",
# ]
# ///
"""
artboard_painter.py - Automated / assisted drawing tool for late.sh communal artboard (Tab 4).

Takes an input image, renders via chafa, maps colors to late.sh's PAINT_PALETTE
with contrast preservation, and generates optimized keystroke streams or connects via SSH.
"""
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
import urllib.request

import typer

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
        except Exception:
            continue
    return set()


def run_chafa(image_path: str, chafa_flags: list[str]) -> str:
    """Execute chafa on image_path with provided flags, ensuring --fg-only and symbol format."""
    if not shutil.which("chafa"):
        raise RuntimeError(
            "chafa binary not found in PATH. Install via `brew install chafa`."
        )

    flags = list(chafa_flags)
    # Check if format flag was passed (-f or --format)
    has_format = any(f == "-f" or f.startswith("--format") for f in flags)
    if not has_format:
        flags.append("--format=symbols")

    if not any(f.startswith("--fg-only") for f in flags):
        flags.append("--fg-only")

    # Decouple from calling terminal dimensions: use full canvas dimensions
    if not any(f.startswith("--view-size") for f in flags):
        flags.append(f"--view-size={CANVAS_WIDTH}x{CANVAS_HEIGHT}")
    if not any(f.startswith("--margin-bottom") for f in flags):
        flags.append("--margin-bottom=0")
    if not any(f.startswith("--margin-right") for f in flags):
        flags.append("--margin-right=0")
    if not any(f == "-s" or f.startswith("--size") for f in flags):
        flags.append(f"--size={CANVAS_WIDTH}x{CANVAS_HEIGHT}")
    cmd = ["chafa"] + flags + [image_path]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"chafa execution failed (code {res.returncode}): {res.stderr.strip()}"
        )
    return res.stdout


def map_colors_with_contrast(grid: list[list[Cell]]) -> None:
    """
    Contrast-preserving color mapping:
    1. Collects unique colors and builds an adjacency graph between distinct adjacent colors.
    2. Maps colors to PAINT_PALETTE indices (0-15) such that:
       - Visual similarity to source color is high (low CIELAB delta).
       - Adjacent distinct colors avoid collapsing to the same or indistinguishable palette colors.
    """
    height = len(grid)
    if height == 0:
        return
    width = max(len(row) for row in grid)

    # Gather unique non-empty colors
    color_map: dict[tuple[int, int, int], int] = {}  # source_rgb -> palette_idx
    unique_colors: list[tuple[int, int, int]] = []
    color_counts: dict[tuple[int, int, int], int] = {}

    for y in range(height):
        for x in range(len(grid[y])):
            cell = grid[y][x]
            if cell.char != " " and cell.color is not None:
                c = cell.color
                color_counts[c] = color_counts.get(c, 0) + 1
                if c not in color_map:
                    color_map[c] = -1
                    unique_colors.append(c)

    if not unique_colors:
        return

    # Build adjacency graph between colors that share adjacent cells
    adjacency: dict[tuple[int, int, int], set[tuple[int, int, int]]] = {
        c: set() for c in unique_colors
    }

    for y in range(height):
        row = grid[y]
        for x in range(len(row)):
            c1 = row[x].color
            if c1 is None or row[x].char == " ":
                continue
            # Check neighbors: right and down
            for dx, dy in ((1, 0), (0, 1)):
                nx, ny = x + dx, y + dy
                if ny < height and nx < len(grid[ny]):
                    c2 = grid[ny][nx].color
                    if c2 is not None and grid[ny][nx].char != " " and c1 != c2:
                        adjacency[c1].add(c2)
                        adjacency[c2].add(c1)

    # Sort colors by frequency descending so prominent colors get primary placement
    unique_colors.sort(key=lambda c: color_counts[c], reverse=True)

    # Optimization / Assignment
    # If number of unique colors <= 16, we can assign distinct palette indices
    # Otherwise, we cluster or assign with contrast penalty
    assigned: dict[tuple[int, int, int], int] = {}

    for c in unique_colors:
        c_lab = rgb_to_cielab(c)
        neighbors = adjacency[c]
        neighbor_pal_indices = {assigned[n] for n in neighbors if n in assigned}

        best_idx = 1
        best_score = float("inf")

        for pal_idx in range(len(PAINT_PALETTE)):
            pal_lab = PALETTE_LAB[pal_idx]
            dist_to_src = delta_e_cielab(c_lab, pal_lab)

            # Contrast penalty for adjacent colors
            conflict_penalty = 0.0
            if pal_idx in neighbor_pal_indices:
                # Big penalty for identical color assignment to adjacent distinct region
                conflict_penalty += 200.0
            else:
                for n_idx in neighbor_pal_indices:
                    n_lab = PALETTE_LAB[n_idx]
                    pal_dist = delta_e_cielab(pal_lab, n_lab)
                    if pal_dist < 20.0:  # Perceptually too close
                        conflict_penalty += (20.0 - pal_dist) * 5.0

            # Favor index 1 if close to neutral
            total_score = dist_to_src + conflict_penalty
            if total_score < best_score:
                best_score = total_score
                best_idx = pal_idx

        assigned[c] = best_idx

    # Apply assigned palette indices back to the grid
    for y in range(height):
        for x in range(len(grid[y])):
            cell = grid[y][x]
            if cell.color is not None and cell.color in assigned:
                cell.palette_idx = assigned[cell.color]
            else:
                cell.palette_idx = 1  # Default


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
    palette_mode: bool = False,
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
    if palette_mode:
        curr_palette_idx = 1
        if initial_color is not None:
            for idx, pal_rgb in enumerate(PAINT_PALETTE):
                if pal_rgb == initial_color:
                    curr_palette_idx = idx
                    break
        curr_rgb_color: tuple[int, int, int] | None = None
    else:
        curr_rgb_color = (
            initial_color if initial_color is not None else PAINT_PALETTE[1]
        )
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
        hex_str = "%02X%02X%02X" % rgb
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

            if palette_mode:
                target_pal = cell.palette_idx
                while idx < len(row):
                    c = row[idx]
                    cw = char_display_width(c.char)
                    ctx = origin_x + col
                    c_occ = occupied_cells is not None and any(
                        (ctx + dx, ty) in occupied_cells for dx in range(cw)
                    )
                    if c.char == " " or c_occ or c.palette_idx != target_pal:
                        break
                    run_chars.append(c.char)
                    run_visual_width += cw
                    col += cw
                    idx += 1
            else:
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
                    c_rgb = (
                        c.color if c.color is not None else PAINT_PALETTE[c.palette_idx]
                    )
                    if c.char == " " or c_occ or c_rgb != target_rgb:
                        break
                    run_chars.append(c.char)
                    run_visual_width += cw
                    col += cw
                    idx += 1
            tx = origin_x + start_col
            ty = origin_y + y

            if curr_x != tx or curr_y != ty:
                move_to(tx, ty)

            if palette_mode:
                set_palette_color(target_pal)
            else:
                set_hex_color(target_rgb)
            # Emit bracketed paste for run: \x1b[200~<text>\x1b[201~
            run_text = "".join(run_chars)
            run_bytes = run_text.encode("utf-8")
            stream.extend(b"\x1b[200~" + run_bytes + b"\x1b[201~")
            stats["chars_typed"] += len(run_chars)
            curr_x += run_visual_width

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
    image_path: str,
    chafa_args: str | list[str] = "--symbols=block+border --size=40x20",
    palette_mode: bool = False,
) -> list[list[Cell]]:
    """
    Render an image file via chafa into an ANSI cell grid, mapping colors as requested.
    """
    if isinstance(chafa_args, str):
        chafa_flags = shlex.split(chafa_args)
    else:
        chafa_flags = list(chafa_args)

    chafa_output = run_chafa(image_path, chafa_flags)
    grid = parse_chafa_ansi(chafa_output)
    if palette_mode:
        map_colors_with_contrast(grid)
    return grid


def prepare_drawing_stream(
    image_path: str,
    origin_x: int = 10,
    origin_y: int = 10,
    chafa_args: str | list[str] = "--symbols=block+border --size=40x20",
    palette_mode: bool = False,
    occupied_cells: set[tuple[int, int]] | None = None,
    initial_color: tuple[int, int, int] | None = None,
) -> tuple[list[list[Cell]], bytes, dict[str, int]]:
    """
    Process image and generate optimized drawing stream with stats.
    Returns (grid, drawing_stream, stats).
    """
    grid = render_image_to_grid(image_path, chafa_args=chafa_args, palette_mode=palette_mode)
    drawing_stream, stats = optimize_drawing_stream(
        grid,
        origin_x=origin_x,
        origin_y=origin_y,
        initial_color=initial_color,
        palette_mode=palette_mode,
        occupied_cells=occupied_cells,
    )
    return grid, drawing_stream, stats


async def execute_ssh_drawing(
    grid: list[list[Cell]],
    origin_x: int,
    origin_y: int,
    host: str = "127.0.0.1",
    port: int = 2222,
    username: str = "drawer",
    identity: str | None = None,
    palette_mode: bool = False,
    overlay: bool = False,
    occupied_cells: set[tuple[int, int]] | None = None,
) -> None:
    """Connect to late.sh via SSH and stream the drawing commands."""
    import asyncio

    try:
        import asyncssh
    except ImportError:
        sys.stderr.write("Error: asyncssh not installed. Run via `uv run`.\n")
        sys.exit(1)

    height = len(grid)
    print(f"[*] Connecting to {username}@{host}:{port}...")
    client_keys = None
    agent_path = ()  # Default: use environment SSH_AUTH_SOCK

    candidate_key = identity or os.path.expanduser("~/.ssh/id_late_sh_ed25519")
    expanded_key = os.path.expanduser(candidate_key)
    if os.path.exists(expanded_key):
        try:
            client_keys = [asyncssh.read_private_key(expanded_key)]
            agent_path = None
        except Exception:
            if identity:
                raise
    conn = await asyncssh.connect(
        host,
        port=port,
        username=username,
        client_keys=client_keys,
        agent_path=agent_path,
        known_hosts=None,
        encoding=None,
    )
    proc = await conn.create_process(
        term_type="xterm-256color",
        term_size=(100, 30),
        encoding=None,
    )
    print("[*] SSH connection established.")

    raw_buffer = bytearray()

    async def reader():
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                raw_buffer.extend(chunk)
        except Exception:
            pass

    reader_task = asyncio.create_task(reader())

    # Wait for splash screen to initialize
    await asyncio.sleep(2.0)

    # Dismiss splash screen with Esc
    print("[*] Dismissing splash screen (Esc)...")
    proc.stdin.write(b"\x1b")
    await proc.stdin.drain()
    await asyncio.sleep(1.0)

    # Navigate to Screen::Artboard (Tab 4)
    print("[*] Navigating to Screen::Artboard (Tab 4)...")
    proc.stdin.write(b"4")
    await proc.stdin.drain()
    await asyncio.sleep(1.5)

    # Enter interactive edit mode ('i')
    print("[*] Entering interactive edit mode ('i')...")
    proc.stdin.write(b"i")
    await proc.stdin.drain()
    await asyncio.sleep(1.0)

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

    actual_stream, actual_stats = optimize_drawing_stream(
        grid,
        origin_x=origin_x,
        origin_y=origin_y,
        initial_color=active_color,
        palette_mode=palette_mode,
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
    await asyncio.sleep(1.0)

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
            await asyncio.sleep(0.02)
            current_chunk = bytearray()
        current_chunk.extend(tok)
    if current_chunk:
        proc.stdin.write(bytes(current_chunk))
        await proc.stdin.drain()
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.5)

    print("[*] Drawing finished! Exiting edit mode (Esc)...")
    proc.stdin.write(b"\x1b")
    await asyncio.sleep(1.0)

    reader_task.cancel()
    proc.close()
    conn.close()
    print("[*] Done!")


def draw_art(
    image: str,
    chafa_args: str = "--symbols=block+border --size=40x20",
    origin: str = "10,10",
    dry_run: bool = False,
    preview: bool = False,
    palette_mode: bool = False,
    direct_color: bool = True,
    danger_mode: bool = False,
    overlay: bool = False,
    output_bytes: str | None = None,
    host: str = "127.0.0.1",
    port: int = 2222,
    username: str = "drawer",
    identity: str | None = None,
) -> None:
    """Core drawing workflow decoupled from CLI presentation."""
    # STRICT PROD SAFETY RULE:
    # Reject connections to live production late.sh unless explicitly bypassed with --danger-mode
    if not dry_run and not danger_mode:
        lowered_host = host.lower()
        if "late.sh" in lowered_host or lowered_host == "159.203.111.45":
            sys.stderr.write(
                "\n[SAFETY ERROR] Direct execution against production late.sh is strictly forbidden!\n"
                "Only local / mock test instances are permitted (pass --danger-mode to override).\n"
            )
            sys.exit(1)

    try:
        origin_x, origin_y = parse_origin(origin)
    except ValueError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)

    parsed_chafa_flags = shlex.split(chafa_args)
    print(f"[*] Running chafa on {image} with flags: {parsed_chafa_flags}...")
    chafa_output = run_chafa(image, parsed_chafa_flags)

    print("[*] Parsing ANSI grid...")
    grid = parse_chafa_ansi(chafa_output)
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
            f"[*] Overlay mode active: fetching existing artboard snapshot from {host}..."
        )
        occupied_cells = fetch_artboard_occupied_cells(host)
        print(
            f"[*] Found {len(occupied_cells)} occupied cells to preserve on artboard."
        )

    if palette_mode:
        print(
            "[*] Performing contrast-preserving color quantization to 16-color PAINT_PALETTE..."
        )
        map_colors_with_contrast(grid)
    else:
        print(
            "[*] Using direct 24-bit RGB colors with Artboard hex color picker (Ctrl+K)..."
        )

    if preview or dry_run:
        title = "16-color PAINT_PALETTE" if palette_mode else "24-bit RGB Direct Hex"
        print(f"\n--- Mapped Artwork Preview ({title}) ---")
        print(generate_ansi_preview(grid, palette_mode=palette_mode))
        print("------------------------------------------------------\n")

    print("[*] Optimizing keystroke / byte drawing stream...")
    drawing_stream, stats = optimize_drawing_stream(
        grid,
        origin_x=origin_x,
        origin_y=origin_y,
        palette_mode=palette_mode,
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

    import asyncio

    asyncio.run(
        execute_ssh_drawing(
            grid=grid,
            origin_x=origin_x,
            origin_y=origin_y,
            host=host,
            port=port,
            username=username,
            identity=identity,
            palette_mode=palette_mode,
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
    image: str = typer.Option(..., "--image", help="Path to input image file."),
    chafa_args: str = typer.Option(
        "--symbols=block+border --size=40x20",
        "--chafa-args",
        help="Extra flags to pass to chafa (e.g. '--symbols=ascii --size=60x30').",
    ),
    origin: str = typer.Option(
        "10,10",
        "--origin",
        help="Target (x,y) starting coordinates on the 384x192 canvas (e.g. '10,10').",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Perform offline preview and keystroke generation without connecting to SSH.",
    ),
    preview: bool = typer.Option(
        False,
        "--preview",
        help="Output the ANSI color-mapped preview to terminal stdout.",
    ),
    palette_mode: bool = typer.Option(
        False,
        "--palette-mode",
        help="Use legacy 16-color PAINT_PALETTE quantization instead of 24-bit arbitrary hex colors.",
    ),
    direct_color: bool = typer.Option(
        True,
        "--direct-color/--no-direct-color",
        help="Use 24-bit arbitrary hex colors via Ctrl+K picker (default: True).",
    ),
    danger_mode: bool = typer.Option(
        False,
        "--danger-mode",
        help="Bypass production safety gate and allow running against live late.sh instance.",
    ),
    overlay: bool = typer.Option(
        False,
        "--overlay",
        help="Only draw into blank/unused cells without overwriting existing populated cells.",
    ),
    output_bytes: str | None = typer.Option(
        None,
        "--output-bytes",
        help="Optional file path to dump the raw byte sequence stream.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="SSH host of late.sh instance (test server only!).",
    ),
    port: int = typer.Option(
        2222,
        "--port",
        help="SSH port (default: 2222).",
    ),
    username: str = typer.Option(
        "drawer",
        "--username",
        help="SSH username for drawing session.",
    ),
    identity: str | None = typer.Option(
        None,
        "--identity",
        help="Path to SSH private key file (e.g. ~/.ssh/id_late_sh_ed25519).",
    ),
) -> None:
    """Draw artwork onto late.sh artboard."""
    draw_art(
        image=image,
        chafa_args=chafa_args,
        origin=origin,
        dry_run=dry_run,
        preview=preview,
        palette_mode=palette_mode,
        direct_color=direct_color,
        danger_mode=danger_mode,
        overlay=overlay,
        output_bytes=output_bytes,
        host=host,
        port=port,
        username=username,
        identity=identity,
    )


if __name__ == "__main__":
    app()
