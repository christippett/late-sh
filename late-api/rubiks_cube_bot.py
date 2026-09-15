import argparse
import re
import time
from datetime import datetime, timezone

from late_client.bot import LiveBotClient

# Palette matching late-ssh/src/app/arcade/rubiks_cube/ui.rs
PALETTE = {
    'W': (232, 236, 239),
    'Y': (246, 202, 68),
    'O': (236, 126, 42),
    'R': (212, 63, 56),
    'G': (63, 160, 92),
    'B': (65, 115, 204),
}


def _identify_sticker(bg) -> str | None:
    if not bg or len(bg) < 5 or bg[0] != '48' or bg[1] != '2':
        return None
    r, g, b = int(bg[2]), int(bg[3]), int(bg[4])
    best = None
    min_dist = float('inf')
    for name, (pr, pg, pb) in PALETTE.items():
        dist = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if dist < min_dist:
            min_dist = dist
            best = name
    return best if min_dist < 4500 else None


def compute_daily_solution() -> str:
    """
    Computes the exact inverse of today's deterministic daily scramble.
    """
    today = datetime.now(timezone.utc).date()
    seed = 0xcbf29ce484222325
    for b in b"late-sh-rubiks-cube-daily-v1":
        seed ^= b
        seed = (seed * 0x00000100000001b3) & 0xFFFFFFFFFFFFFFFF
    date_str = today.strftime("%Y-%m-%d").encode('utf-8')
    for b in date_str:
        seed ^= b
        seed = (seed * 0x00000100000001b3) & 0xFFFFFFFFFFFFFFFF

    faces = ['U', 'D', 'L', 'R', 'F', 'B']
    previous = None
    moves = []
    for _ in range(24):
        seed = (seed * 6364136223846793005 + 1) & 0xFFFFFFFFFFFFFFFF
        face = faces[seed % len(faces)]
        while face == previous:
            seed = (seed * 6364136223846793005 + 1) & 0xFFFFFFFFFFFFFFFF
            face = faces[seed % len(faces)]
        seed = (seed * 6364136223846793005 + 1) & 0xFFFFFFFFFFFFFFFF
        inverse = (seed % 2 == 0)
        moves.append((face, inverse))
        previous = face

    solution_keys = []
    for face, inverse in reversed(moves):
        new_inv = not inverse
        key = face.upper() if new_inv else face.lower()
        solution_keys.append(key)

    return "".join(solution_keys)


class RubiksCubeBot(LiveBotClient):
    def __init__(self, watch=False):
        super().__init__(watch=watch, width=120, height=40)

    def navigate_to_game(self):
        self.log("Waiting for login...")
        time.sleep(2)

        self.log("Clearing splash screen...")
        self.send_keys('\x1b\x1b')
        time.sleep(1)

        self.log("Entering Arcade (2)...")
        self.send_keys('2')
        time.sleep(1)

        self.log("Selecting Rubik's Cube (5j\\r)...")
        # In the Arcade lobby order:
        # 0: 2048, 1: Tetris, 2: Snake, 3: Traffic, 4: Le Word, 5: Rubik's Cube
        self.send_keys('j\r')
        time.sleep(1)

    def is_solved(self, screen_lines) -> bool:
        flat = "".join(screen_lines)
        return "Solved." in flat or "Daily cube solved" in flat

    def extract_net(self, screen_lines) -> dict[str, list[list[str]]] | None:
        """
        Extracts the 6 face grids (U, L, F, R, B, D) from the 2D unfolded net view.
        """
        # Find the line with "All sides"
        header_r = -1
        for r, line in enumerate(screen_lines):
            if "All sides" in line:
                header_r = r
                break

        if header_r == -1:
            return None

        # U face is at header_r + 2 (top box)
        # L, F, R, B faces are at header_r + 7 (middle strip)
        # D face is at header_r + 12 (bottom box)
        net = {}
        slots = [
            ("U", header_r + 2),
            ("L", header_r + 7),
            ("F", header_r + 7),
            ("R", header_r + 7),
            ("B", header_r + 7),
            ("D", header_r + 12),
        ]

        # Scan for box labels ┌──U───┐, etc.
        for slot in ["U", "L", "F", "R", "B", "D"]:
            label = f"┌──{slot}───┐"
            found = False
            for r in range(header_r, min(len(screen_lines), header_r + 20)):
                line = screen_lines[r]
                if label in line:
                    c = line.index(label)
                    # The 3 sticker rows are at r+1, r+2, r+3
                    # Inside the box: column c+1 to c+7 (3 stickers, width 2 each: c+1..c+2, c+3..c+4, c+5..c+6)
                    grid = []
                    for row_idx in range(3):
                        sticker_r = r + 1 + row_idx
                        row_stickers = []
                        for col_idx in range(3):
                            col_c = c + 1 + col_idx * 2
                            # Read bg color from screen_lines.colors
                            bg = screen_lines.colors[sticker_r][col_c]
                            stk = _identify_sticker(bg)
                            if stk:
                                row_stickers.append(stk)
                            else:
                                row_stickers.append('?')
                        grid.append(row_stickers)
                    net[slot] = grid
                    found = True
                    break
            if not found:
                return None

        return net

    def play(self):
        self.connect()
        self.navigate_to_game()
        self.log("Bot is playing Rubik's Cube Daily Challenge...")

        try:
            # First, check if already solved
            screen = self.get_screen()
            if self.is_solved(screen):
                self.log("Today's Rubik's Cube challenge is already solved! 🎉")
                return

            # Reset cube to clean daily scramble: 's' then 's' confirms reset
            self.log("Resetting cube to today's canonical scramble (ss)...")
            self.send_keys('ss')
            time.sleep(0.5)

            # Compute and execute the exact 24-move inverse sequence
            solution = compute_daily_solution()
            self.log(f"Applying inverse daily solution ({len(solution)} moves): {solution}")
            
            for idx, move in enumerate(solution):
                self.send_keys(move)
                time.sleep(0.04)

            time.sleep(0.5)
            screen = self.get_screen()
            if self.is_solved(screen):
                self.log(f"\n🎉 Daily Rubik's Cube solved successfully in {len(solution)} moves!")
            else:
                self.log("Checking final board state...")
                net = self.extract_net(screen)
                if net:
                    self.log(f"Extracted net: {net}")

        except KeyboardInterrupt:
            self.log("Stopping...")
        finally:
            self.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous Rubik's Cube Bot")
    parser.add_argument("--watch", action="store_true", help="Mirror the VT100 output to stdout for live viewing")
    args = parser.parse_args()

    bot = RubiksCubeBot(watch=args.watch)
    bot.play()
