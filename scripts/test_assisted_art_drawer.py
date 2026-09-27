#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pytest>=7.0.0",
#     "asyncssh>=2.14.0",
#     "typer>=0.9.0",
# ]
# ///
"""
Tests for scripts/artboard_painter.py:
1. CIELAB color conversion and CIE76 Delta-E distance metrics.
2. Chafa ANSI parser (24-bit SGR, 16-color ANSI, extended 256-color cube, control stripping).
3. Contrast-preserving color mapping to PAINT_PALETTE.
4. Keystroke optimization, wide-character display tracking, and virtual artboard canvas simulation.
5. Production safety gate preventing connections to production late.sh.
6. Chafa flags preservation (-c 16, -f symbols, etc.).
"""

import math
import os
import pathlib
import sys
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.artboard_painter import (
    Cell,
    PAINT_PALETTE,
    CANVAS_WIDTH,
    CANVAS_HEIGHT,
    char_display_width,
    rgb_to_cielab,
    delta_e_cielab,
    parse_chafa_ansi,
    map_colors_with_contrast,
    generate_ansi_preview,
    optimize_drawing_stream,
    run_chafa,
)


def test_rgb_to_cielab_and_delta_e():
    # Pure white (255, 255, 255) -> L* should be ~100, a* ~ 0, b* ~ 0
    l_w, a_w, b_w = rgb_to_cielab((255, 255, 255))
    assert abs(l_w - 100.0) < 0.5
    assert abs(a_w) < 0.5
    assert abs(b_w) < 0.5

    # Pure black (0, 0, 0) -> L* should be ~0, a* ~ 0, b* ~ 0
    l_k, a_k, b_k = rgb_to_cielab((0, 0, 0))
    assert abs(l_k) < 0.1
    assert abs(a_k) < 0.1
    assert abs(b_k) < 0.1

    # Distance between white and black should be approx 100
    dist_wk = delta_e_cielab((l_w, a_w, b_w), (l_k, a_k, b_k))
    assert abs(dist_wk - 100.0) < 1.0

    # Self distance is 0
    assert delta_e_cielab((l_w, a_w, b_w), (l_w, a_w, b_w)) == 0.0


def test_char_display_width():
    assert char_display_width("A") == 1
    assert char_display_width(" ") == 1
    assert char_display_width("█") == 1  # block characters
    assert char_display_width("こ") == 2  # hiragana
    assert char_display_width("し") == 2
    assert char_display_width("エ") == 2  # katakana


def test_ansi_parser_24bit_and_control_stripping():
    # Line with 24-bit colors, cursor hide/show, default fg resets
    ansi_input = (
        "\x1b[?25l"  # cursor hide
        "\x1b[38;2;255;110;64mR"  # coral
        "\x1b[38;2;84;196;255mB"  # sky blue
        "\x1b[39m "  # default color space
        "\x1b[38;2;145;226;88mG"  # lime green
        "\x1b[0m\x1b[?25h"  # reset & cursor show
    )
    grid = parse_chafa_ansi(ansi_input)
    assert len(grid) == 1
    row = grid[0]
    assert len(row) == 4
    assert row[0].char == "R"
    assert row[0].color == (255, 110, 64)
    assert row[1].char == "B"
    assert row[1].color == (84, 196, 255)
    assert row[2].char == " "
    assert row[2].color is None
    assert row[3].char == "G"
    assert row[3].color == (145, 226, 88)


def test_ansi_parser_16_color_and_256_color():
    # SGR 31 (Red, ANSI 16), SGR 38;5;11 (Yellow, ANSI 16 bright)
    ansi_input = "\x1b[31mX\x1b[38;5;11mY\x1b[0m"
    grid = parse_chafa_ansi(ansi_input)
    assert len(grid) == 1
    row = grid[0]
    assert len(row) == 2
    assert row[0].char == "X"
    assert row[0].color == (205, 0, 0)  # ANSI 16 red
    assert row[1].char == "Y"
    assert row[1].color == (255, 255, 0)  # ANSI 16 bright yellow


def test_contrast_preserving_color_mapping():
    # Two adjacent cells with distinct red and pink source colors
    c_red = Cell("█", (255, 0, 0))
    c_pink = Cell("█", (255, 120, 180))
    grid = [[c_red, c_pink]]

    map_colors_with_contrast(grid)

    idx_red = grid[0][0].palette_idx
    idx_pink = grid[0][1].palette_idx

    assert 0 <= idx_red < 16
    assert 0 <= idx_pink < 16
    # Crucial contrast invariant: Adjacent distinct colors must not collapse into identical palette color
    assert idx_red != idx_pink

    # Check CIELAB distance between the assigned palette colors
    l1, a1, b1 = rgb_to_cielab(PAINT_PALETTE[idx_red])
    l2, a2, b2 = rgb_to_cielab(PAINT_PALETTE[idx_pink])
    contrast_dist = delta_e_cielab((l1, a1, b1), (l2, a2, b2))
    assert contrast_dist > 15.0  # Visually distinguishable


