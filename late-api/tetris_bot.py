import argparse
import re
import time

from late_client.bot import LiveBotClient

PIECE_OFFSETS = {
    'I': [
        [(0, 0), (0, 1), (0, 2), (0, 3)],
        [(0, 1), (1, 1), (2, 1), (3, 1)],
        [(0, 0), (0, 1), (0, 2), (0, 3)],
        [(0, 1), (1, 1), (2, 1), (3, 1)],
    ],
    'O': [
        [(0, 1), (0, 2), (1, 1), (1, 2)],
        [(0, 1), (0, 2), (1, 1), (1, 2)],
        [(0, 1), (0, 2), (1, 1), (1, 2)],
        [(0, 1), (0, 2), (1, 1), (1, 2)],
    ],
    'T': [
        [(0, 1), (1, 0), (1, 1), (1, 2)],
        [(0, 1), (1, 1), (1, 2), (2, 1)],
        [(1, 0), (1, 1), (1, 2), (2, 1)],
        [(0, 1), (1, 0), (1, 1), (2, 1)],
    ],
    'S': [
        [(0, 1), (0, 2), (1, 0), (1, 1)],
        [(0, 1), (1, 1), (1, 2), (2, 2)],
        [(0, 1), (0, 2), (1, 0), (1, 1)],
        [(0, 1), (1, 1), (1, 2), (2, 2)],
    ],
    'Z': [
        [(0, 0), (0, 1), (1, 1), (1, 2)],
        [(0, 2), (1, 1), (1, 2), (2, 1)],
        [(0, 0), (0, 1), (1, 1), (1, 2)],
        [(0, 2), (1, 1), (1, 2), (2, 1)],
    ],
    'J': [
        [(0, 0), (1, 0), (1, 1), (1, 2)],
        [(0, 1), (0, 2), (1, 1), (2, 1)],
        [(1, 0), (1, 1), (1, 2), (2, 2)],
        [(0, 1), (1, 1), (2, 0), (2, 1)],
    ],
    'L': [
        [(0, 2), (1, 0), (1, 1), (1, 2)],
        [(0, 1), (1, 1), (2, 1), (2, 2)],
        [(1, 0), (1, 1), (1, 2), (2, 0)],
        [(0, 0), (0, 1), (1, 1), (2, 1)],
    ],
}

