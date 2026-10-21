"""Design tokens, typography, and small UX helpers for the CustomTkinter windows."""

from __future__ import annotations

import contextlib
import tkinter.font as tkfont
import tkinter.ttk as ttk
from dataclasses import dataclass
from typing import Any

PAPER = "#FDFCF8"
CARD = "#FFFEFA"
STONE = "#F0EBE5"
SAND = "#E6DCCD"
INK = "#2C2C24"
MUTED = "#66675C"
FAINT = "#8A8B7E"
MOSS = "#5D7052"
MOSS_DARK = "#4C5E43"
MOSS_SOFT = "#E3E8DE"
CLAY = "#C18C5D"
CLAY_DARK = "#9A6A3F"
BORDER = "#DED8CF"
DANGER = "#A85448"
DANGER_DARK = "#8F453B"
DANGER_SOFT = "#F8E9E5"
PALE = "#F3F4F1"
WHITE = "#FFFFFF"
FOCUS = INK

SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24
SPACE_2XL = 32

RADIUS_CARD = 28
RADIUS_PRIMARY = 26
RADIUS_SECONDARY = 22
RADIUS_SMALL = 14

ACTION_LABELS = {
    "play": "Play",
    "scan": "Scan track",
    "fix": "Choose video",
    "doctor": "Check setup",
}
STATUS_TEXT = {
    "play": "Playing",
    "scan": "Scanning this track",
    "doctor": "Checking setup",
}
PLAYER_NAMES = (
    ("spotify", "Spotify"),
    ("tidal", "TIDAL"),
    ("vlc", "VLC"),
    ("musicbee", "MusicBee"),
    ("browser", "Browser"),
)


@dataclass(frozen=True)
class Fonts:
    wordmark: tuple[str, int, str]
    display: tuple[str, int, str]
    title: tuple[str, int, str]
    body: tuple[str, int]
    body_bold: tuple[str, int, str]
    small: tuple[str, int]
    small_bold: tuple[str, int, str]
    mono: tuple[str, int]


def choose_family(
    available: set[str], candidates: tuple[str, ...], fallback: str = "Segoe UI"
) -> str:
    """The first preferred font family installed on this machine."""
    for candidate in candidates:
        if candidate in available:
            return candidate
    return fallback


def make_fonts(root: Any) -> Fonts:
    """Resolve the design's fonts against what this Windows install actually has."""
    available = set(tkfont.families(root))
    display = choose_family(available, ("Segoe UI Variable Display", "Segoe UI"))
    body = choose_family(available, ("Segoe UI Variable Text", "Segoe UI"))
    wordmark = choose_family(available, ("Georgia", "Segoe UI"))
    mono = choose_family(available, ("Cascadia Mono", "Consolas", "Courier New"))
    return Fonts(
        wordmark=(wordmark, 20, "bold"),
        display=(display, 30, "bold"),
        title=(display, 22, "bold"),
        body=(body, 13),
        body_bold=(body, 14, "bold"),
        small=(body, 12),
        small_bold=(body, 12, "bold"),
        mono=(mono, 11),
    )


def friendly_player_name(app_id: str) -> str:
    """A short player label instead of a raw SMTC application id."""
    if not app_id:
        return "Player"
    lowered = app_id.lower()
    for needle, name in PLAYER_NAMES:
        if needle in lowered:
            return name
    base = app_id.split("!", 1)[0]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base or "Player"


def player_option_label(session: Any) -> str:
    """The player picker's label: the app, not the track already shown above."""
    return friendly_player_name(session.app_id)


def action_label(action: str, *, stoppable: bool = False) -> str:
    """User-facing action copy, with Stop replacing Play while a session runs."""
    if action == "play" and stoppable:
        return "Stop"
    return ACTION_LABELS.get(action, action.title())


def status_text(action: str | None, *, locked: bool) -> str:
    """One short status phrase for the header."""
    if locked:
        return "Another session is running"
    if action:
        return STATUS_TEXT.get(action, action.title())
    return "Ready"


def status_color(tone: str) -> str:
    """The status dot and label color for an idle, busy, or locked window."""
    if tone == "busy":
        return MOSS
    if tone == "locked":
        return DANGER
    return MUTED