def test_ansi_preview_generation():
    cell_a = Cell("A", (255, 110, 64))
    cell_a.palette_idx = 0
    cell_b = Cell("B", (84, 196, 255))
    cell_b.palette_idx = 7
    grid = [[cell_a, cell_b]]

    # Direct 24-bit RGB preview
    preview_direct = generate_ansi_preview(grid, palette_mode=False)
    assert "\x1b[38;2;255;110;64mA" in preview_direct
    assert "\x1b[38;2;84;196;255mB" in preview_direct

    # Legacy palette preview
    preview_palette = generate_ansi_preview(grid, palette_mode=True)
    assert "\x1b[38;2;255;110;64mA" in preview_palette
    assert "\x1b[38;2;84;196;255mB" in preview_palette

def test_drawing_stream_simulation_with_wide_characters():
    # Simulate execution on a virtual artboard canvas with narrow and wide characters
    c1 = Cell("こ", (255, 110, 64))  # width 2
    c1.palette_idx = 0
    c2 = Cell("あ", (255, 236, 96))  # width 2
    c2.palette_idx = 1
    c3 = Cell(" ", None)
    c4 = Cell("!", (224, 116, 255))  # width 1
    c4.palette_idx = 12

    grid = [
        [c1, c2],
        [c3, c4],
    ]

    origin_x, origin_y = 10, 10
    stream, stats = optimize_drawing_stream(grid, origin_x=origin_x, origin_y=origin_y, palette_mode=True)

    assert stats["chars_typed"] == 3
    assert stats["color_changes"] >= 1
    assert stats["total_bytes"] == len(stream)

    # Virtual artboard interpreter
    canvas = {}
    cx = origin_x
    cy = origin_y
    color = 1  # default initial color in state.rs is index 1

    i = 0
    b = stream
    selection_anchor = None
    while i < len(b):
        if b[i : i + 6] == b"\x1b[1;2C":  # Shift+Right
            if selection_anchor is None:
                selection_anchor = (cx, cy)
            cx += 1
            i += 6
        elif b[i : i + 3] == b"\x1b[A":  # Up
            cy -= 1
            selection_anchor = None
            i += 3
        elif b[i : i + 3] == b"\x1b[B":  # Down
            cy += 1
            selection_anchor = None
            i += 3
        elif b[i : i + 3] == b"\x1b[C":  # Right
            cx += 1
            selection_anchor = None
            i += 3
        elif b[i : i + 3] == b"\x1b[D":  # Left
            cx -= 1
            selection_anchor = None
            i += 3
        elif b[i] == 0x7F:  # Backspace
            if selection_anchor is not None:
                # Clear selected cells and reset cursor to anchor
                ax, ay = selection_anchor
                min_x, max_x = min(ax, cx), max(ax, cx)
                min_y, max_y = min(ay, cy), max(ay, cy)
                for sy in range(min_y, max_y + 1):
                    for sx in range(min_x, max_x + 1):
                        canvas.pop((sx, sy), None)
                cx, cy = ax, ay
                selection_anchor = None
            i += 1
        elif b[i] == 0x19:  # Ctrl+Y (next color)
            color = (color + 1) % 16
            i += 1
        elif b[i] == 0x15:  # Ctrl+U (prev color)
            color = (color - 1) % 16
            i += 1
        elif b[i : i + 6] == b"\x1b[200~":
            # Bracketed paste start
            i += 6
            paste_end = b.find(b"\x1b[201~", i)
            assert paste_end != -1
            paste_text = b[i:paste_end].decode("utf-8")
            for ch in paste_text:
                w = char_display_width(ch)
                canvas[(cx, cy)] = (ch, color)
                cx += w
            i = paste_end + 6
        else:
            first = b[i]
            if first < 0x80:
                ch_len = 1
            elif (first & 0xE0) == 0xC0:
                ch_len = 2
            elif (first & 0xF0) == 0xE0:
                ch_len = 3
            else:
                ch_len = 4
            ch = b[i : i + ch_len].decode("utf-8")
            w = char_display_width(ch)
            canvas[(cx, cy)] = (ch, color)
            cx += w
            i += ch_len

    # Assert exact visual placement accounting for wide characters:
    # c1 ("こ", width 2) starts at 10 -> occupies 10, 11
    # c2 ("あ", width 2) starts at 12 -> occupies 12, 13
    assert canvas[(10, 10)] == ("こ", 0)
    assert canvas[(12, 10)] == ("あ", 1)
    # Row 11:
    # c3 is space at visual col 10 (width 1)
    # c4 ("!", width 1) is at visual col 11
    assert (10, 11) not in canvas
    assert canvas[(11, 11)] == ("!", 12)