class TetrisBot(LiveBotClient):
    def __init__(self, watch=False, target=None):
        super().__init__(watch=watch, width=120, height=40)
        self.target = target

    def navigate_to_game(self):
        self.log("Waiting for login...")
        time.sleep(2)
        
        self.log("Sending ESC (to clear splash if any)...")
        self.send_keys('\x1b\x1b')
        time.sleep(1)

        self.log("Entering Arcade (2)...")
        self.send_keys('2')
        time.sleep(1)

        self.log("Selecting Lateris (jjjjjjjj\\r)...")
        # Tetris is the 2nd game in the Arcade lobby
        self.send_keys('jjjjjjjj\r')
        time.sleep(1)

        self.log("Starting Game (r)...")
        self.send_keys('r')
        time.sleep(1)

    def get_screen_grid(self, screen):
        top_r, top_c = -1, -1
        for r, line in enumerate(screen):
            c = line.find('┌────────────────────┐')
            if c != -1:
                top_r, top_c = r, c
                break
                
        if top_r == -1: 
            return None

        # Extract 20x10 boolean grid of blocks
        grid = [[False]*10 for _ in range(20)]
        for r in range(20):
            if top_r + 1 + r >= len(screen):
                break
            line = screen[top_r + 1 + r]
            for c in range(10):
                # Cells are 2 characters wide ('██' or '  ')
                idx = top_c + 1 + c*2
                if idx < len(line) and line[idx] == '█':
                    grid[r][c] = True

        return grid

    def detect_active_piece(self, grid):
        """
        Identifies the active piece kind and cells by matching the 7 canonical
        spawn footprints (rotation 0, anchor column 3) top-to-bottom.

        The piece always spawns at column 3 in rotation 0 and only gravity
        moves it before this bot acts, so its column/rotation are fixed at
        detection time. Scanning every row (not just the spawn rows) survives
        the piece having already fallen several rows when the screen is read.
        """
        for spawn_row in range(20):
            for kind, rot_offsets in PIECE_OFFSETS.items():
                offsets = rot_offsets[0] # Spawn rotation is always 0
                cells = [(spawn_row + dr, 3 + dc) for dr, dc in offsets]
                if all(0 <= r < 20 and 0 <= c < 10 and grid[r][c] for r, c in cells):
                    return kind, cells
        return None, None

    def evaluate_board(self, board, landing_height, lines_cleared):
        """
        Pierre Dellacherie's classic evaluation heuristic for Tetris.
        Maintains an exceptionally flat, hole-free board.
        """
        # 1. Row Transitions
        row_transitions = 0
        for r in range(20):
            prev = True # Board left wall is filled
            for c in range(10):
                curr = board[r][c]
                if curr != prev:
                    row_transitions += 1
                prev = curr
            if not prev: # Board right wall is filled
                row_transitions += 1
                
        # 2. Column Transitions
        col_transitions = 0
        for c in range(10):
            prev = False # Top of board is empty
            for r in range(20):
                curr = board[r][c]
                if curr != prev:
                    col_transitions += 1
                prev = curr
            if not prev: # Bottom floor is filled
                col_transitions += 1
                
        # 3. Number of Holes
        holes = 0
        for c in range(10):
            block_above = False
            for r in range(20):
                if board[r][c]:
                    block_above = True
                elif block_above:
                    holes += 1
                    
        # 4. Cumulative Well Sums
        well_sums = 0
        for c in range(10):
            d = 0
            for r in range(20):
                left_filled = True if c == 0 else board[r][c - 1]
                right_filled = True if c == 9 else board[r][c + 1]
                if not board[r][c] and left_filled and right_filled:
                    d += 1
                    well_sums += d
                else:
                    d = 0
                    
        return (
            -4.500158825082766 * landing_height
            + 3.4181268101392694 * lines_cleared
            - 3.2178882868487753 * row_transitions
            - 9.348695305445199 * col_transitions
            - 7.899265427351652 * holes
            - 3.3855972247263626 * well_sums
        )

    def plan_move(self, board, piece_kind):
        """
        Simulates all valid rotations and horizontal shifts in memory,
        evaluating each landing state in <1 millisecond.
        """
        best_score = -float('inf')
        best_rot = 0
        best_col_shift = 0
        
        for rot in range(4):
            offsets = PIECE_OFFSETS[piece_kind][rot]
            min_c = min(c for r, c in offsets)
            max_c = max(c for r, c in offsets)
            
            # Initial spawn anchor column is 3
            min_shift = -min_c - 3
            max_shift = 9 - max_c - 3
            
            for col_shift in range(min_shift, max_shift + 1):
                anchor_col = 3 + col_shift
                shifted = [(r, anchor_col + c) for r, c in offsets]
                
                # Verify valid placement within board boundaries
                if any(r >= 20 or c < 0 or c >= 10 or board[r][c] for r, c in shifted):
                    continue
                    
                # Simulate gravity drop
                drop = 0
                while True:
                    next_pos = [(r + drop + 1, c) for r, c in shifted]
                    if any(r >= 20 or board[r][c] for r, c in next_pos):
                        break
                    drop += 1
                    
                landed = [(r + drop, c) for r, c in shifted]
                max_landed_r = max(r for r, c in landed)
                landing_height = 20 - max_landed_r
                
                # Clone board and place piece
                new_board = [row[:] for row in board]
                for r, c in landed:
                    new_board[r][c] = True
                    
                # Simulate full line clearing and row dropping
                cleared_rows = [r for r in range(20) if all(new_board[r])]
                lines_cleared = len(cleared_rows)
                
                if lines_cleared > 0:
                    cleared_board = [[False]*10 for _ in range(lines_cleared)]
                    for r in range(20):
                        if r not in cleared_rows:
                            cleared_board.append(new_board[r])
                    new_board = cleared_board
                    
                score = self.evaluate_board(new_board, landing_height, lines_cleared)
                if score > best_score:
                    best_score = score
                    best_rot = rot
                    best_col_shift = col_shift
                    
        return best_rot, best_col_shift

    def play(self):
        self.connect()
        self.navigate_to_game()
        self.log("Bot is playing Lateris (Tetris)...")
        summary = None
        target_reached = False
        try:
            while self.is_alive():
                screen = self.get_screen()
                flat_screen = "".join(screen)
                
                # Detect Game Over
                if "GAME OVER" in flat_screen:
                    score_match = re.search(r'score\s+(\d+)', flat_screen)
                    lines_match = re.search(r'lines\s+(\d+)', flat_screen)
                    score = score_match.group(1) if score_match else "0"
                    lines = lines_match.group(1) if lines_match else "0"
                    summary = f"\n💀 Game Over! Lateris run finished.\nFinal Score:   {score}\nLines Cleared: {lines}\n"
                    break

                # Once the target score is reached, stop making moves and just
                # idle so the board stacks up and the game ends naturally.
                if target_reached:
                    time.sleep(0.05)
                    continue

                if self.target is not None:
                    target_match = re.search(r'score\s+(\d+)', flat_screen)
                    if target_match and int(target_match.group(1)) >= self.target:
                        target_reached = True
                        self.log(f"Target score {self.target} reached — no longer making moves")
                        continue
                grid = self.get_screen_grid(screen)
                if not grid:
                    time.sleep(0.05)
                    continue

                piece_kind, piece_cells = self.detect_active_piece(grid)
                if not piece_kind:
                    time.sleep(0.02)
                    continue

                # Build the settled board by clearing the active piece's cells
                settled_board = [row[:] for row in grid]
                for r, c in piece_cells:
                    settled_board[r][c] = False

                # Plan the optimal placement instantaneously
                best_rot, best_shift = self.plan_move(settled_board, piece_kind)

                # Assemble the keystrokes
                keys = ""
                if best_rot > 0:
                    keys += 'k' * best_rot
                if best_shift < 0:
                    keys += 'h' * (-best_shift)
                elif best_shift > 0:
                    keys += 'l' * best_shift
                keys += ' ' # Hard drop!

                # Send the entire sequence in one atomic burst
                self.send_keys(keys)

                # Brief pause for the hard drop and spawn transition
                time.sleep(0.18)

        except KeyboardInterrupt:
            self.log("Stopping...")
        finally:
            self.close()
            if summary:
                print(summary)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous Lateris (Tetris) Bot")
    parser.add_argument("--watch", action="store_true", help="Mirror the VT100 output to stdout for live viewing")
    parser.add_argument("--target", type=int, default=None, help="Stop making moves once this score is reached; the game then ends naturally")
    args = parser.parse_args()

    bot = TetrisBot(watch=args.watch, target=args.target)
    bot.play()
