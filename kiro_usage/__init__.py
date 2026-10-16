"""Kiro Usage Tracker — shared constants and utilities."""

import os, re, sqlite3, json, unicodedata, platform
from pathlib import Path

# ── Platform paths ────────────────────────────────────────────────────────────
if platform.system() == "Darwin":
    CLI_DB = Path.home() / "Library/Application Support/kiro-cli/data.sqlite3"
    IDE_DB = Path.home() / "Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/dev_data/devdata.sqlite"
else:
    CLI_DB = Path.home() / ".local/share/kiro-cli/data.sqlite3"
    IDE_DB = Path.home() / ".config/Kiro/User/globalStorage/kiro.kiroagent/dev_data/devdata.sqlite"

SESSIONS_DIR = Path.home() / ".kiro_sessions"
# Newer kiro-cli keeps transcripts + per-turn credits here instead of the SQLite DB.
CLI_SESSIONS_DIR = Path(os.environ.get("KIRO_CLI_SESSIONS_DIR") or
                        Path.home() / ".kiro" / "sessions" / "cli")
CHARS_PER_TOKEN = 4

# ── Pricing ($/MTok) — Anthropic 5-min cache write rate ──────────────────────
PRICING = {
    "claude-opus-4.6":   (6.25, 0.50, 25),
    "claude-opus-4.5":   (6.25, 0.50, 25),
    "claude-opus-4.1":   (18.75, 1.50, 75),
    "claude-opus-4":     (18.75, 1.50, 75),
    "claude-sonnet-4.6": (3.75, 0.30, 15),
    "claude-sonnet-4.5": (3.75, 0.30, 15),
    "claude-sonnet-4":   (3.75, 0.30, 15),
    "claude-haiku-4.5":  (1.25, 0.10, 5),
}
DEFAULT_PRICING = (6.25, 0.50, 25)  # Opus — most common model

def price_for(model_id):
    if not model_id:
        return DEFAULT_PRICING
    m = model_id.lower()
    for prefix, p in PRICING.items():
        if prefix in m:
            return p
    # Newer model versions (e.g. claude-sonnet-5.5) are not in the table:
    # assume they are priced like the latest known model of the same family.
    if "sonnet" in m:
        return PRICING["claude-sonnet-4.6"]
    if "opus" in m:
        return PRICING["claude-opus-4.6"]
    return DEFAULT_PRICING

def calc_cost(cw, cr, out, model_id=None):
    pw, pr, po = price_for(model_id)
    return (cw * pw + cr * pr + out * po) / 1_000_000

# ── ANSI ──────────────────────────────────────────────────────────────────────
C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "green": "\033[32m", "yellow": "\033[33m", "blue": "\033[34m",
    "cyan": "\033[36m", "red": "\033[31m", "magenta": "\033[35m",
    "white": "\033[97m", "gray": "\033[90m",
    "bg_cyan": "\033[46m", "bg_blue": "\033[44m", "bg_magenta": "\033[45m",
    "bg_green": "\033[42m", "bg_yellow": "\033[43m", "bg_red": "\033[41m",
    "underline": "\033[4m",
}
NO_COLOR = os.environ.get("NO_COLOR") is not None

def c(text, *styles):
    if NO_COLOR:
        return str(text)
    return "".join(C[s] for s in styles) + str(text) + C["reset"]

def fmt(n):
    if n >= 1_000_000: return "{:.1f}M".format(n / 1_000_000)
    if n >= 1_000: return "{:.1f}K".format(n / 1_000)
    return str(n)

def fmt_cost(v):
    if v >= 1: return "${:.2f}".format(v)
    if v >= 0.01: return "${:.3f}".format(v)
    return "${:.4f}".format(v)

def fmt_credits(v):
    if v >= 100: return "{:.0f}".format(v)
    if v >= 10: return "{:.1f}".format(v)
    if v >= 0.005: return "{:.2f}".format(v)
    return "0" if v == 0 else "<0.01"

def bar(pct, width=20):
    filled = int(pct / 100 * width)
    if pct < 40:
        return c("━" * filled, "green") + c("╌" * (width - filled), "dim")
    elif pct < 70:
        return c("━" * filled, "yellow") + c("╌" * (width - filled), "dim")
    else:
        return c("━" * filled, "red") + c("╌" * (width - filled), "dim")

def tw():
    try: return os.get_terminal_size().columns
    except: return 80

# ── Box drawing helpers ───────────────────────────────────────────────────────
_ansi_re = re.compile(r'\033\[[0-9;]*m')

def vlen(s):
    plain = _ansi_re.sub('', s)
    w = 0
    for ch in plain:
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in ('W', 'F') else 1
    return w

def vrpad(s, width):
    return s + " " * max(width - vlen(s), 0)

def vlpad(s, width):
    return " " * max(width - vlen(s), 0) + s

def vpad(s, width):
    return vrpad(s, width)

def box_top(title, width):
    inner = width - 2
    label = " {} ".format(title)
    lw = vlen(label)
    left = (inner - lw) // 2
    right = inner - lw - left
    return (c("╭", "dim") + c("─" * left, "dim") +
            c(label, "bold", "cyan") + c("─" * right, "dim") + c("╮", "dim"))

def box_bot(width):
    return c("╰" + "─" * (width - 2) + "╯", "dim")

def box_sep(width):
    return c("├" + "─" * (width - 2) + "┤", "dim")

def box_line(content, width):
    inner = width - 2
    return c("│", "dim") + vpad(" " + content, inner) + c("│", "dim")

# ── DB ────────────────────────────────────────────────────────────────────────
def query(db_path, sql, params=()):
    if not db_path.exists():
        return []
    conn = sqlite3.connect("file:{}?mode=ro".format(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
