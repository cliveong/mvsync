import importlib.metadata

from mvsync import gui_theme
from mvsync.player_smtc import PlayerSession


def contrast_ratio(foreground: str, background: str) -> float:
    def channel(value: int) -> float:
        normalized = value / 255
        if normalized <= 0.04045:
            return normalized / 12.92
        return ((normalized + 0.055) / 1.055) ** 2.4

    def luminance(color: str) -> float:
        raw = color.lstrip("#")
        red, green, blue = (int(raw[index : index + 2], 16) for index in (0, 2, 4))
        return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_customtkinter_is_installed():
    parts = importlib.metadata.version("customtkinter").split(".")[:2]
    assert tuple(int(part) for part in parts) >= (6, 0)


def test_choose_family_prefers_the_first_available_candidate():
    available = {"Segoe UI", "Segoe UI Variable Display"}
    assert (
        gui_theme.choose_family(available, ("Segoe UI Variable Display", "Segoe UI"))
        == "Segoe UI Variable Display"
    )


def test_choose_family_falls_back_when_no_candidate_exists():
    assert gui_theme.choose_family({"Arial"}, ("Segoe UI Variable Display",)) == "Segoe UI"


def test_friendly_player_name_shortens_known_apps():
    assert gui_theme.friendly_player_name("SpotifyAB!Spotify") == "Spotify"
    assert gui_theme.friendly_player_name("vlc.exe") == "VLC"
    assert gui_theme.friendly_player_name("MusicBee.exe") == "MusicBee"


def test_friendly_player_name_falls_back_to_the_app_id():
    assert gui_theme.friendly_player_name("Contoso.Player!App") == "Contoso.Player"
    assert gui_theme.friendly_player_name("") == "Player"


def test_player_option_label_uses_the_friendly_name():
    session = PlayerSession("SpotifyAB!Spotify", "Song", "Artist", True)
    assert gui_theme.player_option_label(session) == "Spotify"


def test_action_label_uses_plain_language():
    assert gui_theme.action_label("play") == "Play"
    assert gui_theme.action_label("play", stoppable=True) == "Stop"
    assert gui_theme.action_label("scan") == "Scan track"
    assert gui_theme.action_label("fix") == "Choose video"
    assert gui_theme.action_label("doctor") == "Check setup"


def test_status_text_covers_idle_busy_and_locked():
    assert gui_theme.status_text(None, locked=False) == "Ready"
    assert gui_theme.status_text("play", locked=False) == "Playing"
    assert gui_theme.status_text("scan", locked=False) == "Scanning this track"

    assert gui_theme.status_text("doctor", locked=False) == "Checking setup"
    assert gui_theme.status_text(None, locked=True) == "Another session is running"


def test_status_color_distinguishes_idle_busy_and_locked():
    assert gui_theme.status_color("idle") == gui_theme.MUTED
    assert gui_theme.status_color("busy") == gui_theme.MOSS
    assert gui_theme.status_color("locked") == gui_theme.DANGER


def test_next_focus_index_wraps_in_both_directions():
    assert gui_theme.next_focus_index(-1, 3) == 0
    assert gui_theme.next_focus_index(2, 3) == 0
    assert gui_theme.next_focus_index(0, 3, backwards=True) == 2
    assert gui_theme.next_focus_index(-1, 0) == -1


def test_title_wraplength_never_drops_below_the_readable_floor():
    assert gui_theme.title_wraplength(920) == 780
    assert gui_theme.title_wraplength(300) == 240


def test_text_tokens_meet_aa_contrast():
    pairs = (
        (gui_theme.INK, gui_theme.PAPER),
        (gui_theme.INK, gui_theme.CARD),
        (gui_theme.MUTED, gui_theme.PAPER),
        (gui_theme.MUTED, gui_theme.STONE),
        (gui_theme.PALE, gui_theme.MOSS),
        (gui_theme.WHITE, gui_theme.DANGER),
        (gui_theme.CLAY_DARK, gui_theme.PAPER),
    )
    for foreground, background in pairs:
        assert contrast_ratio(foreground, background) >= 4.5, (foreground, background)


def test_button_styles_keep_the_primary_action_large_and_moss():
    primary = gui_theme.button_style("primary")
    assert primary["height"] == 54
    assert primary["fg_color"] == gui_theme.MOSS
    assert primary["text_color"] == gui_theme.PALE

    danger = gui_theme.button_style("danger")
    assert danger["fg_color"] == gui_theme.DANGER
    assert danger["text_color"] == gui_theme.WHITE


def test_fit_scale_uses_the_system_dpi_while_keeping_the_window_on_screen():
    assert gui_theme.fit_scale(1.75, 1024, 768) == 1.002
    assert gui_theme.fit_scale(1.25, 1920, 1080) == 1.25
    assert gui_theme.fit_scale(1.75, 3840, 2160) == 1.75
    assert gui_theme.fit_scale(2.0, 3840, 2160) == 2.0
    assert gui_theme.fit_scale(0.8, 1920, 1080) == 0.85


def test_content_padding_caps_the_desktop_width_and_keeps_a_margin():
    assert gui_theme.content_padding(600) == 28
    assert gui_theme.content_padding(1174) == 137
    assert gui_theme.content_padding(1920) == 510
    assert gui_theme.content_padding(1174, max_width=1040) == 67


def test_content_padding_scales_the_cap_with_the_monitor():
    assert gui_theme.content_padding(1620, scale=1.735) == 48
    assert gui_theme.content_padding(3840, max_width=1040, scale=1.75) == 1010


def test_hotkey_button_style_looks_like_a_clickable_chip():
    style = gui_theme.button_style("hotkey")
    assert style["fg_color"] == gui_theme.STONE
    assert style["hover_color"] == gui_theme.SAND
    assert style["border_width"] == 1
    assert style["text_color"] == gui_theme.INK
