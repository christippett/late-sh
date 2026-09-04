import argparse
import random
import re
import time

from late_client.bot import LiveBotClient


def _build_row_lookup_tables():
    """Precomputes 16-bit row transition tables for instant bitboard operations."""
    row_left = [0] * 65536
    row_right = [0] * 65536

    for r in range(65536):
        n0 = (r >> 12) & 0xF
        n1 = (r >> 8) & 0xF
        n2 = (r >> 4) & 0xF
        n3 = r & 0xF

        # Left shift & merge
        tiles = [x for x in (n0, n1, n2, n3) if x != 0]
        merged = []
        i = 0
        while i < len(tiles):
            if i + 1 < len(tiles) and tiles[i] == tiles[i + 1] and tiles[i] < 15:
                merged.append(tiles[i] + 1)
                i += 2
            else:
                merged.append(tiles[i])
                i += 1
        while len(merged) < 4:
            merged.append(0)
        row_left[r] = (merged[0] << 12) | (merged[1] << 8) | (merged[2] << 4) | merged[3]

        # Right shift & merge
        tiles = [x for x in (n3, n2, n1, n0) if x != 0]
        merged = []
        i = 0
        while i < len(tiles):
            if i + 1 < len(tiles) and tiles[i] == tiles[i + 1] and tiles[i] < 15:
                merged.append(tiles[i] + 1)
                i += 2
            else:
                merged.append(tiles[i])
                i += 1
        while len(merged) < 4:
            merged.append(0)
        row_right[r] = (merged[3] << 12) | (merged[2] << 8) | (merged[1] << 4) | merged[0]

    return row_left, row_right


ROW_LEFT, ROW_RIGHT = _build_row_lookup_tables()

# Generate all 8 orientations of the monotonic snake heuristic matrix
WEIGHTS_BASE = [
    [15, 14, 13, 12],
    [ 8,  9, 10, 11],
    [ 7,  6,  5,  4],
    [ 0,  1,  2,  3],
]


def _rotate_matrix(m):
    return [[m[3 - c][r] for c in range(4)] for r in range(4)]


def _flip_matrix(m):
    return [[m[r][3 - c] for c in range(4)] for r in range(4)]


_PATTERNS = []
_curr = WEIGHTS_BASE
for _ in range(4):
    _PATTERNS.append(_curr)
    _PATTERNS.append(_flip_matrix(_curr))
    _curr = _rotate_matrix(_curr)

# 3.5 base factor produces strong monotonic ordering without float overflow
SNAKE_WEIGHTS = [[3.5 ** rank for row in p for rank in row] for p in _PATTERNS]


def _transpose(b: int) -> int:
    r0 = (b >> 48) & 0xFFFF
    r1 = (b >> 32) & 0xFFFF
    r2 = (b >> 16) & 0xFFFF
    r3 = b & 0xFFFF
    c0 = ((r0 & 0xF000) | ((r1 & 0xF000) >> 4) | ((r2 & 0xF000) >> 8) | ((r3 & 0xF000) >> 12))
    c1 = (((r0 & 0x0F00) << 4) | (r1 & 0x0F00) | ((r2 & 0x0F00) >> 4) | ((r3 & 0x0F00) >> 8))
    c2 = (((r0 & 0x00F0) << 8) | ((r1 & 0x00F0) << 4) | (r2 & 0x00F0) | ((r3 & 0x00F0) >> 4))
    c3 = (((r0 & 0x000F) << 12) | ((r1 & 0x000F) << 8) | ((r2 & 0x000F) << 4) | (r3 & 0x000F))
    return (c0 << 48) | (c1 << 32) | (c2 << 16) | c3


