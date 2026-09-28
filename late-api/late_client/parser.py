import re


class ScreenLines(list):
    def __init__(self, lines, colors, fg_colors=None):
        super().__init__(lines)
        self.colors = colors
        self.fg_colors = fg_colors if fg_colors is not None else colors


class Vt100Screen:
    """
    An incremental VT100/ANSI terminal emulator.

    Feed bytes as they arrive with ``feed`` and read the current screen with
    ``lines``. Repeating this over a long-lived session stays O(new input)
    per call instead of O(entire history), so a bot that scrapes the screen in
    a tight loop does not slow down as the session output grows.

    An escape sequence split across two ``feed`` calls is held back until the
    next call completes it, so chunk boundaries never corrupt the screen.
    """

    def __init__(self, width=120, height=80):
        self.width = width
        self.height = height
        self.screen = [[' ' for _ in range(width)] for _ in range(height)]
        self.colors = [[[] for _ in range(width)] for _ in range(height)]
        self.fg_colors = [[None for _ in range(width)] for _ in range(height)]
        self.row = 0
        self.col = 0
        self.sgr_state = {'fg': None, 'bg': None, 'attrs': set()}
        self._pending = ''

    def feed(self, text):
        text = self._pending + text
        self._pending = ''

        width = self.width
        height = self.height
        screen = self.screen
        colors = self.colors
        fg_colors = self.fg_colors
        sgr_state = self.sgr_state
        row = self.row
        col = self.col
        i = 0

        while i < len(text):
            if text[i] == '\x1b':
                if i + 1 < len(text) and text[i + 1] == '[':
                    match = re.match(r'^\[([!-?]*)([ -/]*)([@-~])', text[i + 1:])
                    if match:
                        params_str = match.group(1)
                        params = params_str.split(';') if params_str else []
                        cmd = match.group(3)

                        if cmd == 'm':
                            if not params or params == ['0'] or params == ['']:
                                sgr_state['fg'] = None
                                sgr_state['bg'] = None
                                sgr_state['attrs'] = set()
                            else:
                                idx = 0
                                while idx < len(params):
                                    p = params[idx]
                                    if not p or p == '0':
                                        sgr_state['fg'] = None
                                        sgr_state['bg'] = None
                                        sgr_state['attrs'] = set()
                                    elif p == '38':
                                        if idx + 1 < len(params) and params[idx + 1] == '5' and idx + 2 < len(params):
                                            sgr_state['fg'] = params[idx:idx + 3]
                                            idx += 2
                                        elif idx + 1 < len(params) and params[idx + 1] == '2' and idx + 4 < len(params):
                                            sgr_state['fg'] = params[idx:idx + 5]
                                            idx += 4
                                        else:
                                            sgr_state['fg'] = [p]
                                    elif p == '48':
                                        if idx + 1 < len(params) and params[idx + 1] == '5' and idx + 2 < len(params):
                                            sgr_state['bg'] = params[idx:idx + 3]
                                            idx += 2
                                        elif idx + 1 < len(params) and params[idx + 1] == '2' and idx + 4 < len(params):
                                            sgr_state['bg'] = params[idx:idx + 5]
                                            idx += 4
                                        else:
                                            sgr_state['bg'] = [p]
                                    elif p == '39':
                                        sgr_state['fg'] = None
                                    elif p == '49':
                                        sgr_state['bg'] = None
                                    elif p.isdigit():
                                        val = int(p)
                                        if (30 <= val <= 37) or (90 <= val <= 97):
                                            sgr_state['fg'] = [p]
                                        elif (40 <= val <= 47) or (100 <= val <= 107):
                                            sgr_state['bg'] = [p]
                                    idx += 1
                        elif cmd == 'H' or cmd == 'f':
                            r = int(params[0]) if len(params) > 0 and params[0] else 1
                            c = int(params[1]) if len(params) > 1 and params[1] else 1
                            row = min(height - 1, max(0, r - 1))
                            col = min(width - 1, max(0, c - 1))
                        elif cmd == 'G':
                            c = int(params[0]) if len(params) > 0 and params[0] else 1
                            col = min(width - 1, max(0, c - 1))
                        elif cmd == 'd':
                            r = int(params[0]) if len(params) > 0 and params[0] else 1
                            row = min(height - 1, max(0, r - 1))
                        elif cmd == 'C':
                            c = int(params[0]) if len(params) > 0 and params[0] else 1
                            col = min(width - 1, col + c)
                        elif cmd == 'D':
                            c = int(params[0]) if len(params) > 0 and params[0] else 1
                            col = max(0, col - c)
                        elif cmd == 'B':
                            r = int(params[0]) if len(params) > 0 and params[0] else 1
                            row = min(height - 1, row + r)
                        elif cmd == 'A':
                            r = int(params[0]) if len(params) > 0 and params[0] else 1
                            row = max(0, row - r)
                        elif cmd == 'K':
                            mode = int(params[0]) if len(params) > 0 and params[0] else 0
                            if mode == 0:
                                for c_idx in range(col, width):
                                    screen[row][c_idx] = ' '
                            elif mode == 1:
                                for c_idx in range(0, col + 1):
                                    screen[row][c_idx] = ' '
                            elif mode == 2:
                                for c_idx in range(0, width):
                                    screen[row][c_idx] = ' '
                        elif cmd == 'J':
                            mode = int(params[0]) if len(params) > 0 and params[0] else 0
                            if mode == 2 or mode == 3:
                                for r_idx in range(height):
                                    for c_idx in range(width):
                                        screen[r_idx][c_idx] = ' '
                        elif cmd == 'X':
                            count = int(params[0]) if len(params) > 0 and params[0] else 1
                            for c_idx in range(col, min(width, col + count)):
                                screen[row][c_idx] = ' '

                        i += 1 + match.end()
                        continue
                    # ESC[ present but the CSI sequence is incomplete in this
                    # buffer (no command byte yet): hold it for the next call.
                    self._pending = text[i:]
                    break
                elif i + 1 >= len(text):
                    # Lone trailing ESC may be the start of a sequence that
                    # continues in the next buffer.
                    self._pending = text[i:]
                    break
                # else: ESC followed by a non-'[' char — drop the ESC byte and
                # fall through to the generic handler (pre-existing behavior).

            if text[i] == '\r':
                col = 0
            elif text[i] == '\n':
                row = min(height - 1, row + 1)
            elif text[i] == '\x08':
                col = max(0, col - 1)
            elif ord(text[i]) >= 32:
                if 0 <= row < height and 0 <= col < width:
                    screen[row][col] = text[i]
                    active_fg = sgr_state['fg'] or []
                    active_bg = sgr_state['bg'] or []
                    colors[row][col] = active_fg + active_bg
                    fg_colors[row][col] = sgr_state['fg']
                col += 1
            i += 1

        self.row = row
        self.col = col

    def lines(self):
        text_lines = ["".join(r).rstrip() for r in self.screen]
        return ScreenLines(text_lines, self.colors, self.fg_colors)


def parse_vt100(text, width=120, height=80):
    """
    Parse a full VT100 stream into the final screen state. Kept for one-shot
    callers; use :class:`Vt100Screen` for incremental parsing over a session.
    """
    vt = Vt100Screen(width, height)
    vt.feed(text)
    return vt.lines()
