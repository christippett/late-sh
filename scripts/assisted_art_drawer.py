#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "asyncssh>=2.14.0",
# ]
# ///
"""
assisted_art_drawer.py - Automated / assisted drawing tool for late.sh communal artboard (Tab 4).

Takes an input image, renders via chafa, maps colors to late.sh's PAINT_PALETTE
with contrast preservation, and generates optimized keystroke streams or connects via SSH.
"""

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from typing import Dict, List, Optional, Set, Tuple

def char_display_width(ch: str) -> int:
    """Return display column width of a character on the terminal canvas (1 or 2)."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
# late-ssh/src/app/artboard/state.rs: PAINT_PALETTE (16 colors)
# Index 0..15
PAINT_PALETTE: List[Tuple[int, int, int]] = [
    (255, 110, 64),   # 0: Coral / Orange
    (255, 236, 96),   # 1: Bright Yellow (default)
    (255, 214, 102),  # 2: Golden Amber
    (145, 226, 88),   # 3: Lime Green
    (188, 255, 128),  # 4: Pastel Lime
    (72, 220, 170),   # 5: Mint / Teal
    (86, 245, 214),   # 6: Aqua Cyan
    (84, 196, 255),   # 7: Sky Blue
    (96, 225, 255),   # 8: Ice Blue
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
ANSI_16_COLORS: List[Tuple[int, int, int]] = [
    (0, 0, 0),        # 30: Black
    (205, 0, 0),      # 31: Red
    (0, 205, 0),      # 32: Green
    (205, 205, 0),    # 33: Yellow
    (0, 0, 238),      # 34: Blue
    (205, 0, 205),    # 35: Magenta
    (0, 205, 205),    # 36: Cyan
    (229, 229, 229),  # 37: White
    (127, 127, 127),  # 90: Bright Black (Gray)
    (255, 0, 0),      # 91: Bright Red
    (0, 255, 0),      # 92: Bright Green
    (255, 255, 0),    # 93: Bright Yellow
    (92, 92, 255),    # 94: Bright Blue
    (255, 0, 255),    # 95: Bright Magenta
    (0, 255, 255),    # 96: Bright Cyan
    (255, 255, 255),  # 97: Bright White
]


def rgb_to_cielab(rgb: Tuple[int, int, int]) -> Tuple[float, float, float]:
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
        return t ** (1.0 / 3.0) if t > delta**3 else (t / (3.0 * delta**2)) + (4.0 / 29.0)

    fx, fy, fz = f(x), f(y), f(z)
    l_star = 116.0 * fy - 16.0
    a_star = 500.0 * (fx - fy)
    b_star = 200.0 * (fy - fz)
    return l_star, a_star, b_star


def delta_e_cielab(lab1: Tuple[float, float, float], lab2: Tuple[float, float, float]) -> float:
    """Euclidean distance in CIELAB (CIE76 delta E)."""
    return math.sqrt(
        (lab1[0] - lab2[0]) ** 2 + (lab1[1] - lab2[1]) ** 2 + (lab1[2] - lab2[2]) ** 2
    )


# Precompute CIELAB values for PAINT_PALETTE
PALETTE_LAB: List[Tuple[float, float, float]] = [rgb_to_cielab(c) for c in PAINT_PALETTE]


class Cell:
    def __init__(self, char: str = " ", color: Optional[Tuple[int, int, int]] = None):
        self.char = char
        self.color = color  # (R, G, B) or None
        self.palette_idx: int = 1  # Default to yellow (index 1)


def parse_chafa_ansi(text: str) -> List[List[Cell]]:
    """
    Parse Chafa's terminal ANSI output into a 2D grid of Cell objects.
    Extracts character glyphs and 24-bit / 16-color ANSI foreground escapes.
    Strips cursor controls and background escapes.
    """
    lines = text.split("\n")
    grid: List[List[Cell]] = []

    # Matches any ANSI escape sequence: \x1b[ ... <final_char>
    escape_regex = re.compile(r"\x1b(\[[0-9;?]*[a-zA-Z]|\([B0-9]|\)[B0-9]|.)")
    sgr_regex = re.compile(r"^\x1b\[([0-9;]*)m$")

    for raw_line in lines:
        if not raw_line and len(grid) > 0 and raw_line == lines[-1]:
            continue  # trailing blank line

        row: List[Cell] = []
        pos = 0
        current_color: Optional[Tuple[int, int, int]] = None

        while pos < len(raw_line):
            if raw_line[pos] == "\x1b":
                esc_match = escape_regex.match(raw_line, pos)
                if esc_match:
                    full_esc = esc_match.group(0)
                    pos = esc_match.end()
                    sgr_m = sgr_regex.match(full_esc)
                    if sgr_m:
                        seq = sgr_m.group(1)
                        if not seq or seq == "0":
                            current_color = None
                        elif seq == "39":
                            current_color = None
                        else:
                            parts = [int(p) for p in seq.split(";") if p.isdigit()]
                            i = 0
                            while i < len(parts):
                                code = parts[i]
                                if code == 0:
                                    current_color = None
                                    i += 1
                                elif code == 39:
                                    current_color = None
                                    i += 1
                                elif code == 38:
                                    # Extended color: 38;2;R;G;B or 38;5;Idx
                                    if i + 1 < len(parts):
                                        mode = parts[i + 1]
                                        if mode == 2 and i + 4 < len(parts):  # 24-bit RGB
                                            current_color = (parts[i + 2], parts[i + 3], parts[i + 4])
                                            i += 5
                                        elif mode == 5 and i + 2 < len(parts):  # 256-color
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

    return grid


def run_chafa(image_path: str, chafa_flags: List[str]) -> str:
    """Execute chafa on image_path with provided flags, ensuring --fg-only and symbol format."""
    if not shutil.which("chafa"):
        raise RuntimeError("chafa binary not found in PATH. Install via `brew install chafa`.")

    flags = list(chafa_flags)
    # Check if format flag was passed (-f or --format)
    has_format = False
    i = 0
    while i < len(flags):
        f = flags[i]
        if f == "-f" or f.startswith("--format"):
            has_format = True
            break
        i += 1
    if not has_format:
        flags.append("--format=symbols")

    if not any(f.startswith("--fg-only") for f in flags):
        flags.append("--fg-only")

    cmd = ["chafa"] + flags + [image_path]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"chafa execution failed (code {res.returncode}): {res.stderr.strip()}")
    return res.stdout

def map_colors_with_contrast(grid: List[List[Cell]]) -> None:
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
    color_map: Dict[Tuple[int, int, int], int] = {}  # source_rgb -> palette_idx
    unique_colors: List[Tuple[int, int, int]] = []
    color_counts: Dict[Tuple[int, int, int], int] = {}

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
    adjacency: Dict[Tuple[int, int, int], Set[Tuple[int, int, int]]] = {
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
    assigned: Dict[Tuple[int, int, int], int] = {}

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


def generate_ansi_preview(grid: List[List[Cell]]) -> str:
    """Render the grid to an ANSI string using exact PAINT_PALETTE colors."""
    lines: List[str] = []
    current_idx = -1

    for row in grid:
        line_parts: List[str] = []
        for cell in row:
            if cell.char == " ":
                line_parts.append(" ")
                continue
            if cell.palette_idx != current_idx:
                current_idx = cell.palette_idx
                r, g, b = PAINT_PALETTE[current_idx]
                line_parts.append(f"\x1b[38;2;{r};{g};{b}m")
            line_parts.append(cell.char)
        line_parts.append("\x1b[0m")
        current_idx = -1
        lines.append("".join(line_parts))

    return "\n".join(lines)


def optimize_drawing_stream(
    grid: List[List[Cell]],
    origin_x: int = 0,
    origin_y: int = 0,
    initial_color_idx: int = 1,
) -> Tuple[bytes, Dict[str, int]]:
    """
    Generate the minimal byte sequence to draw grid onto late.sh Artboard.
    Tracks visual cursor column (accounting for wide characters) and active color state.
    Color cycling:
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
    curr_color = initial_color_idx

    def move_to(target_x: int, target_y: int):
        nonlocal curr_x, curr_y
        dy = target_y - curr_y
        dx = target_x - curr_x

        if dy > 0:
            stream.extend(b"\x1b[B" * dy)
            stats["nav_moves"] += dy
        elif dy < 0:
            stream.extend(b"\x1b[A" * (-dy))
            stats["nav_moves"] += (-dy)

        if dx > 0:
            stream.extend(b"\x1b[C" * dx)
            stats["nav_moves"] += dx
        elif dx < 0:
            stream.extend(b"\x1b[D" * (-dx))
            stats["nav_moves"] += (-dx)

        curr_x = target_x
        curr_y = target_y

    def set_color(target_color: int):
        nonlocal curr_color
        if curr_color == target_color:
            return
        delta = (target_color - curr_color) % 16
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
        curr_color = target_color

    height = len(grid)
    for y in range(height):
        row = grid[y]
        col = 0
        idx = 0
        while idx < len(row):
            cell = row[idx]
            w = char_display_width(cell.char)
            if cell.char == " ":
                col += w
                idx += 1
                continue

            # Find contiguous run of cells on the same row with the same palette_idx
            start_col = col
            target_pal = cell.palette_idx
            run_chars = []
            run_visual_width = 0

            while idx < len(row):
                c = row[idx]
                cw = char_display_width(c.char)
                if c.char == " " or c.palette_idx != target_pal:
                    break
                run_chars.append(c.char)
                run_visual_width += cw
                col += cw
                idx += 1

            tx = origin_x + start_col
            ty = origin_y + y

            if curr_x != tx or curr_y != ty:
                move_to(tx, ty)

            set_color(target_pal)

            # Emit bracketed paste for run: \x1b[200~<text>\x1b[201~
            run_text = "".join(run_chars)
            run_bytes = run_text.encode("utf-8")
            stream.extend(b"\x1b[200~" + run_bytes + b"\x1b[201~")
            stats["chars_typed"] += len(run_chars)
            curr_x += run_visual_width

    stats["total_bytes"] = len(stream)
    return bytes(stream), stats