def test_chafa_view_size_and_full_output_parsing():
    # Test parsing ANSI output with trailing cursor controls and blank lines
    raw_ansi = (
        "\x1b[?25l\x1b[39m\x1b[38;2;255;0;0m█\x1b[39m\n"
        "\x1b[38;2;0;255;0m█\x1b[39m\n"
        "\x1b[?25h\n"
    )
    grid = parse_chafa_ansi(raw_ansi)
    assert len(grid) == 2
    assert grid[0][0].char == "█"
    assert grid[0][0].color == (255, 0, 0)
    assert grid[1][0].char == "█"
    assert grid[1][0].color == (0, 255, 0)


def test_hex_color_picker_stream():
    # Test direct 24-bit RGB hex selection sequence generation
    cell_1 = Cell("A", (0x12, 0x34, 0x56))
    cell_2 = Cell("B", (0xFE, 0xDC, 0xBA))
    grid = [[cell_1, cell_2]]

    stream, stats = optimize_drawing_stream(grid, origin_x=0, origin_y=0, palette_mode=False)

    # Initial color defaults to PAINT_PALETTE[1] (255, 236, 96 = #FFEC60).
    # First cell (0x12, 0x34, 0x56):
    # \x0b123456\r
    assert b"\x0b123456\r" in stream
    # Second cell (0xFE, 0xDC, 0xBA):
    # \x0bFEDCBA\r
    assert b"\x0bFEDCBA\r" in stream
    assert stats["color_changes"] == 2


def test_drawing_stream_simulation_with_direct_24bit_hex():
    # Simulate execution on a virtual artboard canvas using direct 24-bit hex color picking
    c1 = Cell("こ", (18, 52, 86))   # width 2, #123456
    c2 = Cell("あ", (254, 220, 186)) # width 2, #FEDCBA
    c3 = Cell(" ", None)
    c4 = Cell("!", (42, 42, 42))    # width 1, #2A2A2A

    grid = [
        [c1, c2],
        [c3, c4],
    ]

    origin_x, origin_y = 5, 5
    stream, stats = optimize_drawing_stream(grid, origin_x=origin_x, origin_y=origin_y, palette_mode=False)

    assert stats["chars_typed"] == 3
    assert stats["color_changes"] == 3

    # Virtual artboard interpreter supporting 0x0B hex picker
    canvas = {}
    cx = origin_x
    cy = origin_y
    curr_color = PAINT_PALETTE[1]

    i = 0
    b = stream
    selection_anchor = None
    while i < len(b):
        if b[i : i + 6] == b"\x1b[1;2C":  # Shift+Right
            if selection_anchor is None:
                selection_anchor = (cx, cy)
            cx += 1
            i += 6
        elif b[i : i + 3] == b"\x1b[A":  # Up
            cy -= 1
            selection_anchor = None
            i += 3
        elif b[i : i + 3] == b"\x1b[B":  # Down
            cy += 1
            selection_anchor = None
            i += 3
        elif b[i : i + 3] == b"\x1b[C":  # Right
            cx += 1
            selection_anchor = None
            i += 3
        elif b[i : i + 3] == b"\x1b[D":  # Left
            cx -= 1
            selection_anchor = None
            i += 3
        elif b[i] == 0x7F:  # Backspace
            if selection_anchor is not None:
                # Clear selected cells and reset cursor to anchor
                ax, ay = selection_anchor
                min_x, max_x = min(ax, cx), max(ax, cx)
                min_y, max_y = min(ay, cy), max(ay, cy)
                for sy in range(min_y, max_y + 1):
                    for sx in range(min_x, max_x + 1):
                        canvas.pop((sx, sy), None)
                cx, cy = ax, ay
                selection_anchor = None
            i += 1
        elif b[i] == 0x0B:  # Ctrl+K (open hex color picker)
            # Expect 6 hex digits followed by \r
            i += 1
            hex_digits = b[i : i + 6].decode("ascii")
            r = int(hex_digits[0:2], 16)
            g = int(hex_digits[2:4], 16)
            b_val = int(hex_digits[4:6], 16)
            curr_color = (r, g, b_val)
            i += 6
            assert b[i] == 0x0D  # \r (Enter applies color)
            i += 1
        elif b[i : i + 6] == b"\x1b[200~":
            # Bracketed paste start
            i += 6
            paste_end = b.find(b"\x1b[201~", i)
            assert paste_end != -1
            paste_text = b[i:paste_end].decode("utf-8")
            for ch in paste_text:
                w = char_display_width(ch)
                canvas[(cx, cy)] = (ch, curr_color)
                cx += w
            i = paste_end + 6
        else:
            first = b[i]
            if first < 0x80:
                ch_len = 1
            elif (first & 0xE0) == 0xC0:
                ch_len = 2
            elif (first & 0xF0) == 0xE0:
                ch_len = 3
            else:
                ch_len = 4
            ch = b[i : i + ch_len].decode("utf-8")
            w = char_display_width(ch)
            canvas[(cx, cy)] = (ch, curr_color)
            cx += w
            i += ch_len

    assert canvas[(5, 5)] == ("こ", (18, 52, 86))
    assert canvas[(7, 5)] == ("あ", (254, 220, 186))
    assert (5, 6) not in canvas
    assert canvas[(6, 6)] == ("!", (42, 42, 42))

