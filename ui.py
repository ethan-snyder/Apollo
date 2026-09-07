"""
Apollo — terminal presentation layer.

ANSI theming, the block-gradient banner, and the drawing primitives the shell
in `terminal.py` composes. Kept separate from the shell so the look can change
without touching any control flow.

Colour handling:
  * 24-bit truecolour is used when the terminal supports it, with a 256-colour
    fallback and a plain-text fallback below that.
  * Windows 10+ needs VT processing switched on explicitly; `enable_ansi()`
    does that via the Win32 API and degrades silently on older builds.
  * Honours NO_COLOR (https://no-color.org) and a piped/redirected stdout.
"""
from __future__ import annotations

import os
import shutil
import sys

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
# The base is Caishen's gold. Everything else is derived from it so the theme
# stays coherent if this one value changes.

GOLD = (0xFF, 0xD7, 0x00)          # #FFD700

# The banner is drawn with block characters of decreasing ink coverage. Giving
# each one a proportionally dimmer shade of GOLD turns the glyph density into a
# real luminance gradient rather than flat text.
BLOCK_STEPS: dict[str, float] = {
    "█": 1.00,     # full block  — brightest, the true GOLD
    "▓": 0.72,     # dark shade
    "▒": 0.48,     # medium shade
    "░": 0.28,     # light shade
}

# Semantic colours
FG_TEXT = (0xD4, 0xD4, 0xD8)
FG_DIM = (0x7A, 0x7A, 0x85)
FG_FAINT = (0x4A, 0x4A, 0x55)
BULL = (0x3F, 0xD1, 0x7A)
BEAR = (0xF2, 0x5F, 0x5F)
NEUTRAL = (0x9A, 0x9A, 0xA5)
WARN = (0xFF, 0xA5, 0x2C)
INFO = (0x5A, 0xB8, 0xF0)

BANNER = r"""
 ░▒▓██████▓▒░░▒▓███████▓▒░ ░▒▓██████▓▒░░▒▓█▓▒░      ░▒▓█▓▒░      ░▒▓██████▓▒░
░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░      ░▒▓█▓▒░     ░▒▓█▓▒░░▒▓█▓▒░
░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░      ░▒▓█▓▒░     ░▒▓█▓▒░░▒▓█▓▒░
░▒▓████████▓▒░▒▓███████▓▒░░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░      ░▒▓█▓▒░     ░▒▓█▓▒░░▒▓█▓▒░
░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░      ░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░      ░▒▓█▓▒░     ░▒▓█▓▒░░▒▓█▓▒░
░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░      ░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░      ░▒▓█▓▒░     ░▒▓█▓▒░░▒▓█▓▒░
░▒▓█▓▒░░▒▓█▓▒░▒▓█▓▒░       ░▒▓██████▓▒░░▒▓████████▓▒░▒▓████████▓▒░▒▓██████▓▒░
"""

TAGLINE = "news aggregation  ·  signal generation  ·  BTC / ETH / SOL"

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------