def next_focus_index(current: int, count: int, *, backwards: bool = False) -> int:
    """The next widget index in a wrapping Tab order, or -1 for an empty order."""
    if count <= 0:
        return -1
    if current < 0:
        return count - 1 if backwards else 0
    step = -1 if backwards else 1
    return (current + step) % count


def title_wraplength(card_width: int) -> int:
    """A readable title width that does not collapse beside the record mark."""
    return max(240, int(card_width) - 140)


def fit_scale(
    system_scale: float,
    screen_width: int,
    screen_height: int,
    *,
    design_width: int = 920,
    design_height: int = 640,
) -> float:
    """Use the monitor's DPI unless the window would not fit on the screen."""
    width_limit = screen_width * 0.9 / design_width
    height_limit = screen_height * 0.9 / design_height
    return round(max(0.85, min(system_scale, width_limit, height_limit)), 3)


def content_padding(
    width: int,
    *,
    max_width: int = 900,
    minimum: int = 28,
    scale: float = 1.0,
) -> int:
    """Side padding that keeps the desktop content from stretching indefinitely."""
    scaled_max = int(max_width * scale)
    scaled_minimum = int(minimum * scale)
    return max(scaled_minimum, (int(width) - scaled_max) // 2)


def button_style(kind: str) -> dict[str, Any]:
    """Shared CustomTkinter button options for the four button roles."""
    common: dict[str, Any] = {
        "border_width": 0,
        "text_color_disabled": FAINT,
    }
    if kind == "primary":
        return {
            **common,
            "height": 54,
            "corner_radius": RADIUS_PRIMARY,
            "fg_color": MOSS,
            "hover_color": MOSS_DARK,
            "text_color": PALE,
        }
    if kind == "danger":
        return {
            **common,
            "height": 54,
            "corner_radius": RADIUS_PRIMARY,
            "fg_color": DANGER,
            "hover_color": DANGER_DARK,
            "text_color": WHITE,
        }
    if kind == "secondary":
        return {
            **common,
            "height": 44,
            "corner_radius": RADIUS_SECONDARY,
            "fg_color": "transparent",
            "hover_color": STONE,
            "text_color": MOSS,
            "border_width": 1,
            "border_color": BORDER,
        }
    if kind == "hotkey":
        return {
            **common,
            "height": 34,
            "corner_radius": 17,
            "fg_color": STONE,
            "hover_color": SAND,
            "text_color": INK,
            "border_width": 1,
            "border_color": BORDER,
        }
    return {
        **common,
        "height": 32,
        "corner_radius": RADIUS_SMALL,
        "fg_color": "transparent",
        "hover_color": STONE,
        "text_color": MUTED,
    }


def configure_ttk(root: Any, fonts: Fonts | None = None) -> None:
    """Style the native controls that CustomTkinter does not replace."""
    fonts = fonts or make_fonts(root)
    style = ttk.Style(root)
    with contextlib.suppress(Exception):
        style.theme_use("clam")
    style.configure(
        "Player.TCombobox",
        fieldbackground=STONE,
        background=STONE,
        foreground=INK,
        arrowcolor=MUTED,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        padding=8,
        font=fonts.body,
    )
    style.map(
        "Player.TCombobox",
        fieldbackground=[("readonly", STONE)],
        foreground=[("readonly", INK)],
        selectbackground=[("readonly", STONE)],
        selectforeground=[("readonly", INK)],
        bordercolor=[("focus", MOSS)],
        lightcolor=[("focus", MOSS)],
        darkcolor=[("focus", MOSS)],
    )
    style.configure(
        "Results.Treeview",
        background=CARD,
        fieldbackground=CARD,
        foreground=INK,
        rowheight=44,
        borderwidth=0,
        font=fonts.body,
    )
    style.configure(
        "Results.Treeview.Heading",
        background=STONE,
        foreground=MUTED,
        borderwidth=0,
        relief="flat",
        font=fonts.small_bold,
        padding=(8, 10),
    )
    style.map(
        "Results.Treeview",
        background=[("selected", MOSS_SOFT)],
        foreground=[("selected", INK)],
    )
