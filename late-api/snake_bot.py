import time
import argparse
import re
from collections import deque
from late_client.bot import LiveBotClient

class SnakeBot(LiveBotClient):
    def __init__(self, watch=False):
        super().__init__(watch=watch, width=120, height=40)
        self.last_processed_head = None
        self.current_direction = 'd' # Game spawns snake moving Right ('d')

    def navigate_to_game(self):
        self.log("Waiting for login...")
        time.sleep(2)
        
        self.log("Sending ESC (to clear splash if any)...")
        self.send_keys('\x1b\x1b')
        time.sleep(1)

        self.log("Entering Arcade (2)...")
        self.send_keys('2')
        time.sleep(1)

        self.log("Selecting Snake (jj\\r)...")
        self.send_keys('jj\r')
        time.sleep(1)

        self.log("Starting Game (r)...")
        self.send_keys('r')
        self.current_direction = 'd'
        time.sleep(1)

    def find_snake_arena(self, screen):
        """
        Locates the exact bounding box of the centered Snake game frame.
        """
        top_r, left_c = -1, -1
        bottom_r, right_c = -1, -1
        
        for r in range(len(screen)):
            line = screen[r]
            for c in range(len(line)):
                if line[c] == '╔':
                    top_r, left_c = r, c
                elif line[c] == '╝':
                    bottom_r = r
                    right_c = c + 1
                    
        if top_r != -1 and bottom_r != -1:
            return top_r, left_c, bottom_r, right_c
            
        # Fallback if bottom-right corner is obscured
        if top_r != -1:
            for r in range(top_r + 1, len(screen)):
                if left_c < len(screen[r]) and screen[r][left_c] == '╚':
                    bottom_r = r
                    for c in range(left_c + 2, len(screen[r])):
                        if screen[r][c] in ('╝', '║'):
                            right_c = c + 1
                    break
            if bottom_r != -1 and right_c != -1:
                return top_r, left_c, bottom_r, right_c
                
        return None

    def is_yellow(self, fg):
        if not fg:
            return False
        if fg[0] in ('33', '93'):
            return True
        if fg[0] == '38' and len(fg) >= 3 and fg[1] == '5':
            return fg[2] in ('3', '11', '220', '226', '227', '228')
        if fg[0] == '38' and len(fg) >= 5 and fg[1] == '2':
            try:
                r, g, b = int(fg[2]), int(fg[3]), int(fg[4])
                return r > 150 and g > 150 and b < 120
            except ValueError:
                return False
        return False

    def parse_grid(self, screen):
        """
        Converts the terminal screen into a 1-to-1 logical snake grid (gy, gx).
        """
        arena = self.find_snake_arena(screen)
        if not arena:
            return None, None, None, None, None, None
            
        top_r, left_c, bottom_r, right_c = arena
        H = bottom_r - top_r + 1
        W = (right_c - left_c + 1) // 2
        
        head = None
        foods = []
        drugs = []
        obstacles = set()
        
        for gy in range(H):
            for gx in range(W):
                r = top_r + gy
                c = left_c + gx * 2
                chunk = screen[r][c : c + 2] if r < len(screen) and c < len(screen[r]) else '  '
                
                # Outer walls
                if any(ch in '╔╗╚╝═║' for ch in chunk) or gy == 0 or gy == H - 1 or gx == 0 or gx == W - 1:
                    obstacles.add((gy, gx))
                elif '×' in chunk: # Rocks
                    obstacles.add((gy, gx))
                elif '●' in chunk: # Tail
                    obstacles.add((gy, gx))
                elif '★' in chunk: # Power-up / Speed Star
                    drugs.append((gy, gx))
                elif '☻' in chunk: # Snake Head
                    head = (gy, gx)
                elif '◉' in chunk:
                    fg = None
                    if hasattr(screen, 'fg_colors') and r < len(screen.fg_colors):
                        if c < len(screen.fg_colors[r]):
                            fg = screen.fg_colors[r][c] or (screen.fg_colors[r][c+1] if c+1 < len(screen.fg_colors[r]) else None)
                    if self.is_yellow(fg):
                        foods.append((gy, gx))
                    else:
                        obstacles.add((gy, gx)) # Body segments
                        
        return H, W, head, foods, drugs, obstacles

    def find_best_move(self, H, W, head, foods, drugs, obstacles):
        """
        Uses BFS to route to the nearest star (★) or food (◉). 
        Falls back to largest-flood-fill open space when trapped.
        """
        if not head:
            return None
            
        opposite = {'w': 's', 's': 'w', 'a': 'd', 'd': 'a'}
        directions = {
            'w': (-1, 0),
            's': (1, 0),
            'a': (0, -1),
            'd': (0, 1)
        }

        # 1. BFS to targets
        def bfs(targets):
            if not targets:
                return None
            queue = deque([(head, [])])
            visited = set([head])
            
            while queue:
                (cy, cx), path = queue.popleft()
                if (cy, cx) in targets:
                    return path[0] if path else None
                    
                for move, (dy, dx) in directions.items():
                    # Never 180° reverse into our own moving direction on the immediate step
                    if not path and self.current_direction and move == opposite.get(self.current_direction):
                        continue
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < H and 0 <= nx < W and (ny, nx) not in visited and (ny, nx) not in obstacles:
                        visited.add((ny, nx))
                        queue.append(((ny, nx), path + [move]))
            return None

        # Prioritize star (★)
        move = bfs(drugs)
        if move:
            return move

        # Then food (◉)
        move = bfs(foods)
        if move:
            return move

        # 2. Fallback: flood fill for maximum safe survival area
        best_move = None
        max_area = -1
        
        for move, (dy, dx) in directions.items():
            if self.current_direction and move == opposite.get(self.current_direction):
                continue
            ny, nx = head[0] + dy, head[1] + dx
            if 0 <= ny < H and 0 <= nx < W and (ny, nx) not in obstacles:
                area_visited = set([head, (ny, nx)])
                area_q = deque([(ny, nx)])
                area_count = 0
                while area_q:
                    ay, ax = area_q.popleft()
                    area_count += 1
                    for _, (ady, adx) in directions.items():
                        any_y, any_x = ay + ady, ax + adx
                        if 0 <= any_y < H and 0 <= any_x < W and (any_y, any_x) not in area_visited and (any_y, any_x) not in obstacles:
                            area_visited.add((any_y, any_x))
                            area_q.append((any_y, any_x))
                if area_count > max_area:
                    max_area = area_count
                    best_move = move

        return best_move

    def play(self):
        self.connect()
        self.navigate_to_game()
        self.log("Bot is playing Snake...")
        summary = None
        try:
            while self.is_alive():
                screen = self.get_screen()
                
                # Check for Game Over
                flat_screen = "".join(screen)
                if "GAME OVER" in flat_screen or "YOU DIED!" in flat_screen:
                    score_match = re.search(r'score\s+(\d+)', flat_screen)
                    best_match = re.search(r'best\s+(\d+)', flat_screen)
                    score = score_match.group(1) if score_match else "0"
                    best = best_match.group(1) if best_match else "0"
                    summary = f"\n💀 Game Over! The snake died.\nFinal Score: {score}\nBest Score:  {best}\n"
                    break

                H, W, head, foods, drugs, obstacles = self.parse_grid(screen)
                if not head:
                    time.sleep(0.02)
                    continue

                # Deduplication: only calculate and send moves when the snake has stepped into a new cell
                if head == self.last_processed_head:
                    time.sleep(0.01)
                    continue

                best_move = self.find_best_move(H, W, head, foods, drugs, obstacles)
                
                if best_move:
                    # ONLY send the directional key if we are changing direction!
                    # Sending the same direction key causes accidental game acceleration/stutter resets.
                    if best_move != self.current_direction:
                        self.send_keys(best_move)
                        self.current_direction = best_move
                        
                    self.last_processed_head = head

                time.sleep(0.01)

        except KeyboardInterrupt:
            self.log("Stopping...")
        finally:
            self.close()
            if summary:
                print(summary)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous Snake Bot")
    parser.add_argument("--watch", action="store_true", help="Mirror the VT100 output to stdout for live viewing")
    args = parser.parse_args()

    bot = SnakeBot(watch=args.watch)
    bot.play()