def enable_ansi() -> bool:
    """
    Turn on VT processing on Windows. Returns True if ANSI should be used.

    Windows 10 build 10586+ supports ANSI but has it off by default for
    legacy console apps; ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004) switches
    it on. Non-Windows platforms need nothing.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("APOLLO_NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False                      # piped or redirected — stay plain

    if sys.platform != "win32":
        return True

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # -11 = STD_OUTPUT_HANDLE
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if kernel32.SetConsoleMode(handle, mode.value | 0x0004):
            return True
        return False
    except Exception:
        return False


ANSI = enable_ansi()
TRUECOLOR = ANSI and os.environ.get("COLORTERM", "").lower() in (
    "truecolor", "24bit") or (ANSI and sys.platform == "win32")


def _rgb_to_256(r: int, g: int, b: int) -> int:
    """Nearest xterm-256 index, using the 6x6x6 colour cube."""
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + round((r - 8) / 247 * 24)
    return (16 + 36 * round(r / 255 * 5)
            + 6 * round(g / 255 * 5) + round(b / 255 * 5))


def fg(rgb: tuple[int, int, int]) -> str:
    if not ANSI:
        return ""
    r, g, b = rgb
    if TRUECOLOR:
        return f"\033[38;2;{r};{g};{b}m"
    return f"\033[38;5;{_rgb_to_256(r, g, b)}m"


def c(text: str, rgb: tuple[int, int, int], bold: bool = False) -> str:
    if not ANSI:
        return text
    return f"{BOLD if bold else ''}{fg(rgb)}{text}{RESET}"


def shade(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return (max(0, min(255, int(rgb[0] * factor))),
            max(0, min(255, int(rgb[1] * factor))),
            max(0, min(255, int(rgb[2] * factor))))


def width() -> int:
    try:
        return max(60, min(shutil.get_terminal_size().columns, 120))
    except Exception:
        return 80


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def render_banner(center: bool = True) -> str:
    """
    Colour the banner by block density.

    Consecutive characters of the same density share one escape sequence,
    which keeps the output roughly 8x smaller than naively colouring every
    glyph — noticeable on slow terminals and over SSH.
    """
    lines = BANNER.strip("\n").split("\n")
    term_w = width()
    out: list[str] = []

    for line in lines:
        pad = " " * max(0, (term_w - len(line)) // 2) if center else ""
        if not ANSI:
            out.append(pad + line)
            continue

        buf: list[str] = [pad]
        current: str | None = None
        run: list[str] = []

        def flush() -> None:
            if run:
                buf.append((current or "") + "".join(run) + (RESET if current else ""))
                run.clear()

        for ch in line:
            step = BLOCK_STEPS.get(ch)
            col = fg(shade(GOLD, step)) if step is not None else None
            if col != current:
                flush()
                current = col
            run.append(ch)
        flush()
        out.append("".join(buf))

    return "\n".join(out)


def banner(subtitle: str = "") -> str:
    term_w = width()
    parts = ["", render_banner(), ""]
    tag = subtitle or TAGLINE
    parts.append(c(tag.center(term_w), shade(GOLD, 0.55)))
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------

def rule(char: str = "─", rgb: tuple[int, int, int] | None = None) -> str:
    return c(char * width(), rgb or shade(GOLD, 0.35))


def header(title: str, subtitle: str = "") -> str:
    w = width()
    bar = c("═" * w, shade(GOLD, 0.45))
    mid = c(f"  {title}", GOLD, bold=True)
    if subtitle:
        mid += c(f"   {subtitle}", FG_DIM)
    return f"\n{bar}\n{mid}\n{bar}"


def subrule() -> str:
    return c("  " + "─" * (width() - 4), shade(GOLD, 0.25))


def section(title: str) -> str:
    return "\n" + c(f"  {title}", GOLD, bold=True) + "\n" + subrule()


def kv(key: str, value: str, key_w: int = 22,
       value_rgb: tuple[int, int, int] | None = None) -> str:
    return (c(f"  {key:<{key_w}}", FG_DIM)
            + c(str(value), value_rgb or FG_TEXT))


def direction_color(direction: str) -> tuple[int, int, int]:
    d = (direction or "").upper()
    if "BULL" in d:
        return BULL
    if "BEAR" in d:
        return BEAR
    return NEUTRAL


def arrow(direction: str) -> str:
    return {
        "STRONG_BULLISH": "▲▲", "BULLISH": "▲ ",
        "NEUTRAL": "──", "BEARISH": "▼ ", "STRONG_BEARISH": "▼▼",
    }.get((direction or "").upper(), "· ")


def sparkbar(value: float, lo: float, hi: float, cells: int = 20) -> str:
    """
    Horizontal bar centred on zero when lo < 0 < hi.

    Used for signal strength, where the sign matters as much as the magnitude.
    """
    if hi <= lo:
        return " " * cells
    if lo < 0 < hi:
        mid = cells // 2
        span = max(abs(lo), abs(hi))
        n = int(round(abs(value) / span * mid)) if span else 0
        n = min(n, mid)
        if value >= 0:
            body = " " * mid + c("█" * n, BULL) + " " * (mid - n)
        else:
            body = " " * (mid - n) + c("█" * n, BEAR) + " " * mid
        return body
    frac = (value - lo) / (hi - lo)
    n = max(0, min(cells, int(round(frac * cells))))
    return c("█" * n, GOLD) + " " * (cells - n)


def clear() -> None:
    if ANSI:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    else:
        os.system("cls" if sys.platform == "win32" else "clear")


def ok(msg: str) -> str:
    return c("  ✓ ", BULL) + c(msg, FG_TEXT)


def fail(msg: str) -> str:
    return c("  ✗ ", BEAR) + c(msg, FG_TEXT)


def note(msg: str) -> str:
    return c("  · ", FG_FAINT) + c(msg, FG_DIM)


def warn(msg: str) -> str:
    return c("  ! ", WARN) + c(msg, FG_TEXT)


def prompt(label: str = "apollo") -> str:
    return (c("  ┌─ ", shade(GOLD, 0.4)) + c(label, GOLD, bold=True)
            + "\n" + c("  └─▸ ", shade(GOLD, 0.4)))