def _move_board(b: int, direction: int) -> int:
    # 0: up (k), 1: down (j), 2: left (h), 3: right (l)
    if direction == 2:  # Left
        r0 = ROW_LEFT[(b >> 48) & 0xFFFF]
        r1 = ROW_LEFT[(b >> 32) & 0xFFFF]
        r2 = ROW_LEFT[(b >> 16) & 0xFFFF]
        r3 = ROW_LEFT[b & 0xFFFF]
        return (r0 << 48) | (r1 << 32) | (r2 << 16) | r3
    elif direction == 3:  # Right
        r0 = ROW_RIGHT[(b >> 48) & 0xFFFF]
        r1 = ROW_RIGHT[(b >> 32) & 0xFFFF]
        r2 = ROW_RIGHT[(b >> 16) & 0xFFFF]
        r3 = ROW_RIGHT[b & 0xFFFF]
        return (r0 << 48) | (r1 << 32) | (r2 << 16) | r3
    elif direction == 0:  # Up
        t = _transpose(b)
        r0 = ROW_LEFT[(t >> 48) & 0xFFFF]
        r1 = ROW_LEFT[(t >> 32) & 0xFFFF]
        r2 = ROW_LEFT[(t >> 16) & 0xFFFF]
        r3 = ROW_LEFT[t & 0xFFFF]
        return _transpose((r0 << 48) | (r1 << 32) | (r2 << 16) | r3)
    elif direction == 1:  # Down
        t = _transpose(b)
        r0 = ROW_RIGHT[(t >> 48) & 0xFFFF]
        r1 = ROW_RIGHT[(t >> 32) & 0xFFFF]
        r2 = ROW_RIGHT[(t >> 16) & 0xFFFF]
        r3 = ROW_RIGHT[t & 0xFFFF]
        return _transpose((r0 << 48) | (r1 << 32) | (r2 << 16) | r3)
    return b


def _evaluate_board(b: int) -> float:
    nibbles = [(b >> ((15 - i) * 4)) & 0xF for i in range(16)]
    vals = [(1 << n) if n > 0 else 0 for n in nibbles]

    # 1. Monotonic snake patterns (evaluates across all 8 board symmetries)
    max_snake = 0.0
    for p in SNAKE_WEIGHTS:
        s = sum(vals[i] * p[i] for i in range(16))
        max_snake = max(max_snake, s)

    # 2. Empty cell bonus
    empties = nibbles.count(0)
    empty_score = (empties ** 1.5) * 500000.0 if empties > 0 else 0.0

    # 3. Smoothness penalty
    smoothness = 0.0
    for r in range(4):
        for c in range(3):
            idx = r * 4 + c
            if nibbles[idx] > 0 and nibbles[idx + 1] > 0:
                smoothness -= abs(nibbles[idx] - nibbles[idx + 1]) * 15000.0
    for c in range(4):
        for r in range(3):
            idx = r * 4 + c
            if nibbles[idx] > 0 and nibbles[idx + 4] > 0:
                smoothness -= abs(nibbles[idx] - nibbles[idx + 4]) * 15000.0

    # 4. Merging potential
    merges = 0
    for r in range(4):
        for c in range(3):
            idx = r * 4 + c
            if nibbles[idx] > 0 and nibbles[idx] == nibbles[idx + 1]:
                merges += 1
    for c in range(4):
        for r in range(3):
            idx = r * 4 + c
            if nibbles[idx] > 0 and nibbles[idx] == nibbles[idx + 4]:
                merges += 1
    merge_score = merges * 200000.0

    return max_snake + empty_score + smoothness + merge_score


def _expectimax(b: int, depth: int, max_samples: int = 3) -> float:
    if depth == 0:
        return _evaluate_board(b)

    best = -float('inf')
    moved = False

    for m in (0, 1, 2, 3):
        nb = _move_board(b, m)
        if nb != b:
            moved = True
            empty_shifts = [i * 4 for i in range(16) if ((nb >> (i * 4)) & 0xF) == 0]
            num_empty = len(empty_shifts)
            if num_empty == 0:
                s = _evaluate_board(nb)
            else:
                chosen = empty_shifts if num_empty <= max_samples else random.sample(empty_shifts, max_samples)
                s = 0.0
                for shift in chosen:
                    s += 0.9 * _expectimax(nb | (1 << shift), depth - 1, max_samples)
                    s += 0.1 * _expectimax(nb | (2 << shift), depth - 1, max_samples)
                s /= len(chosen)
            best = max(best, s)

    if not moved:
        return _evaluate_board(b)
    return best