def test_production_safety_gate():
    import subprocess
    cmd = [
        sys.executable,
        "scripts/artboard_painter.py",
        "--image", "test.ppm",
        "--host", "late.sh",
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert res.returncode == 1
    assert "SAFETY ERROR" in res.stderr

    # With --danger-mode and --dry-run, it should not fail on host check
    cmd_danger = [
        sys.executable,
        "scripts/artboard_painter.py",
        "--image", "late-web/static/og-image.png",
        "--host", "late.sh",
        "--danger-mode",
        "--dry-run",
    ]
    res_danger = subprocess.run(cmd_danger, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert res_danger.returncode == 0
    assert "SAFETY ERROR" not in res_danger.stderr

def test_overlay_mode_skips_occupied_cells():
    # 2x2 grid:
    # (0,0): "X"  (1,0): "Y"
    # (0,1): "Z"  (1,1): "W"
    c1 = Cell("X", (255, 110, 64))
    c2 = Cell("Y", (255, 110, 64))
    c3 = Cell("Z", (255, 110, 64))
    c4 = Cell("W", (255, 110, 64))
    grid = [[c1, c2], [c3, c4]]

    # Suppose (10,10) and (11,11) are occupied by other users
    occupied = {(10, 10), (11, 11)}
    stream, stats = optimize_drawing_stream(
        grid,
        origin_x=10,
        origin_y=10,
        palette_mode=True,
        occupied_cells=occupied,
    )

    # Only "Y" at (11,10) and "Z" at (10,11) should be typed!
    assert stats["chars_typed"] == 2
    assert b"\x1b[200~Y\x1b[201~" in stream
    assert b"\x1b[200~Z\x1b[201~" in stream
    assert b"X" not in stream
    assert b"W" not in stream

def test_extract_owner_from_tui_text():
    from scripts.artboard_painter import extract_owner_from_tui_text
    # Unpopulated cell: '?'
    tui_unpopulated = "Mode       active\nCursor     10,10\nMouse      10,10\nOwner      ?\n"
    assert extract_owner_from_tui_text(tui_unpopulated) is None

    # Populated cell with ANSI escapes
    tui_populated = "Mode       active\x1b[38;2;164;176;193;49mOwner      \x1b[1m\x1b[38;2;255;255;255;49mchris\x1b[10;99H\x1b[22m"
    assert extract_owner_from_tui_text(tui_populated) == "chris"

    # Empty or missing
    assert extract_owner_from_tui_text("") is None
def test_decoupled_core_logic_import():
    from scripts.artboard_painter import (
        parse_origin,
        render_image_to_grid,
        prepare_drawing_stream,
        draw_art,
        app,
    )
    x, y = parse_origin("15,20")
    assert (x, y) == (15, 20)

    grid, stream, stats = prepare_drawing_stream(
        "late-web/static/og-image.png",
        origin_x=5,
        origin_y=5,
        chafa_args="--symbols=block --size=20x10",
        palette_mode=True,
    )
    assert len(grid) > 0
    assert stats["chars_typed"] > 0
    assert len(stream) > 0
    assert app is not None

def test_scheduled_artboard_painter():
    from scripts.scheduled_artboard_painter import (
        choose_next_image,
        run_scheduler,
        POOL_A,
        POOL_B,
        PAINT_CONFIG,
        INTERVAL_SECONDS,
    )

    assert INTERVAL_SECONDS == 90 * 60
    assert len(POOL_A) > 0 and len(POOL_B) > 0

    # Repetition prevention test
    pool = ["img1.png", "img2.png"]
    assert choose_next_image(pool, "img1.png") == "img2.png"
    assert choose_next_image(pool, "img2.png") == "img1.png"
    assert choose_next_image(["img1.png"], "img1.png") == "img1.png"

    # Dry run 2 runs alternating between Pool A and Pool B
    cfg = dict(PAINT_CONFIG)
    cfg["dry_run"] = True
    run_scheduler(
        pool_a=["late-web/static/og-image.png"],
        pool_b=["late-web/static/apple-touch-icon.png"],
        interval_seconds=1,
        config=cfg,
        max_runs=2,
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
