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
scheduled_artboard_painter.py - Run scheduled artboard drawing jobs alternating between two image pools.
"""

import pathlib
import random
import signal
import sys
import time

# Ensure repository root is on sys.path when executed directly or imported
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.artboard_painter import draw_art

# ==============================================================================
# CONFIGURATION
# ==============================================================================
INTERVAL_SECONDS = 30 * 60  # 90 minutes

IMAGE_BASE_PATH = pathlib.Path("/Users/chris/Desktop/artboard/")

# Image pools
POOL_A = [
    "eye-direct.webp",
    "eye-left.webp",
    "eye-right.webp",
    "eye-white.webp",
    "eye-tv.webp",
    "eye-static.webp",
    "reset-ahead.jpg",
]

POOL_B = [
    "braces.webp",
    "dummy.webp",
    "teeth.webp",
    "fangs.webp",
    "gums.webp",
    "rainbow.webp",
    "pool.webp",
    "reset-right.jpg"
]

# Shared paint job parameters inherited by every run
PAINT_CONFIG = {
    "chafa_args": "--symbols=wedge --size=102 -c full --fg-only",
    "origin": "0,0",
    "dry_run": False,
    "preview": False,
    "palette_mode": False,
    "direct_color": True,
    "danger_mode": True,
    "overlay": False,
    "output_bytes": None,
    "host": "late.sh",
    "port": 22,
    "username": "chris",
    "identity": str(pathlib.Path("/Users/chris/.ssh/id_late_sh_ed25519")),
}


def choose_next_image(pool: list[str], last_chosen: str | None = None) -> str:
    """Randomly pick an image from the pool, preventing immediate repetition when possible."""
    if not pool:
        raise ValueError("Image pool is empty.")
    if len(pool) == 1:
        return pool[0]

    candidates = [img for img in pool if img != last_chosen]
    return random.choice(candidates if candidates else pool)


def run_single_paint_job(image_path: str, config: dict = PAINT_CONFIG) -> None:
    """Execute a single paint run using the shared configuration."""
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting paint job with image: {image_path}"
    )
    draw_art(image=image_path, **config)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Finished paint job.")


def run_scheduler(
    pool_a: list[str] = POOL_A,
    pool_b: list[str] = POOL_B,
    interval_seconds: int = INTERVAL_SECONDS,
    config: dict = PAINT_CONFIG,
    max_runs: int | None = None,
) -> None:
    """Run scheduled painting loop alternating between Pool A and Pool B."""
    running = True

    def handle_shutdown(signum, frame):
        nonlocal running
        print(f"\nReceived shutdown signal ({signum}). Exiting scheduler loop...")
        running = False

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    last_chosen_a: str | None = None
    last_chosen_b: str | None = None
    use_pool_a = True
    runs_completed = 0

    print(
        f"Starting artboard painter scheduler (interval: {interval_seconds}s / {interval_seconds // 60}m)..."
    )

    while running:
        if use_pool_a:
            chosen = choose_next_image(pool_a, last_chosen_a)
            last_chosen_a = chosen
            pool_name = "Pool A"
        else:
            chosen = choose_next_image(pool_b, last_chosen_b)
            last_chosen_b = chosen
            pool_name = "Pool B"

        use_pool_a = not use_pool_a

        print(f"\n--- Run #{runs_completed + 1} ({pool_name}): {chosen} ---")
        try:
            run_single_paint_job(chosen, config)
        except Exception as e:
            print(f"[!] Error during paint job: {e}", file=sys.stderr)

        runs_completed += 1
        if max_runs is not None and runs_completed >= max_runs:
            print(f"Reached max runs ({max_runs}). Exiting.")
            break

        print(f"Sleeping for {interval_seconds} seconds until next run...")
        # Sleep in small increments to respond quickly to termination signals
        deadline = time.time() + interval_seconds
        while running and time.time() < deadline:
            time.sleep(min(1.0, max(0.1, deadline - time.time())))


if __name__ == "__main__":
    eyes = [str(IMAGE_BASE_PATH / fname) for fname in POOL_A]
    mouth = [str(IMAGE_BASE_PATH / fname) for fname in POOL_B]
    run_scheduler(pool_a=eyes, pool_b=mouth)