class TwentyFortyEightBot(LiveBotClient):
    def __init__(self, watch=False, target=None):
        super().__init__(watch=watch, width=120, height=40)
        self.target = target

    def navigate_to_game(self):
        self.log("Waiting for login...")
        time.sleep(2)

        self.log("Clearing splash screen...")
        self.send_keys('\x1b\x1b')
        time.sleep(1)

        self.log("Entering Arcade (2)...")
        self.send_keys('2')
        time.sleep(1)

        self.log("Selecting 2048 (\\r)...")
        self.send_keys('\r')
        time.sleep(1)

        self.log("Starting/Restarting Game (r)...")
        self.send_keys('r')
        time.sleep(1)

    def extract_bitboard(self, screen) -> int | None:
        """
        Parses the 4x4 grid from rendered VT100 terminal lines into a 64-bit integer.
        Each cell in ratatui 2048 is drawn as an individual bordered block.
        """
        top_left = None
        for r, line in enumerate(screen):
            c = line.find('┌')
            if c != -1 and '┌──────┐' in line:
                top_left = (r, line.index('┌──────┐'))
                break

        if not top_left:
            return None

        base_r, base_c = top_left
        bitboard = 0

        for row in range(4):
            val_r = base_r + row * 4 + 2
            if val_r >= len(screen):
                return None
            line = screen[val_r]

            for col in range(4):
                col_c = base_c + col * 8
                cell_text = line[col_c:col_c + 8]
                cell_clean = re.sub(r'[│┌┐└┘─\s]', '', cell_text)
                if cell_clean.isdigit():
                    val = int(cell_clean)
                    nibble = val.bit_length() - 1 if val > 0 else 0
                else:
                    nibble = 0

                idx = row * 4 + col
                bitboard |= (nibble << ((15 - idx) * 4))

        return bitboard

    def plan_move(self, bitboard: int) -> str | None:
        """
        Calculates the highest expectation move using bitboard expectimax search
        with adaptive depth and 8-fold symmetric snake heuristics.
        """
        nibbles = [(bitboard >> ((15 - i) * 4)) & 0xF for i in range(16)]
        empties = nibbles.count(0)

        # Adaptive search depth
        if empties <= 3:
            depth = 3
            max_samples = 3
        elif empties <= 7:
            depth = 2
            max_samples = 4
        else:
            depth = 2
            max_samples = 3

        best_move = None
        best_score = -float('inf')

        move_keys = ['k', 'j', 'h', 'l']  # 0: Up, 1: Down, 2: Left, 3: Right

        for m in (0, 1, 2, 3):
            nb = _move_board(bitboard, m)
            if nb != bitboard:
                empty_shifts = [i * 4 for i in range(16) if ((nb >> (i * 4)) & 0xF) == 0]
                num_empty = len(empty_shifts)
                if num_empty == 0:
                    s = _evaluate_board(nb)
                else:
                    chosen = empty_shifts if num_empty <= 4 else random.sample(empty_shifts, 4)
                    s = 0.0
                    for shift in chosen:
                        s += 0.9 * _expectimax(nb | (1 << shift), depth - 1, max_samples)
                        s += 0.1 * _expectimax(nb | (2 << shift), depth - 1, max_samples)
                    s /= len(chosen)

                if s > best_score:
                    best_score = s
                    best_move = move_keys[m]

        return best_move

    def play(self):
        self.connect()
        self.navigate_to_game()
        self.log("Bot is playing 2048...")

        last_board = None
        stuck_counter = 0

        try:
            while self.is_alive():
                screen = self.get_screen()
                flat_screen = "".join(screen)

                if "GAME OVER" in flat_screen:
                    score_match = re.search(r'score\s+(\d+)', flat_screen)
                    score = score_match.group(1) if score_match else "0"
                    print(f"\n💀 Game Over! 2048 run finished.\nFinal Score: {score}\n")
                    break

                if self.target is not None:
                    target_match = re.search(r'score\s+(\d+)', flat_screen)
                    if target_match and int(target_match.group(1)) >= self.target:
                        self.log(f"Target score {self.target} reached!")
                        break

                bitboard = self.extract_bitboard(screen)
                if bitboard is not None:
                    if bitboard == last_board:
                        stuck_counter += 1
                    else:
                        stuck_counter = 0
                        last_board = bitboard

                    if stuck_counter > 3:
                        # Unstick by trying any valid movement
                        for m_idx in (2, 1, 3, 0):  # Left, Down, Right, Up
                            if _move_board(bitboard, m_idx) != bitboard:
                                self.send_keys(['k', 'j', 'h', 'l'][m_idx])
                                break
                    else:
                        move = self.plan_move(bitboard)
                        if move:
                            self.send_keys(move)
                else:
                    self.send_keys(random.choice(['h', 'j', 'l']))

                time.sleep(0.08)

        except KeyboardInterrupt:
            self.log("Stopping...")
        finally:
            self.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous 2048 Bot")
    parser.add_argument("--watch", action="store_true", help="Mirror the VT100 output to stdout for live viewing")
    parser.add_argument("--target", type=int, default=None, help="Stop making moves once this score is reached")
    args = parser.parse_args()

    bot = TwentyFortyEightBot(watch=args.watch, target=args.target)
    bot.play()
