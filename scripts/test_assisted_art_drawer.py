#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest>=7.0.0",
#     "asyncssh>=2.14.0",
#     "typer>=0.9.0",
#     "chafa.py>=1.2.0",
#     "pillow>=10.0.0",
# ]
# ///
"""
test_assisted_art_drawer.py - Test suite for artboard drawing tool.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import chafa

from scripts.artboard_painter import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    KC,
    ArtboardClient,
    Chafa,
    Color,
    Coords,
    PaintPalette,
    RemoteHost,
    app,
)


def test_coords_parsing_and_validation():
    # Valid parsing
    c = Coords.from_string("15,20")
    assert (c.x, c.y) == (15, 20)
    assert str(c) == "15,20"

    # from_input coercion
    assert Coords.from_input("0,0") == Coords(0, 0)
    assert Coords.from_input((10, 20)) == Coords(10, 20)
    assert Coords.from_input(Coords(5, 5)) == Coords(5, 5)

    # Invalid string formats
    with pytest.raises(ValueError):
        Coords.from_string("not,a,coord")
    with pytest.raises(ValueError):
        Coords.from_string("10")

    # Out of bounds
    with pytest.raises(ValueError):
        Coords.from_string(f"{CANVAS_WIDTH},0")
    with pytest.raises(ValueError):
        Coords.from_string(f"0,{CANVAS_HEIGHT}")
    with pytest.raises(ValueError):
        Coords.from_string("-1,0")

    # Invalid type
    with pytest.raises(TypeError):
        Coords.from_input(123)  # type: ignore[arg-type]


def test_remote_host_parsing_and_safety():
    h1 = RemoteHost.from_string("late-dev")
    assert h1.host == "late-dev"
    assert h1.port is None
    assert h1.username is None
    assert not h1.is_prod()

    h2 = RemoteHost.from_string("chris@late.sh:2222")
    assert h2.host == "late.sh"
    assert h2.username == "chris"
    assert h2.port == 2222
    assert h2.is_prod()

    h3 = RemoteHost.from_string("159.203.111.45:22")
    assert h3.host == "159.203.111.45"
    assert h3.port == 22
    assert h3.is_prod()

    assert RemoteHost.from_input(h1) == h1
    assert RemoteHost.from_input("late-dev") == h1

    with pytest.raises(ValueError):
        RemoteHost.from_string("")
    with pytest.raises(TypeError):
        RemoteHost.from_input(123)  # type: ignore[arg-type]


def test_color_and_palette():
    c = Color.from_hex("#FFAABB")
    assert (c.r, c.g, c.b) == (255, 170, 187)
    assert str(c) == "#FFAABB"

    with pytest.raises(ValueError):
        Color.from_hex("invalid")
    with pytest.raises(ValueError):
        Color.from_hex("#12345")

    assert PaintPalette.YELLOW.value == Color(255, 236, 96)


def test_char_display_width():
    client = ArtboardClient(RemoteHost(host="late-dev"))
    assert client._get_display_width("A") == 1
    assert client._get_display_width(" ") == 1
    assert client._get_display_width("█") == 1  # block characters
    assert client._get_display_width("こ") == 2  # hiragana
    assert client._get_display_width("し") == 2
    assert client._get_display_width("エ") == 2  # katakana


def test_hex_color_picker_stream():
    # Test direct 24-bit RGB hex selection sequence generation using native Canvas
    cfg = chafa.CanvasConfig()
    cfg.width = 10
    cfg.height = 1
    canvas = chafa.Canvas(cfg)
    canvas[0, 0].char = "A"
    canvas[0, 0].fg_color = (0x12, 0x34, 0x56)
    canvas[0, 1].char = "B"
    canvas[0, 1].fg_color = (0xFE, 0xDC, 0xBA)

    client = ArtboardClient(RemoteHost(host="late-dev"))
    tokens = client._generate_drawing_tokens(canvas, origin=Coords(0, 0))
    stream = b"".join(tokens)

    # First cell (0x12, 0x34, 0x56): \x0b123456\r
    assert b"\x0b123456\r" in stream
    # Second cell (0xFE, 0xDC, 0xBA): \x0bFEDCBA\r
    assert b"\x0bFEDCBA\r" in stream
    assert stream.count(KC.CTRL_K) == 2


def test_drawing_stream_simulation_with_direct_24bit_hex():
    # Simulate execution on a virtual artboard canvas using direct 24-bit hex color picking
    cfg = chafa.CanvasConfig()
    cfg.width = 4
    cfg.height = 2
    canvas = chafa.Canvas(cfg)

    # Row 0: "こ" (width 2) at col 0, continuation '\x00' at col 1, "あ" (width 2) at col 2
    canvas[0, 0].char = "こ"
    canvas[0, 0].fg_color = (18, 52, 86)
    canvas[0, 1].char = "\x00"
    canvas[0, 1].fg_color = (18, 52, 86)
    canvas[0, 2].char = "あ"
    canvas[0, 2].fg_color = (254, 220, 186)
    canvas[0, 3].char = "\x00"
    canvas[0, 3].fg_color = (254, 220, 186)

    # Row 1: space at col 0, "!" at col 1
    canvas[1, 0].char = " "
    canvas[1, 0].fg_color = None
    canvas[1, 1].char = "!"
    canvas[1, 1].fg_color = (42, 42, 42)

    origin = Coords(5, 5)
    client = ArtboardClient(RemoteHost(host="late-dev"))
    tokens = client._generate_drawing_tokens(canvas, origin=origin)
    stream = b"".join(tokens)

    # Virtual artboard interpreter supporting 0x0B hex picker and bracketed paste
    virtual_canvas: dict[tuple[int, int], tuple[str, tuple[int, int, int]]] = {}
    cx = origin.x
    cy = origin.y
    curr_color = (255, 255, 255)

    i = 0
    b = stream
    while i < len(b):
        if b[i : i + 3] == b"\x1b[A":  # Up
            cy -= 1
            i += 3
        elif b[i : i + 3] == b"\x1b[B":  # Down
            cy += 1
            i += 3
        elif b[i : i + 3] == b"\x1b[C":  # Right
            cx += 1
            i += 3
        elif b[i : i + 3] == b"\x1b[D":  # Left
            cx -= 1
            i += 3
        elif b[i] == 0x0B:  # Ctrl+K (open hex color picker)
            i += 1
            hex_digits = b[i : i + 6].decode("ascii")
            r = int(hex_digits[0:2], 16)
            g = int(hex_digits[2:4], 16)
            b_val = int(hex_digits[4:6], 16)
            curr_color = (r, g, b_val)
            i += 6
            assert b[i] == 0x0D  # \r
            i += 1
        elif b[i : i + 6] == b"\x1b[200~":  # Bracketed paste
            i += 6
            paste_end = b.find(b"\x1b[201~", i)
            assert paste_end != -1
            paste_text = b[i:paste_end].decode("utf-8")
            for ch in paste_text:
                w = client._get_display_width(ch)
                virtual_canvas[(cx, cy)] = (ch, curr_color)
                cx += w
            i = paste_end + 6
        else:
            i += 1

    assert virtual_canvas[(5, 5)] == ("こ", (18, 52, 86))
    assert virtual_canvas[(7, 5)] == ("あ", (254, 220, 186))
    assert (5, 6) not in virtual_canvas
    assert virtual_canvas[(6, 6)] == ("!", (42, 42, 42))


def test_production_safety_gate():
    # Attempting to run against late.sh without --danger-mode must fail with returncode 1
    cmd = [
        sys.executable,
        "scripts/artboard_painter.py",
        "late-web/static/apple-touch-icon.png",
        "--remote",
        "late.sh",
        "--preview",
    ]
    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
    assert res.returncode == 1
    assert "SAFETY ERROR" in res.stdout or "SAFETY ERROR" in res.stderr

    # With --danger-mode and --preview, it should succeed without attempting SSH
    cmd_danger = [
        sys.executable,
        "scripts/artboard_painter.py",
        "late-web/static/apple-touch-icon.png",
        "--remote",
        "late.sh",
        "--danger-mode",
        "--preview",
    ]
    res_danger = subprocess.run(cmd_danger, check=False, capture_output=True, text=True)
    assert res_danger.returncode == 0
    assert "SAFETY ERROR" not in res_danger.stdout and "SAFETY ERROR" not in res_danger.stderr


def test_decoupled_core_logic_and_render_image():
    image_path = pathlib.Path("late-web/static/apple-touch-icon.png")
    painter = Chafa(image_path=image_path, size="20x10")
    canvas = painter.get_canvas()
    assert isinstance(canvas, chafa.Canvas)

    origin = Coords.from_string("15,20")
    assert (origin.x, origin.y) == (15, 20)

    client = ArtboardClient(RemoteHost(host="late-dev"))
    tokens = client._generate_drawing_tokens(canvas, origin=Coords(5, 5))
    stream = b"".join(tokens)
    assert len(tokens) > 0
    assert len(stream) > 0
    assert app is not None


def test_scheduled_artboard_painter():
    from scripts.scheduled_artboard_painter import (
        INTERVAL_SECONDS,
        PAINT_CONFIG,
        POOL_A,
        POOL_B,
        choose_next_image,
        run_scheduler,
    )

    assert INTERVAL_SECONDS == 30 * 60
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