def parse_args():
    parser = argparse.ArgumentParser(
        description="Assisted art drawer for late.sh communal artboard (Tab 4)."
    )
    parser.add_argument("--image", required=True, help="Path to input image file.")
    parser.add_argument(
        "--chafa-args",
        default="--symbols=block+border --size=40x20",
        help="Extra flags to pass to chafa (e.g. '--symbols=ascii --size=60x30').",
    )
    parser.add_argument(
        "--origin",
        default="10,10",
        help="Target (x,y) starting coordinates on the 384x192 canvas (e.g. '10,10').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform offline preview and keystroke generation without connecting to SSH.",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Output the ANSI color-mapped preview to terminal stdout.",
    )
    parser.add_argument(
        "--output-bytes",
        help="Optional file path to dump the raw byte sequence stream.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="SSH host of late.sh instance (test server only!).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=2222,
        help="SSH port (default: 2222).",
    )
    parser.add_argument(
        "--username",
        default="drawer",
        help="SSH username for drawing session.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # STRICT PROD SAFETY RULE:
    # Reject connections to live production late.sh
    if not args.dry_run:
        lowered_host = args.host.lower()
        if "late.sh" in lowered_host or lowered_host == "159.203.111.45":
            sys.stderr.write(
                "\n[SAFETY ERROR] Direct execution against production late.sh is strictly forbidden!\n"
                "Only local / mock test instances are permitted.\n"
            )
            sys.exit(1)

    coords = [int(v.strip()) for v in args.origin.split(",")]
    origin_x, origin_y = coords[0], coords[1]
    if origin_x < 0 or origin_x >= CANVAS_WIDTH or origin_y < 0 or origin_y >= CANVAS_HEIGHT:
        sys.stderr.write(
            f"Error: origin ({origin_x}, {origin_y}) out of bounds (canvas is {CANVAS_WIDTH}x{CANVAS_HEIGHT}).\n"
        )
        sys.exit(1)

    import shlex

    chafa_flags = shlex.split(args.chafa_args)

    print(f"[*] Running chafa on {args.image} with flags: {chafa_flags}...")
    chafa_output = run_chafa(args.image, chafa_flags)

    print("[*] Parsing ANSI grid...")
    grid = parse_chafa_ansi(chafa_output)
    height = len(grid)
    width = max(len(row) for row in grid) if height > 0 else 0
    print(f"[*] Parsed grid dimensions: {width} columns x {height} rows.")

    if origin_x + width > CANVAS_WIDTH or origin_y + height > CANVAS_HEIGHT:
        print(
            f"[!] Warning: Artwork extends past canvas bounds ({origin_x + width}x{origin_y + height} vs {CANVAS_WIDTH}x{CANVAS_HEIGHT})."
        )

    print("[*] Performing contrast-preserving color quantization to PAINT_PALETTE...")
    map_colors_with_contrast(grid)

    if args.preview or args.dry_run:
        print("\n--- Mapped Artwork Preview (16-color PAINT_PALETTE) ---")
        print(generate_ansi_preview(grid))
        print("------------------------------------------------------\n")

    print("[*] Optimizing keystroke / byte drawing stream...")
    drawing_stream, stats = optimize_drawing_stream(grid, origin_x, origin_y)

    print("[*] Drawing Statistics:")
    print(f"    - Characters typed:  {stats['chars_typed']}")
    print(f"    - Color cycle steps: {stats['color_changes']}")
    print(f"    - Navigation steps:  {stats['nav_moves']}")
    print(f"    - Total bytes:       {stats['total_bytes']}")

    if args.output_bytes:
        with open(args.output_bytes, "wb") as f:
            f.write(drawing_stream)
        print(f"[*] Byte stream saved to {args.output_bytes}.")

    if args.dry_run:
        print("[*] Dry run complete. No SSH connection attempted.")
        return

    # SSH Drawing Execution (targeted to local dev / mock server)
    import asyncio
    try:
        import asyncssh
    except ImportError:
        sys.stderr.write("Error: asyncssh not installed. Run via `uv run`.\n")
        sys.exit(1)

    async def run_ssh_drawer():
        print(f"[*] Connecting to {args.username}@{args.host}:{args.port}...")
        conn = await asyncssh.connect(
            args.host,
            port=args.port,
            username=args.username,
            known_hosts=None,
            encoding=None,
        )
        proc = await conn.create_process(
            term_type="xterm-256color",
            term_size=(100, 30),
            encoding=None,
        )
        print("[*] SSH connection established.")

        # Buffer incoming PTY text
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
        await asyncio.sleep(1.0)

        # Navigate to Screen::Artboard (Tab 4)
        print("[*] Navigating to Screen::Artboard (Tab 4)...")
        proc.stdin.write(b"4")
        await asyncio.sleep(1.5)

        # Enter interactive edit mode ('i')
        print("[*] Entering interactive edit mode ('i')...")
        proc.stdin.write(b"i")
        await asyncio.sleep(1.0)

        # Read current active color from Artboard Info overlay
        def detect_active_color_idx() -> int:
            text = raw_buffer.decode("utf-8", errors="replace")
            # Search for Color #RRGGBB
            m = re.findall(r"Color\s+(#[0-9A-Fa-f]{6})", text)
            if m:
                active_hex = m[-1].upper()
                for idx, pal_rgb in enumerate(PAINT_PALETTE):
                    pal_hex = ("#%02X%02X%02X" % pal_rgb).upper()
                    if pal_hex == active_hex:
                        return idx
            return 1  # Default fallback

        active_color_idx = detect_active_color_idx()
        print(f"[*] Detected active paint color index: {active_color_idx} ({PAINT_PALETTE[active_color_idx]}).")

        # Re-optimize drawing stream based on actual active color
        actual_stream, actual_stats = optimize_drawing_stream(
            grid, origin_x=origin_x, origin_y=origin_y, initial_color_idx=active_color_idx
        )

        # Navigate to (origin_x, origin_y):
        # First clamp cursor to (0, 0) using 200 Up + 400 Left arrows
        print(f"[*] Resetting cursor to (0, 0) and navigating to ({origin_x}, {origin_y})...")
        init_nav = (b"\x1b[A" * 200) + (b"\x1b[D" * 400) + (b"\x1b[B" * origin_y) + (b"\x1b[C" * origin_x)
        proc.stdin.write(init_nav)
        await asyncio.sleep(1.0)

        # Stream drawing bytes in small chunks at ~50 Hz
        print(f"[*] Streaming {len(actual_stream)} bytes of drawing operations...")
        chunk_size = 32
        for i in range(0, len(actual_stream), chunk_size):
            chunk = actual_stream[i : i + chunk_size]
            proc.stdin.write(chunk)
            await asyncio.sleep(0.02)

        await asyncio.sleep(1.0)

        # Exit edit mode with Esc
        print("[*] Drawing finished! Exiting edit mode (Esc)...")
        proc.stdin.write(b"\x1b")
        await asyncio.sleep(1.0)

        reader_task.cancel()
        proc.close()
        conn.close()
        print("[*] Done!")

    asyncio.run(run_ssh_drawer())

if __name__ == "__main__":
    main()
