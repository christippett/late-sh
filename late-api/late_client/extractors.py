import re


def extract_leaderboards(lines):
    """
    Parses the structured lines to match headers and their closest
    underlying entries based on column positions.
    """
    headers = []
    leaderboards = {}

    for line in lines:
        # Match headings like "  ── monthly ── "
        h_matches = list(re.finditer(r"──\s+(.*?)\s+──", line))
        if h_matches:
            for m in h_matches:
                col = m.start()
                title = m.group(1).strip()
                # Overwrite headers that share similar column alignment
                headers = [h for h in headers if abs(h[0] - col) > 10]
                headers.append((col, title))
                if title not in leaderboards:
                    leaderboards[title] = []

        # Match leaderboard entries like "#1   username    100 chips"
        e_matches = list(re.finditer(r"#(\d+)\s+(\S+)\s+(.*?)(?=\s{2,}#|\s*$)", line))
        for m in e_matches:
            col = m.start()
            rank = int(m.group(1))
            username = m.group(2)
            # Strip potential TUI box drawing characters from the right tail
            score = m.group(3).strip(" │║").strip()

            if headers:
                closest_header = min(headers, key=lambda h: abs(h[0] - col))
                title = closest_header[1]
                leaderboards[title].append(
                    {"ranking": rank, "username": username, "score": score}
                )

    # Filter out headers that contained no entries
    return {k: v for k, v in leaderboards.items() if v}


def extract_status(screen1_lines, screen2_lines, screen3_lines):
    """
    Parses the terminal screens to extract pet, multiplayer, and arcade status.
    screen1_lines should be the Home lounge view.
    screen2_lines should be the Lobby modal view (after pressing Ctrl+G).
    screen3_lines should be the Arcade view.
    """
    needs_water = False
    needs_food = False

    # 1. Pet Status from screen1 (Home screen)
    for r, line in enumerate(screen1_lines):
        water_idx = line.find("/pet water")
        if water_idx != -1 and r >= 2:
            bowl_content = screen1_lines[r - 2][max(0, water_idx - 2) : water_idx + 12]
            if "(~~~~~~~)" not in bowl_content:
                needs_water = True

        food_idx = line.find("/pet feed")
        if food_idx != -1 and r >= 2:
            bowl_content = screen1_lines[r - 2][max(0, food_idx - 2) : food_idx + 12]
            if "(⚬⚬⚬⚬⚬⚬⚬)" not in bowl_content:
                needs_food = True

    # 2. Games Status from screen2 (Lobby modal)
    games = []
    in_matches_section = False

    for line in screen2_lines:
        if "your matches" in line and "──" in line:
            in_matches_section = True
            continue

        if in_matches_section:
            # If we hit another section (like "lobby" or "live games"), break
            if "──" in line and "your matches" not in line:
                break

            # Active matches end with "your turn" or "waiting"
            if "your turn" in line or "waiting" in line:
                # Based on fixed column widths: marker(2), NAME(16), GAME(12)
                opponent = line[2:18].strip()
                game = line[18:30].strip()

                is_turn = "your turn" in line

                games.append(
                    {"opponent": opponent, "game": game, "their_turn": is_turn}
                )

    # 3. Arcade Status from screen3
    arcade_games = extract_arcade(screen3_lines)

    return {
        "pet": {"needs_water": needs_water, "needs_food": needs_food},
        "games": games,
        "arcade": arcade_games,
    }


def extract_arcade(lines):
    """
    Parses the terminal screen (lines: ScreenLines with colors)
    to extract Arcade daily games completion status.
    """
    arcade_games = {}
    in_daily_section = False

    for row, line in enumerate(lines):
        if "─── Daily Games ───" in line:
            in_daily_section = True
            continue

        if in_daily_section:
            if "───" in line:
                break

            match = re.match(r"^[> ]+\s*\[\s*(.+?)\s*\]\s*(.*)$", line)
            if match:
                game_name = match.group(1).strip()
                payouts_str = match.group(2)

                difficulties = []
                for p_match in re.finditer(r"([✓✗])(\d+)", payouts_str):
                    chips = int(p_match.group(2))
                    start_idx = match.start(2) + p_match.start()

                    # Ensure we have color data for this cell
                    color = []
                    if (
                        hasattr(lines, "colors")
                        and row < len(lines.colors)
                        and start_idx < len(lines.colors[row])
                    ):
                        color = lines.colors[row][start_idx]

                    # Standard green is '32', 256-color is '2' (e.g., ['38', '5', '2'])
                    is_completed = ("2" in color and "38" in color) or ("32" in color)

                    difficulties.append({"chips": chips, "completed": is_completed})

                if difficulties:
                    arcade_games[game_name] = difficulties

    return arcade_games
