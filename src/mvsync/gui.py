"""Tkinter windows: the everyday main window, plus the MV picker behind Fix MV."""

from __future__ import annotations

import contextlib
import os
import queue
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from functools import partial
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk

from mvsync import config, devlog, gui_theme, paths, session, video_choice
from mvsync.align_store import load_alignment, load_newest_alignment, make_track_id
from mvsync.gui_console import KeySource, LogSink
from mvsync.mv_audio import parse_video_id
from mvsync.mv_search import Candidate, pick_mv_candidates, prioritize, video_url
from mvsync.player_process import guess_player_exe
from mvsync.player_smtc import PlayerBridge, PlayerSession, PlayerState
from mvsync.scan_runtime import ScanAborted, ScanResult, scan_with_candidates

ctk.set_appearance_mode("light")
_SCALING_CONFIGURED = False
_UI_SCALE = 1.0


def configure_ctk_scaling(root: ctk.CTk) -> float:
    """Use the monitor's DPI scaling unless the designed window would not fit."""
    global _SCALING_CONFIGURED, _UI_SCALE
    if _SCALING_CONFIGURED:
        return _UI_SCALE
    dpi_scale = ctk.ScalingTracker.get_window_scaling(root)
    desired = gui_theme.fit_scale(
        dpi_scale, root.winfo_screenwidth(), root.winfo_screenheight()
    )
    factor = desired / dpi_scale if dpi_scale else 1.0
    ctk.set_window_scaling(factor)
    ctk.set_widget_scaling(factor)
    _UI_SCALE = desired
    _SCALING_CONFIGURED = True
    return desired


MAIN_ACTIONS = ("play", "scan", "fix", "doctor")
HOTKEYS_BY_KEYSYM = {"q": "q", "r": "r", "bracketleft": "[", "bracketright": "]", "p": "p"}
HOTKEY_HINTS = {"q": "Stop", "r": "Resync", "[": "Trim -", "]": "Trim +", "p": "Player"}
CLOSE_STOP_TIMEOUT_S = 10.0
LOG_PUMP_MS = 250
TRACK_REFRESH_MS = 5000


def enabled_actions(
    busy: bool, locked: bool, *, has_track: bool = True
) -> tuple[str, ...]:
    """The actions the window offers: none while it works or the lock is held."""
    if busy or locked:
        return ()
    if not has_track:
        return ("doctor",)
    return MAIN_ACTIONS


def lock_status(pid: int) -> str:
    """The status line for a window another session has locked out of play."""
    return f"Another session is running (pid {pid}). Press Refresh to re-check."


def append_log(widget: tk.Text | ctk.CTkTextbox, lines: list[str]) -> None:
    """Append whole lines to a read-only text pane and scroll to the newest."""
    widget.configure(state="normal")
    for line in lines:
        widget.insert("end", line)
    widget.see("end")
    widget.configure(state="disabled")


def sync_badge_text(track_id: str | None, root: Path | None = None) -> tuple[str, str]:
    """What the next play will do: synced, rough, or still on its first listen."""
    if track_id is None:
        return "", "idle"
    store = paths.alignments_dir(root)
    choice = video_choice.load_video_choice(track_id, root)
    if choice is not None:
        if load_alignment(store, choice, track_id) is not None:
            return "Synced", "synced"
        return "Rough, refining", "rough"
    if load_newest_alignment(store, track_id) is not None:
        return "Synced", "synced"
    return "First listen, refining", "rough"


def drain_lines(sink: LogSink | None) -> list[str]:
    """The complete lines a worker has printed so far, taken out of its buffer."""
    return sink.drain() if sink is not None else []


def format_length(seconds: float | None) -> str:
    """Minutes and seconds, or "?" for an unknown duration."""
    if seconds is None:
        return "?"
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def now_playing_values(state: PlayerState | None) -> tuple[str, str, str]:
    """Title, artist, and a short state line for the now-playing header."""
    if state is None:
        return (
            "Nothing playing yet",
            "Start a song in Spotify, then press Play.",
            "No active player",
        )
    title = state.title or "Unknown title"
    artist = state.artist or "Unknown artist"
    playback = "Playing" if state.playing else "Paused"
    return title, artist, f"{format_length(state.duration_s)} · {playback}"


def status_tone(*, busy: bool, locked: bool) -> str:
    """The status chip tone: busy wins over another session's lock."""
    if busy:
        return "busy"
    if locked:
        return "locked"
    return "idle"


def hotkey_hint(key: str) -> str:
    """A human label for one playback hotkey."""
    return HOTKEY_HINTS[key]


def player_index(sessions: list[PlayerSession], match: str) -> int | None:
    """The configured player's index, else the first playing session, else zero."""
    if not sessions:
        return None
    if match:
        wanted = match.lower()
        for index, session in enumerate(sessions):
            if wanted in session.app_id.lower():
                return index
        return None
    for index, session in enumerate(sessions):
        if session.playing:
            return index
    return 0


def candidate_row_values(
    rank: int, candidate: Candidate, in_use_video_id: str | None
) -> tuple[str, str, str, str, str]:
    """One Treeview row: number, title, channel, length, in-use status."""
    status = "Current" if candidate.video_id == in_use_video_id else ""
    return (
        str(rank),
        candidate.title,
        candidate.channel,
        format_length(candidate.duration_s),
        status,
    )


def close_question(action: str | None) -> str:
    """What closing really does: only a running play is stopped, the rest is abandoned."""
    if action == "play":
        return "Play is running; closing now stops it. Close anyway?"
    return f"A {action or 'session'} is running; closing now abandons it part-way. Close anyway?"


def hotkey_chip(
    parent: tk.Misc,
    key: str,
    label: str,
    command: Callable[[str], None],
    fonts: gui_theme.Fonts,
    scale: float,
) -> ctk.CTkFrame:
    """A clickable keycap plus action label, so the shortcut reads as a key."""
    style = gui_theme.button_style("hotkey")
    chip = ctk.CTkFrame(
        parent,
        fg_color=style["fg_color"],
        corner_radius=style["corner_radius"],
        border_width=style["border_width"],
        border_color=style["border_color"],
        cursor="hand2",
    )
    keycap = ctk.CTkLabel(
        chip,
        text=key,
        width=int(24 * scale),
        height=int(22 * scale),
        fg_color=gui_theme.CARD,
        text_color=style["text_color"],
        corner_radius=max(4, int(6 * scale)),
        border_width=1,
        border_color=gui_theme.BORDER,
        font=fonts.small_bold,
        cursor="hand2",
    )
    keycap.pack(
        side="left", padx=(int(7 * scale), int(5 * scale)), pady=int(5 * scale)
    )
    label_widget = ctk.CTkLabel(
        chip,
        text=label,
        text_color=style["text_color"],
        font=fonts.small,
        cursor="hand2",
    )
    label_widget.pack(side="left", padx=(0, int(9 * scale)))

    def enter(_event: tk.Event) -> None:
        chip.configure(fg_color=style["hover_color"])

    def leave(_event: tk.Event) -> None:
        chip.configure(fg_color=style["fg_color"])

    def click(_event: tk.Event) -> None:
        command(key)

    for widget in (chip, keycap, label_widget):
        widget.bind("<Button-1>", click)
        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)
    return chip


class MainWindow(ctk.CTk):
    """Everyday window: play, scan, fix, and doctor over one live log pane."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self._ui_scale = configure_ctk_scaling(self)
        self.title("mvsync")
        self.minsize(740, 600)
        with contextlib.suppress(tk.TclError):
            self.geometry("920x640")
        self._root_dir = root
        self._fonts = gui_theme.make_fonts(self)
        self._state: PlayerState | None = None
        self._players: list[PlayerSession] = []
        self._keys = KeySource()
        self._play_stop = threading.Event()
        self._busy = False
        self._locked = False
        self._lock_pid = 0
        self._closing = False
        self._action: str | None = None
        self._sink: LogSink | None = None
        self._pump_after: str | None = None
        self._refresh_after: str | None = None
        self._worker: threading.Thread | None = None
        self._buttons: dict[str, ctk.CTkButton] = {}
        self._key_consumers: tuple[tk.Misc, ...] = ()
        self._notice_kind: str | None = None
        self._video_switches: queue.Queue | None = None
        self._picker_open = False
        self._picker_track_id: str | None = None
        self._picker_candidates: list[Candidate] = []
        self._picker_current_video: str | None = None
        self._picker_busy = False
        self._url_revealed = False
        self._focus_index = -1
        self._build_ui()
        self._wire_keyboard()
        self._set_icon()
        log_path = devlog.configure(root)
        if log_path is not None:
            self._log(f"[log] writing {log_path}")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self.refresh_track)
        self._refresh_after = self.after(TRACK_REFRESH_MS, self._auto_refresh)
        self.refresh_players()

    def _build_ui(self) -> None:
        gui_theme.configure_ttk(self, self._fonts)
        self.configure(fg_color=gui_theme.PAPER)
        self.bind("<Configure>", self._resize_shell, add="+")

        def record_mark(parent: tk.Misc, size: int, fill: str, hole: str) -> ctk.CTkFrame:
            mark = ctk.CTkFrame(
                parent,
                width=size,
                height=size,
                corner_radius=size // 2,
                fg_color=fill,
            )
            mark.pack_propagate(False)
            centre = ctk.CTkFrame(
                mark,
                width=max(8, size // 5),
                height=max(8, size // 5),
                corner_radius=max(4, size // 10),
                fg_color=hole,
            )
            centre.place(relx=0.5, rely=0.5, anchor="center")
            return mark

        def ghost(text: str, command: Callable[[], None]) -> ctk.CTkButton:
            return ctk.CTkButton(
                self,
                text=text,
                command=command,
                font=self._fonts.small,
                **gui_theme.button_style("ghost"),
            )

        shell = ctk.CTkFrame(self, fg_color="transparent")
        shell.pack(fill="both", expand=True, padx=28, pady=24)
        self._shell = shell

        self._notice = ctk.CTkFrame(
            shell,
            fg_color=gui_theme.DANGER_SOFT,
            corner_radius=16,
            border_width=1,
            border_color=gui_theme.BORDER,
        )
        notice_inner = ctk.CTkFrame(self._notice, fg_color="transparent")
        notice_inner.pack(fill="x", padx=16, pady=10)
        self._notice_label = ctk.CTkLabel(
            notice_inner,
            text="",
            font=self._fonts.small,
            text_color=gui_theme.INK,
            anchor="w",
            justify="left",
            wraplength=680,
        )
        self._notice_label.pack(side="left", fill="x", expand=True)
        dismiss_style = gui_theme.button_style("ghost")
        dismiss_style["height"] = 26
        ctk.CTkButton(
            notice_inner,
            text="Dismiss",
            command=self._clear_notice,
            width=72,
            font=self._fonts.small,
            **dismiss_style,
        ).pack(side="right", padx=(12, 0))

        views = ctk.CTkFrame(shell, fg_color="transparent")
        views.pack(fill="both", expand=True)
        self._now_view = ctk.CTkFrame(views, fg_color="transparent")
        self._picker_view = ctk.CTkFrame(views, fg_color="transparent")

        header = ctk.CTkFrame(self._now_view, fg_color="transparent")
        header.pack(fill="x")
        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.pack(side="left")
        record_mark(brand, 34, gui_theme.MOSS, gui_theme.PAPER).pack(side="left")
        ctk.CTkLabel(
            brand,
            text="mvsync",
            font=self._fonts.wordmark,
            text_color=gui_theme.INK,
        ).pack(side="left", padx=(10, 0))

        status = ctk.CTkFrame(header, fg_color="transparent")
        status.pack(side="right")
        self._status_dot = ctk.CTkFrame(
            status,
            width=8,
            height=8,
            corner_radius=4,
            fg_color=gui_theme.MUTED,
        )
        self._status_dot.pack(side="left", padx=(0, 8))
        self._status_label = ctk.CTkLabel(
            status,
            text="Ready",
            font=self._fonts.small,
            text_color=gui_theme.MUTED,
        )
        self._status_label.pack(side="left")
        self._progress = ctk.CTkProgressBar(
            status,
            mode="indeterminate",
            width=96,
            height=4,
            fg_color=gui_theme.STONE,
            progress_color=gui_theme.MOSS,
        )

        hero = ctk.CTkFrame(
            self._now_view,
            fg_color=gui_theme.CARD,
            corner_radius=gui_theme.RADIUS_CARD,
            border_width=1,
            border_color=gui_theme.BORDER,
        )
        hero.pack(fill="x", pady=(18, 12))
        hero_inner = ctk.CTkFrame(hero, fg_color="transparent")
        hero_inner.pack(fill="x", padx=26, pady=24)
        hero_top = ctk.CTkFrame(hero_inner, fg_color="transparent")
        hero_top.pack(fill="x")
        record_mark(hero_top, 58, gui_theme.MOSS, gui_theme.CARD).pack(
            side="left", padx=(0, 18), anchor="n"
        )
        now_info = ctk.CTkFrame(hero_top, fg_color="transparent")
        now_info.pack(side="left", fill="x", expand=True)
        kicker = ctk.CTkFrame(now_info, fg_color="transparent")
        kicker.pack(fill="x")
        ctk.CTkLabel(
            kicker,
            text="NOW PLAYING",
            font=self._fonts.small_bold,
            text_color=gui_theme.MUTED,
            anchor="w",
        ).pack(side="left")
        self._sync_badge = ctk.CTkLabel(
            kicker,
            text="",
            font=self._fonts.small_bold,
            text_color=gui_theme.CLAY_DARK,
            fg_color=gui_theme.STONE,
            corner_radius=12,
            padx=int(10 * self._ui_scale),
            pady=int(3 * self._ui_scale),
        )
        self._sync_badge.pack(side="right")
        self._now_title = ctk.CTkLabel(
            now_info,
            text="Nothing playing yet",
            font=self._fonts.display,
            text_color=gui_theme.INK,
            anchor="w",
            justify="left",
            wraplength=560,
        )
        self._now_title.pack(anchor="w", pady=(5, 0))
        self._now_artist = ctk.CTkLabel(
            now_info,
            text="Start a song in Spotify, then press Play.",
            font=self._fonts.body,
            text_color=gui_theme.MOSS,
            anchor="w",
        )
        self._now_artist.pack(anchor="w", pady=(4, 0))
        self._now_meta = ctk.CTkLabel(
            now_info,
            text="No active player",
            font=self._fonts.small,
            text_color=gui_theme.MUTED,
            anchor="w",
        )
        self._now_meta.pack(anchor="w", pady=(8, 0))
        hero.bind("<Configure>", self._resize_hero)

        actions = ctk.CTkFrame(self._now_view, fg_color="transparent")
        actions.pack(fill="x")
        actions.grid_columnconfigure(1, weight=1)
        actions.grid_columnconfigure(2, weight=1)
        play = ctk.CTkButton(
            actions,
            text="Play",
            width=240,
            command=partial(self._dispatch, "play"),
            font=self._fonts.body_bold,
            **gui_theme.button_style("primary"),
        )
        play.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._buttons["play"] = play

        scan = ctk.CTkButton(
            actions,
            text="Scan track",
            command=partial(self._dispatch, "scan"),
            font=self._fonts.body_bold,
            **gui_theme.button_style("secondary"),
        )
        scan.grid(row=0, column=1, sticky="ew", padx=(0, 4))
        fix = ctk.CTkButton(
            actions,
            text="Choose video",
            command=partial(self._dispatch, "fix"),
            font=self._fonts.body_bold,
            **gui_theme.button_style("secondary"),
        )
        fix.grid(row=0, column=2, sticky="ew", padx=(4, 0))
        self._buttons["scan"] = scan
        self._buttons["fix"] = fix

        player_row = ctk.CTkFrame(self._now_view, fg_color="transparent")
        player_row.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(
            player_row,
            text="Player",
            font=self._fonts.small,
            text_color=gui_theme.MUTED,
        ).pack(side="left")
        self._player_var = tk.StringVar()
        self._player_box = ttk.Combobox(
            player_row,
            textvariable=self._player_var,
            state="readonly",
            style="Player.TCombobox",
            height=10,
        )
        self._player_box.pack(side="left", fill="x", expand=True, padx=(10, 10))
        self._player_box.bind("<<ComboboxSelected>>", lambda event: self._select_player())
        self._refresh_button = ctk.CTkButton(
            player_row,
            text="Refresh",
            command=self.refresh_all,
            font=self._fonts.small,
            **gui_theme.button_style("ghost"),
        )
        self._refresh_button.pack(side="right")

        self._url_toggle = ctk.CTkButton(
            self._now_view,
            text="Use a specific video",
            command=self._toggle_url,
            font=self._fonts.small,
            **gui_theme.button_style("ghost"),
        )
        self._url_toggle.pack(anchor="w", pady=(6, 0))
        self._url_panel = ctk.CTkFrame(
            self._now_view,
            fg_color=gui_theme.STONE,
            corner_radius=18,
            border_width=1,
            border_color=gui_theme.BORDER,
        )
        url_inner = ctk.CTkFrame(self._url_panel, fg_color="transparent")
        url_inner.pack(fill="x", padx=12, pady=10)
        self._url_var = tk.StringVar()
        self._url_entry = tk.Entry(
            url_inner,
            textvariable=self._url_var,
            bg=gui_theme.STONE,
            fg=gui_theme.INK,
            insertbackground=gui_theme.INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=gui_theme.BORDER,
            highlightcolor=gui_theme.MOSS,
            font=self._fonts.body,
        )
        self._url_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self._url_entry.bind("<Return>", lambda event: self.use_url())
        self._key_consumers = (self._url_entry,)
        self._url_scan_button = ctk.CTkButton(
            url_inner,
            text="Scan this video",
            command=self.use_url,
            font=self._fonts.small_bold,
            **gui_theme.button_style("secondary"),
        )
        self._url_scan_button.pack(side="right", padx=(10, 0))

        self._hotkey_row = ctk.CTkFrame(self._now_view, fg_color="transparent")
        ctk.CTkLabel(
            self._hotkey_row,
            text="Controls",
            font=self._fonts.small,
            text_color=gui_theme.MUTED,
        ).pack(side="left", padx=(0, int(10 * self._ui_scale)))
        for key in HOTKEYS_BY_KEYSYM.values():
            hotkey_chip(
                self._hotkey_row,
                key,
                hotkey_hint(key),
                self._push_key,
                self._fonts,
                self._ui_scale,
            ).pack(side="left", padx=(0, int(5 * self._ui_scale)))

        picker_header = ctk.CTkFrame(self._picker_view, fg_color="transparent")
        picker_header.pack(fill="x")
        self._picker_back = ctk.CTkButton(
            picker_header,
            text="Back",
            command=self._close_picker,
            font=self._fonts.small_bold,
            width=70,
            **gui_theme.button_style("ghost"),
        )
        self._picker_back.pack(side="left")
        ctk.CTkLabel(
            picker_header,
            text="Choose a video",
            font=self._fonts.title,
            text_color=gui_theme.INK,
        ).pack(side="left", padx=(14, 0))
        self._picker_status = ctk.CTkLabel(
            picker_header,
            text="",
            font=self._fonts.small,
            text_color=gui_theme.MUTED,
        )
        self._picker_status.pack(side="right")

        track_card = ctk.CTkFrame(
            self._picker_view,
            fg_color=gui_theme.CARD,
            corner_radius=gui_theme.RADIUS_CARD,
            border_width=1,
            border_color=gui_theme.BORDER,
        )
        track_card.pack(fill="x", pady=(18, 12))
        track_inner = ctk.CTkFrame(track_card, fg_color="transparent")
        track_inner.pack(fill="x", padx=24, pady=20)
        track_text = ctk.CTkFrame(track_inner, fg_color="transparent")
        track_text.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            track_text,
            text="CURRENT TRACK",
            font=self._fonts.small_bold,
            text_color=gui_theme.MUTED,
            anchor="w",
        ).pack(anchor="w")
        self._picker_track_label = ctk.CTkLabel(
            track_text,
            text="No player session",
            font=self._fonts.title,
            text_color=gui_theme.INK,
            anchor="w",
            justify="left",
            wraplength=560,
        )
        self._picker_track_label.pack(anchor="w", pady=(6, 0))
        self._picker_search_button = ctk.CTkButton(
            track_inner,
            text="Search videos",
            command=self._search_picker,
            font=self._fonts.body_bold,
            **gui_theme.button_style("primary"),
        )
        self._picker_search_button.pack(side="right", padx=(16, 0))

        table_card = ctk.CTkFrame(
            self._picker_view,
            fg_color=gui_theme.CARD,
            corner_radius=gui_theme.RADIUS_CARD,
            border_width=1,
            border_color=gui_theme.BORDER,
        )
        table_card.pack(fill="both", expand=True)
        table_inner = ctk.CTkFrame(table_card, fg_color="transparent")
        table_inner.pack(fill="both", expand=True, padx=16, pady=14)
        columns = ("#", "mv", "channel", "length", "status")
        self._picker_tree = ttk.Treeview(
            table_inner,
            columns=columns,
            show="headings",
            height=8,
            style="Results.Treeview",
            selectmode="browse",
        )
        for column, heading, width, minimum, stretch in (
            ("#", "#", 44, 40, False),
            ("mv", "Video", 360, 220, True),
            ("channel", "Channel", 190, 120, True),
            ("length", "Length", 70, 60, False),
            ("status", "Status", 90, 80, False),
        ):
            self._picker_tree.heading(column, text=heading, anchor="w")
            self._picker_tree.column(
                column, width=width, minwidth=minimum, stretch=stretch, anchor="w"
            )
        self._picker_tree.tag_configure("in_use", foreground=gui_theme.MOSS)
        self._picker_tree.pack(side="left", fill="both", expand=True)
        picker_scroll = ttk.Scrollbar(
            table_inner, orient="vertical", command=self._picker_tree.yview
        )
        self._picker_tree.configure(yscrollcommand=picker_scroll.set)
        picker_scroll.pack(side="right", fill="y")
        self._picker_empty = ctk.CTkLabel(
            table_inner,
            text="Search to see videos for this track",
            font=self._fonts.body,
            text_color=gui_theme.MUTED,
        )
        self._picker_empty.place(relx=0.5, rely=0.5, anchor="center")
        self._picker_tree.bind("<<TreeviewSelect>>", lambda event: self._update_picker_buttons())
        self._picker_tree.bind("<Double-1>", lambda event: self.open_selected())
        self._picker_tree.bind("<Return>", lambda event: self._use_selected())

        picker_link_card = ctk.CTkFrame(
            self._picker_view,
            fg_color=gui_theme.STONE,
            corner_radius=18,
            border_width=1,
            border_color=gui_theme.BORDER,
        )
        picker_link_card.pack(fill="x", pady=(12, 0))
        picker_link_inner = ctk.CTkFrame(picker_link_card, fg_color="transparent")
        picker_link_inner.pack(fill="x", padx=12, pady=10)
        ctk.CTkLabel(
            picker_link_inner,
            text="Use a specific video",
            font=self._fonts.small,
            text_color=gui_theme.MUTED,
        ).pack(side="left", padx=(0, 10))
        self._picker_link_var = tk.StringVar()
        self._picker_link_entry = tk.Entry(
            picker_link_inner,
            textvariable=self._picker_link_var,
            bg=gui_theme.STONE,
            fg=gui_theme.INK,
            insertbackground=gui_theme.INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=gui_theme.BORDER,
            highlightcolor=gui_theme.MOSS,
            font=self._fonts.body,
        )
        self._picker_link_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self._picker_link_entry.bind("<Return>", lambda event: self.use_link())
        self._key_consumers = (*self._key_consumers, self._picker_link_entry)
        self._picker_link_button = ctk.CTkButton(
            picker_link_inner,
            text="Use link",
            command=self.use_link,
            font=self._fonts.small_bold,
            **gui_theme.button_style("secondary"),
        )
        self._picker_link_button.pack(side="right", padx=(10, 0))

        picker_footer = ctk.CTkFrame(self._picker_view, fg_color="transparent")
        picker_footer.pack(fill="x", pady=(12, 0))
        self._picker_use = ctk.CTkButton(
            picker_footer,
            text="Use this video",
            command=self._use_selected,
            font=self._fonts.body_bold,
            **gui_theme.button_style("primary"),
        )
        self._picker_use.pack(side="left")
        self._picker_open_button = ctk.CTkButton(
            picker_footer,
            text="Open in browser",
            command=self.open_selected,
            font=self._fonts.small_bold,
            **gui_theme.button_style("secondary"),
        )
        self._picker_open_button.pack(side="left", padx=(8, 0))
        self._picker_copy_button = ctk.CTkButton(
            picker_footer,
            text="Copy link",
            command=self.copy_selected,
            font=self._fonts.small_bold,
            **gui_theme.button_style("secondary"),
        )
        self._picker_copy_button.pack(side="left", padx=(8, 0))

        activity = ctk.CTkFrame(
            shell,
            fg_color=gui_theme.CARD,
            corner_radius=20,
            border_width=1,
            border_color=gui_theme.BORDER,
        )
        activity.pack(fill="x", pady=(12, 0))
        self._activity_card = activity
        activity_header = ctk.CTkFrame(activity, fg_color="transparent")
        activity_header.pack(fill="x", padx=16, pady=(10, 0))
        self._log_toggle = ctk.CTkButton(
            activity_header,
            text="Activity",
            width=110,
            command=self._toggle_log,
            font=self._fonts.small_bold,
            **gui_theme.button_style("ghost"),
        )
        self._log_toggle.pack(side="left")
        doctor = ctk.CTkButton(
            activity_header,
            text="Check setup",
            width=100,
            command=partial(self._dispatch, "doctor"),
            font=self._fonts.small,
            **gui_theme.button_style("ghost"),
        )
        doctor.pack(side="right")
        self._buttons["doctor"] = doctor
        ctk.CTkButton(
            activity_header,
            text="Open data",
            width=90,
            command=self._open_data_dir,
            font=self._fonts.small,
            **gui_theme.button_style("ghost"),
        ).pack(side="right", padx=(0, 4))
        ctk.CTkButton(
            activity_header,
            text="Copy log",
            width=85,
            command=self._copy_log,
            font=self._fonts.small,
            **gui_theme.button_style("ghost"),
        ).pack(side="right", padx=(0, 4))

        self._log_body = ctk.CTkFrame(activity, fg_color="transparent")
        self._log_text = ctk.CTkTextbox(
            self._log_body,
            height=150,
            state="disabled",
            wrap="word",
            fg_color=gui_theme.STONE,
            text_color=gui_theme.INK,
            corner_radius=16,
            border_width=0,
            font=self._fonts.mono,
        )
        self._log_text.pack(fill="both", expand=True)

        self._show_now_view()
        self._log("[gui] ready")
        self.bind("<KeyPress>", self._on_key)
        self._log_text.bind("<KeyPress>", self._on_key)

    def _set_icon(self) -> None:
        """A small moss record for the title bar instead of the interpreter icon."""
        icon = tk.PhotoImage(width=32, height=32)
        icon.put(gui_theme.PAPER, to=(0, 0, 32, 32))
        outer = 15.5**2
        inner = 4.0**2
        for y in range(32):
            for x in range(32):
                distance = (x - 15.5) ** 2 + (y - 15.5) ** 2
                if distance <= inner:
                    icon.put(gui_theme.CARD, (x, y))
                elif distance <= outer:
                    icon.put(gui_theme.MOSS, (x, y))
        self.iconphoto(True, icon)
        self._icon = icon
        self._iconbitmap_method_called = True

    def _wire_keyboard(self) -> None:
        targets = [
            *self._buttons.values(),
            self._picker_back,
            self._picker_search_button,
            self._picker_use,
            self._picker_open_button,
            self._picker_copy_button,
            self._url_toggle,
            self._url_scan_button,
            self._log_toggle,
        ]
        for widget in targets:
            self._add_focus_ring(widget)
        self.bind("<Tab>", lambda event: self._cycle_focus(False))
        self.bind("<Shift-Tab>", lambda event: self._cycle_focus(True))

    def _add_focus_ring(self, widget: ctk.CTkButton) -> None:
        original_width = widget.cget("border_width")
        original_color = widget.cget("border_color")
        widget.bind(
            "<FocusIn>",
            lambda event: widget.configure(
                border_width=2, border_color=gui_theme.FOCUS
            ),
        )
        widget.bind(
            "<FocusOut>",
            lambda event: widget.configure(
                border_width=original_width, border_color=original_color
            ),
        )
        widget.bind("<Return>", lambda event: widget.invoke())
        widget.bind("<space>", lambda event: widget.invoke())

    def _focus_targets(self) -> list[tk.Misc]:
        if self._picker_open:
            targets: list[tk.Misc] = [
                self._picker_back,
                self._picker_search_button,
                self._picker_tree,
                self._picker_use,
                self._picker_open_button,
                self._picker_copy_button,
                self._log_toggle,
            ]
        else:
            targets = [
                self._buttons["play"],
                self._buttons["scan"],
                self._buttons["fix"],
                self._player_box,
                self._url_toggle,
            ]
            if self._url_revealed:
                targets.extend([self._url_entry, self._url_scan_button])
            targets.extend([self._log_toggle, self._buttons["doctor"]])
        available = []
        for widget in targets:
            if widget is None or not widget.winfo_ismapped():
                continue
            if isinstance(widget, ctk.CTkButton) and widget.cget("state") == "disabled":
                continue
            available.append(widget)
        return available

    def _cycle_focus(self, backwards: bool) -> str:
        targets = self._focus_targets()
        if not targets:
            return "break"
        self._focus_index = gui_theme.next_focus_index(
            self._focus_index, len(targets), backwards=backwards
        )
        targets[self._focus_index].focus_set()
        return "break"

    def _resize_shell(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._shell.pack_configure(
            padx=gui_theme.content_padding(event.width, scale=self._ui_scale)
        )

    def _show_now_view(self) -> None:
        self._picker_view.pack_forget()
        self._now_view.pack(fill="both", expand=True)
        self._activity_card.pack(fill="x", pady=(12, 0))

    def _show_picker_view(self) -> None:
        self._now_view.pack_forget()
        self._activity_card.pack_forget()
        self._picker_view.pack(fill="both", expand=True)

    def _resize_hero(self, event: tk.Event) -> None:
        self._now_title.configure(wraplength=gui_theme.title_wraplength(event.width))

    def _toggle_url(self) -> None:
        self._url_revealed = not self._url_revealed
        if self._url_revealed:
            self._url_panel.pack(fill="x", pady=(8, 0))
            self._url_toggle.configure(text="Hide video link")
            self._url_entry.focus_set()
        else:
            self._url_panel.pack_forget()
            self._url_toggle.configure(text="Use a specific video")

    def _show_notice(self, message: str, kind: str = "danger") -> None:
        notice = getattr(self, "_notice", None)
        if notice is None:
            return
        colors = {
            "danger": (gui_theme.DANGER_SOFT, gui_theme.INK),
            "info": (gui_theme.MOSS_SOFT, gui_theme.MOSS),
            "lock": (gui_theme.STONE, gui_theme.INK),
        }
        background, foreground = colors.get(kind, colors["danger"])
        self._notice.configure(fg_color=background)
        width = self.winfo_width() if hasattr(self, "winfo_width") else 0
        self._notice_label.configure(
            text=message,
            text_color=foreground,
            wraplength=min(680, max(320, width - 180)),
        )
        self._notice_kind = kind
        parent = getattr(self, "_now_view", None)
        before = parent.master if parent is not None else None
        if before is None:
            self._notice.pack(fill="x", pady=(0, 12))
        else:
            self._notice.pack(fill="x", pady=(0, 12), before=before)

    def _clear_notice(self, kind: str | None = None) -> None:
        notice = getattr(self, "_notice", None)
        if notice is None:
            return
        if kind is not None and self._notice_kind != kind:
            return
        notice.pack_forget()
        self._notice_kind = None

    def _refresh_sync_badge(self) -> None:
        badge = getattr(self, "_sync_badge", None)
        if badge is None:
            return
        track_id = None
        if self._state is not None and (self._state.artist or self._state.title):
            track_id = make_track_id(self._state.artist, self._state.title)
        text, tone = sync_badge_text(track_id, self._root_dir)
        if not text:
            badge.pack_forget()
            return
        badge.configure(
            text=text,
            text_color=gui_theme.MOSS if tone == "synced" else gui_theme.CLAY_DARK,
            fg_color=gui_theme.MOSS_SOFT if tone == "synced" else gui_theme.STONE,
        )
        badge.pack(side="right", anchor="n")

    def _auto_refresh(self) -> None:
        self._refresh_after = None
        if self._closing:
            return
        if not self._busy or self._action == "play":
            self.refresh_track(quiet=True)
        self._refresh_after = self.after(TRACK_REFRESH_MS, self._auto_refresh)

    def open_picker(self) -> None:
        state = self._state
        if state is None or (not state.artist.strip() and not state.title.strip()):
            self._show_notice("Start a song in your player, then choose a video.", "info")
            return
        if self._locked:
            self._show_notice(lock_status(self._lock_pid), "lock")
            return
        track_id = make_track_id(state.artist, state.title)
        self._picker_open = True
        self._picker_track_id = track_id
        self._picker_candidates = []
        self._picker_link_var.set("")
        newest = load_newest_alignment(paths.alignments_dir(self._root_dir), track_id)
        self._picker_current_video = video_choice.load_video_choice(track_id, self._root_dir)
        if self._picker_current_video is None and newest is not None:
            self._picker_current_video = newest[2]
        self._picker_track_label.configure(
            text=f"{state.artist or 'Unknown artist'} - {state.title or 'Unknown title'}"
        )
        self._fill_picker_table()
        self._picker_status.configure(text="Search to choose a video")
        self._picker_search_button.configure(state="normal")
        self._update_picker_buttons()
        self._show_picker_view()

    def _close_picker(self) -> None:
        self._picker_open = False
        self._picker_busy = False
        self._show_now_view()
        self._update_buttons()
        self._update_status()

    def _search_picker(self) -> None:
        state = self._state
        if state is None or self._picker_busy or not self._picker_open:
            return
        track_id = make_track_id(state.artist, state.title)
        if track_id != self._picker_track_id:
            self._reset_picker_track()
            return
        artist, title, duration_s = state.artist, state.title, state.duration_s
        self._picker_busy = True
        self._picker_search_button.configure(state="disabled")
        self._picker_status.configure(text="Searching videos...")
        self._log("searching youtube for MVs")

        def work() -> None:
            try:
                candidates = pick_mv_candidates(artist, title, duration_s, root=self._root_dir)
            except RuntimeError as exc:
                self._picker_after(self._picker_search_failed, str(exc))
                return
            self._picker_after(self._picker_search_done, track_id, candidates)

        threading.Thread(target=work, daemon=True, name="picker-search").start()

    def _picker_after(self, callback: Callable[..., None], *args: object) -> None:
        if self._closing:
            return
        with contextlib.suppress(tk.TclError, RuntimeError):
            self.after(0, callback, *args)

    def _picker_search_done(self, track_id: str, candidates: list[Candidate]) -> None:
        self._picker_busy = False
        if self._closing:
            return
        if not self._picker_open or track_id != self._picker_track_id:
            self._picker_search_button.configure(state="normal")
            return
        self._picker_candidates = candidates
        self._picker_search_button.configure(state="normal")
        self._picker_status.configure(text=f"{len(candidates)} videos found")
        self._fill_picker_table()
        if candidates:
            self._picker_tree.selection_set("1")
            self._picker_tree.focus("1")
        self._update_picker_buttons()

    def _picker_search_failed(self, message: str) -> None:
        self._picker_busy = False
        if self._closing:
            return
        if self._picker_open:
            self._picker_search_button.configure(state="normal")
            self._picker_status.configure(text="Search failed")
        self._log(message)
        self._show_notice(message, "danger")

    def _fill_picker_table(self) -> None:
        self._picker_tree.delete(*self._picker_tree.get_children())
        for rank, candidate in enumerate(self._picker_candidates, start=1):
            row = candidate_row_values(rank, candidate, self._picker_current_video)
            tags = ("in_use",) if candidate.video_id == self._picker_current_video else ()
            self._picker_tree.insert("", "end", iid=str(rank), values=row, tags=tags)
        if self._picker_candidates:
            self._picker_empty.place_forget()
        else:
            self._picker_empty.place(relx=0.5, rely=0.5, anchor="center")

    def _reset_picker_track(self) -> None:
        state = self._state
        if state is None:
            self._close_picker()
            return
        self._picker_track_id = make_track_id(state.artist, state.title)
        self._picker_busy = False
        self._picker_candidates = []
        self._picker_current_video = None
        self._picker_link_var.set("")
        self._picker_search_button.configure(state="normal")
        self._picker_track_label.configure(
            text=f"{state.artist or 'Unknown artist'} - {state.title or 'Unknown title'}"
        )
        self._fill_picker_table()
        self._picker_status.configure(text="Track changed. Search again.")
        self._update_picker_buttons()
        self._show_notice("The track changed. Search for the new track.", "info")

    def _selected_index(self) -> int | None:
        selection = self._picker_tree.selection()
        if not selection:
            return None
        return int(selection[0])

    def _selected_candidate(self) -> Candidate | None:
        index = self._selected_index()
        if index is None or not 1 <= index <= len(self._picker_candidates):
            return None
        return self._picker_candidates[index - 1]

    def _update_picker_buttons(self) -> None:
        selected = bool(self._picker_tree.selection()) and not self._picker_busy
        state = "normal" if selected else "disabled"
        self._picker_use.configure(state=state)
        self._picker_open_button.configure(state=state)
        self._picker_copy_button.configure(state=state)
        self._picker_link_button.configure(
            state="disabled" if self._picker_busy else "normal"
        )

    def _use_selected(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            self._show_notice("Select a video first.", "info")
            return
        self._use_candidate(candidate)

    def use_link(self) -> None:
        """Use a pasted YouTube link or bare video id as this track's video."""
        text = self._picker_link_var.get().strip()
        if not text:
            self._show_notice("Paste a YouTube link first.", "info")
            return
        try:
            video_id = parse_video_id(text)
        except ValueError:
            self._show_notice("That is not a YouTube link.", "info")
            return
        self._use_candidate(
            Candidate(video_id=video_id, title="", duration_s=None, channel="")
        )

    def _use_candidate(self, candidate: Candidate) -> None:
        """Save the candidate as the track's video choice, after a track-change check."""
        picker_track = self._picker_track_id
        refresh = getattr(self, "refresh_track", None)
        if callable(refresh):
            refresh(quiet=True)
        state = self._state
        if state is None or picker_track is None:
            self._show_notice("The track changed. Search for the new track.", "info")
            return
        current_track = make_track_id(state.artist, state.title)
        if current_track != picker_track:
            self._show_notice("The track changed. Search for the new track.", "info")
            return
        if candidate.video_id == self._picker_current_video:
            self._show_notice("That video is already in use.", "info")
            return
        video_choice.save_video_choice(
            self._picker_track_id, candidate.video_id, self._root_dir
        )
        if self._action == "play" and self._video_switches is not None:
            from mvsync.sync_runtime import VideoSwitch

            self._video_switches.put(
                VideoSwitch(self._picker_track_id, candidate.video_id)
            )
        mapped = (
            load_alignment(
                paths.alignments_dir(self._root_dir),
                candidate.video_id,
                self._picker_track_id,
            )
            is not None
        )
        self._close_picker()
        if mapped:
            self._show_notice("Switching to the synced video.", "info")
        else:
            self._show_notice(
                "Rough switch on. This video will be refined on the next listen.",
                "info",
            )

    def open_selected(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            self._show_notice("Select a video first.", "info")
            return
        webbrowser.open(video_url(candidate.video_id))

    def copy_selected(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            self._show_notice("Select a video first.", "info")
            return
        self.clipboard_clear()
        self.clipboard_append(video_url(candidate.video_id))
        self._picker_status.configure(text="Link copied")

    def _dispatch(self, action: str) -> None:
        """Button entry point: every MAIN_ACTIONS button carries its own action name."""
        if action == "doctor":
            self.doctor()
        elif action == "scan":
            self.scan()
        elif action == "fix":
            self.open_picker()
        elif action == "play":
            self.play()

    def _log(self, message: str) -> None:
        append_log(self._log_text, [f"{message}\n"])

    def _set_status(self, text: str, tone: str = "idle") -> None:
        self._status_label.configure(text=text, text_color=gui_theme.status_color(tone))
        dot = getattr(self, "_status_dot", None)
        if dot is not None:
            dot.configure(fg_color=gui_theme.status_color(tone))

    def _set_progress(self, running: bool) -> None:
        """Show the thin progress bar only while a worker owns the window."""
        progress = getattr(self, "_progress", None)
        if progress is None:
            return
        if running:
            progress.pack(side="right", padx=(8, 0))
            progress.start()
        else:
            progress.stop()
            progress.pack_forget()

    def _toggle_log(self) -> None:
        body = getattr(self, "_log_body", None)
        if body is None:
            return
        if body.winfo_ismapped():
            body.pack_forget()
            self._log_toggle.configure(text="Activity")
        else:
            body.pack(fill="x", pady=(8, 12))
            self._log_toggle.configure(text="Hide activity")

    def _copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._log_text.get("1.0", "end-1c"))
        self._log("[gui] log copied")

    def _open_data_dir(self) -> None:
        data_dir = paths.ensure_dir(paths.data_dir(self._root_dir))
        with contextlib.suppress(OSError):
            os.startfile(str(data_dir))

    def _refresh_lock(self) -> bool:
        """Re-read the lock, so a session started in another console greys the buttons."""
        pid = session.lock_holder(self._root_dir)
        if pid and not self._locked:
            self._log(f"[gui] {session.lock_refusal_message(pid)}")
        self._locked = pid != 0
        self._lock_pid = pid
        if pid:
            self._show_notice(lock_status(pid), "lock")
        elif self._notice_kind == "lock":
            self._clear_notice("lock")
        self._update_buttons()
        self._update_status()
        return self._locked

    def _update_status(self) -> None:
        """One owner for the status line: what runs, what blocks it, or ready."""
        tone = status_tone(busy=self._busy, locked=self._locked)
        if self._busy:
            text = gui_theme.status_text(self._action, locked=False)
        else:
            text = gui_theme.status_text(None, locked=self._locked)
        self._set_status(text, tone)

    def _update_buttons(self) -> None:
        player_box = getattr(self, "_player_box", None)
        if player_box is not None:
            player_box.configure(state="disabled" if self._busy else "readonly")
        refresh_button = getattr(self, "_refresh_button", None)
        if refresh_button is not None:
            refresh_button.configure(state="disabled" if self._busy else "normal")
        has_track = self._state is not None and bool(
            self._state.artist.strip() or self._state.title.strip()
        )
        url_toggle = getattr(self, "_url_toggle", None)
        if url_toggle is not None:
            url_toggle.configure(
                state="normal"
                if has_track and not self._busy and not self._locked
                else "disabled"
            )
        url_scan = getattr(self, "_url_scan_button", None)
        if url_scan is not None:
            url_scan.configure(
                state="normal"
                if has_track and not self._busy and not self._locked
                else "disabled"
            )
        enabled = set(
            enabled_actions(self._busy, self._locked, has_track=has_track)
        )
        if self._action == "play" and not self._locked:
            enabled = {"play", "fix"}
        for action, button in self._buttons.items():
            stoppable = action == "play" and self._action == "play"
            state = "normal" if action in enabled else "disabled"
            if action == "play":
                if stoppable:
                    button.configure(
                        state=state,
                        text="Stop",
                        fg_color=gui_theme.DANGER,
                        hover_color=gui_theme.DANGER_DARK,
                        text_color=gui_theme.WHITE,
                    )
                elif state == "disabled":
                    button.configure(
                        state=state,
                        text="Play",
                        fg_color=gui_theme.STONE,
                        hover_color=gui_theme.STONE,
                        text_color=gui_theme.FAINT,
                    )
                else:
                    button.configure(
                        state=state,
                        text="Play",
                        fg_color=gui_theme.MOSS,
                        hover_color=gui_theme.MOSS_DARK,
                        text_color=gui_theme.PALE,
                    )
            elif action == "doctor":
                button.configure(
                    state=state,
                    text=gui_theme.action_label(action),
                    fg_color="transparent",
                    hover_color=gui_theme.STONE,
                    text_color=gui_theme.MUTED,
                )
            else:
                button.configure(
                    state=state,
                    text=gui_theme.action_label(action),
                    fg_color="transparent",
                    hover_color=gui_theme.STONE,
                    text_color=gui_theme.MOSS,
                    border_color=gui_theme.BORDER,
                )

    def _set_hotkeys_visible(self, visible: bool) -> None:
        if visible:
            self._hotkey_row.pack(fill="x", pady=(14, 0))
        else:
            self._hotkey_row.pack_forget()

    def _push_key(self, key: str) -> None:
        """Queue one play hotkey; mpv holds the focus, so buttons feed the key source."""
        self._keys.push(key)

    def _on_key(self, event: tk.Event) -> str | None:
        """Match keysym, never event.char: a non-US layout reports jamo for [ and ].

        A widget that consumes the key itself (the URL entry) keeps it: Tk runs the
        toplevel binding after the class binding, so typing there would otherwise
        quit play, resync, toggle the player, or trim the offset.
        """
        if event.widget in self._key_consumers:
            return None
        key = HOTKEYS_BY_KEYSYM.get(event.keysym)
        if key is None:
            return None
        self._keys.push(key)
        return "break"

    def _reset_keys(self) -> None:
        """Drop queued keys so a stale q cannot stop the next play instantly."""
        while self._keys.poll():
            pass

    def refresh_track(self, quiet: bool = False) -> None:
        try:
            self._state = PlayerBridge().current()
        except RuntimeError as exc:
            self._state = None
            if not quiet:
                self._log(str(exc))
        state = self._state
        title, artist, meta = now_playing_values(state)
        for name, value in (
            ("_now_title", title),
            ("_now_artist", artist),
            ("_now_meta", meta),
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(text=value)
        if self._picker_open and state is not None:
            current_track = make_track_id(state.artist, state.title)
            if current_track != self._picker_track_id:
                self._reset_picker_track()
        self._refresh_sync_badge()
        self._refresh_lock()

    def refresh_players(self) -> None:
        """Fill the picker with SMTC sessions; a missing bridge only disables it."""
        try:
            sessions = PlayerBridge().sessions()
        except Exception as exc:  # noqa: BLE001 - the window must open without a player
            self._players = []
            self._player_box.configure(values=())
            self._player_box.set("No player found")
            self._log(f"[gui] player list unavailable: {exc}")
            return
        self._players = sessions
        self._player_box.configure(
            values=[gui_theme.player_option_label(session) for session in sessions]
        )
        index = player_index(sessions, config.load_player(self._root_dir))
        if not sessions:
            self._player_box.set("No player found")
        elif index is None:
            self._player_box.set("")
        else:
            self._player_box.current(index)

    def refresh_all(self) -> None:
        """Refresh the player list and the now-playing card together."""
        self.refresh_players()
        self.refresh_track()

    def _select_player(self) -> None:
        """Persist the picked player and re-read the track through it."""
        if not self._players:
            return
        index = self._player_box.current()
        if not 0 <= index < len(self._players):
            return
        app_id = self._players[index].app_id
        exe = guess_player_exe(app_id)
        config.save_player(app_id, exe, self._root_dir)
        self._log(f"[gui] player {app_id}" + (f" ({exe})" if exe else ""))
        self.refresh_track()

    def _run(self, action: str, worker: Callable[[], int]) -> None:
        """Run one operation on a worker thread, capturing stdout into the log pane."""
        if self._busy:
            self._log("[gui] another operation is already running")
            return
        if self._refresh_lock():
            self._log(
                f"[gui] {action} refused: {session.lock_refusal_message(self._lock_pid)}; "
                "press Refresh to re-check"
            )
            return
        self._clear_notice()
        self._busy = True
        self._action = action
        self._sink = LogSink()
        sink = self._sink
        self._set_progress(True)
        self._update_buttons()
        self._update_status()
        if action == "play":
            self._set_hotkeys_visible(True)
        self._pump_after = self.after(LOG_PUMP_MS, self._pump)

        def body() -> None:
            with contextlib.redirect_stdout(sink):
                try:
                    rc = worker()
                except Exception as exc:  # noqa: BLE001 - the GUI reports, never raises
                    print(f"[gui] {action} failed: {exc}")
                    rc = 1
            if self._closing:
                return
            with contextlib.suppress(tk.TclError, RuntimeError):  # root gone, or no main loop
                self.after(0, self._run_finished, action, rc)

        self._worker = threading.Thread(target=body, daemon=True)
        self._worker.start()

    def _pump(self) -> None:
        """Show the worker's output while it runs; a play session lasts an hour."""
        self._pump_after = None
        if self._closing:
            return
        append_log(self._log_text, drain_lines(self._sink))
        self._pump_after = self.after(LOG_PUMP_MS, self._pump)

    def _cancel_pump(self) -> None:
        if self._pump_after is not None:
            self.after_cancel(self._pump_after)
            self._pump_after = None

    def _run_finished(self, action: str, rc: int) -> None:
        self._cancel_pump()
        if self._closing:
            return
        append_log(self._log_text, drain_lines(self._sink))
        self._log(f"[gui] {action} finished")
        if rc:
            self._log(f"[gui] {action} exited with code {rc}")
        self._busy = False
        self._action = None
        self._set_progress(False)
        self._set_hotkeys_visible(False)
        self._refresh_lock()
        if rc:
            self._show_notice(
                f"{gui_theme.action_label(action)} did not finish. Check Activity for details.",
                "danger",
            )
        if action == "scan":
            self.refresh_track()

    def play(self) -> None:
        """Start the follow loop, or stop the running one (the Play button is Stop)."""
        if self._action == "play":
            self._play_stop.set()
            self._keys.push("q")
            return
        self._reset_keys()
        self._play_stop = threading.Event()
        self._video_switches = queue.Queue()
        self._run("play", self._play_worker)

    def _play_worker(self) -> int:
        from mvsync import sync_runtime

        return sync_runtime.run_play(
            None,
            root=self._root_dir,
            keys=self._keys.poll,
            stop_event=self._play_stop,
            switches=self._video_switches,
        )

    def scan(self) -> None:
        self._start_scan("")

    def use_url(self) -> None:
        url = self._url_var.get().strip()
        if not url:
            self._show_notice("Paste a YouTube link first.", "info")
            return
        self._start_scan(url)

    def _start_scan(self, url: str) -> None:
        """Read the entry here, on the Tk thread; the worker only gets a plain string."""
        self._run("scan", lambda: self._scan_worker(url))

    def _scan_worker(self, url: str) -> int:
        try:
            candidates = self._scan_candidates(url)
        except (ValueError, RuntimeError) as exc:
            print(f"[scan] aborted: {exc}")
            return 1
        result = scan_with_candidates(candidates, root=self._root_dir)
        print(f"[scan] saved {result.path.name} for {result.video_id}")
        return 0

    def _scan_candidates(self, url: str) -> list[Candidate]:
        """The candidates a scan records against: the given url, else a fresh search."""
        if url:
            return [
                Candidate(video_id=parse_video_id(url), title="", duration_s=None, channel="")
            ]
        state = self._state
        if state is None:
            raise RuntimeError("no player session; start playback in your media player")
        return pick_mv_candidates(state.artist, state.title, state.duration_s, root=self._root_dir)

    def doctor(self) -> None:
        self._run("doctor", self._doctor_worker)

    def _doctor_worker(self) -> int:
        from mvsync.cli import doctor

        return doctor()

    def _on_close(self) -> None:
        if self._busy and not messagebox.askyesno("mvsync", close_question(self._action)):
            return
        self._closing = True
        self._cancel_pump()
        if self._refresh_after is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self._refresh_after)
            self._refresh_after = None
        if self._action == "play":
            self._stop_play()
        self.destroy()

    def _stop_play(self) -> None:
        """Ask the follow loop to quit and wait for its mpv and session cleanup."""
        self._play_stop.set()
        self._keys.push("q")
        if self._worker is not None:
            self._worker.join(timeout=CLOSE_STOP_TIMEOUT_S)


class FixerWindow(ctk.CTk):
    """Pick the right MV for the current player track and rebuild its map."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self._ui_scale = configure_ctk_scaling(self)
        self.title("mvsync - Rebuild map")
        self.minsize(820, 620)
        with contextlib.suppress(tk.TclError):
            self.geometry("980x720")
        self._root_dir = root
        self._fonts = gui_theme.make_fonts(self)
        self._state: PlayerState | None = None
        self._track_id: str | None = None
        self._candidates: list[Candidate] = []
        self._in_use_video_id: str | None = None
        self._busy = False
        self.configure(fg_color=gui_theme.PAPER)
        self._build_ui()
        log_path = devlog.configure(root)
        if log_path is not None:
            self._log(f"[log] writing {log_path}")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_track()

    def _build_ui(self) -> None:
        gui_theme.configure_ttk(self, self._fonts)
        shell = ctk.CTkFrame(self, fg_color="transparent")
        shell.pack(fill="both", expand=True, padx=28, pady=24)
        self._shell = shell
        self.bind("<Configure>", self._resize_shell, add="+")

        header = ctk.CTkFrame(shell, fg_color="transparent")
        header.pack(fill="x")
        mark = ctk.CTkFrame(
            header,
            width=34,
            height=34,
            corner_radius=17,
            fg_color=gui_theme.MOSS,
        )
        mark.pack(side="left")
        mark.pack_propagate(False)
        ctk.CTkFrame(
            mark,
            width=8,
            height=8,
            corner_radius=4,
            fg_color=gui_theme.PAPER,
        ).place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(
            header,
            text="mvsync",
            font=self._fonts.wordmark,
            text_color=gui_theme.INK,
        ).pack(side="left", padx=(10, 0))
        ctk.CTkLabel(
            header,
            text="Rebuild map",
            font=self._fonts.small,
            text_color=gui_theme.MUTED,
        ).pack(side="left", padx=(10, 0))

        track_card = ctk.CTkFrame(
            shell,
            fg_color=gui_theme.CARD,
            corner_radius=gui_theme.RADIUS_CARD,
            border_width=1,
            border_color=gui_theme.BORDER,
        )
        track_card.pack(fill="x", pady=(18, 12))
        track_inner = ctk.CTkFrame(track_card, fg_color="transparent")
        track_inner.pack(fill="x", padx=24, pady=20)
        track_text = ctk.CTkFrame(track_inner, fg_color="transparent")
        track_text.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            track_text,
            text="CURRENT TRACK",
            font=self._fonts.small_bold,
            text_color=gui_theme.MUTED,
            anchor="w",
        ).pack(anchor="w")
        self._track_label = ctk.CTkLabel(
            track_text,
            text="No player session",
            font=self._fonts.title,
            text_color=gui_theme.INK,
            anchor="w",
            justify="left",
            wraplength=560,
        )
        self._track_label.pack(anchor="w", pady=(6, 0))
        self._search_button = ctk.CTkButton(
            track_inner,
            text="Search videos",
            command=self.search,
            font=self._fonts.body_bold,
            **gui_theme.button_style("primary"),
        )
        self._search_button.pack(side="right", padx=(16, 0))
        self._refresh_button = ctk.CTkButton(
            track_inner,
            text="Refresh",
            command=self.refresh_track,
            font=self._fonts.small,
            **gui_theme.button_style("ghost"),
        )
        self._refresh_button.pack(side="right", padx=(8, 0))

        table_card = ctk.CTkFrame(
            shell,
            fg_color=gui_theme.CARD,
            corner_radius=gui_theme.RADIUS_CARD,
            border_width=1,
            border_color=gui_theme.BORDER,
        )
        table_card.pack(fill="both", expand=True)
        table_inner = ctk.CTkFrame(table_card, fg_color="transparent")
        table_inner.pack(fill="both", expand=True, padx=16, pady=14)
        columns = ("#", "mv", "channel", "length", "status")
        self._tree = ttk.Treeview(
            table_inner,
            columns=columns,
            show="headings",
            height=8,
            style="Results.Treeview",
            selectmode="browse",
        )
        for column, heading, width, minimum, stretch in (
            ("#", "#", 44, 40, False),
            ("mv", "Video", 360, 220, True),
            ("channel", "Channel", 190, 120, True),
            ("length", "Length", 70, 60, False),
            ("status", "Status", 90, 80, False),
        ):
            self._tree.heading(column, text=heading, anchor="w")
            self._tree.column(
                column, width=width, minwidth=minimum, stretch=stretch, anchor="w"
            )
        self._tree.tag_configure("in_use", foreground=gui_theme.MOSS)
        self._tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_inner, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._tree.bind("<<TreeviewSelect>>", lambda event: self._update_buttons())
        self._tree.bind("<Double-1>", lambda event: self.open_selected())
        self._tree.bind("<Return>", lambda event: self.use_selected())

        link_card = ctk.CTkFrame(
            shell,
            fg_color=gui_theme.STONE,
            corner_radius=18,
            border_width=1,
            border_color=gui_theme.BORDER,
        )
        link_card.pack(fill="x", pady=(12, 0))
        link_inner = ctk.CTkFrame(link_card, fg_color="transparent")
        link_inner.pack(fill="x", padx=12, pady=10)
        ctk.CTkLabel(
            link_inner,
            text="Use a specific video",
            font=self._fonts.small,
            text_color=gui_theme.MUTED,
        ).pack(side="left", padx=(0, 10))
        self._link_var = tk.StringVar()
        self._link_entry = tk.Entry(
            link_inner,
            textvariable=self._link_var,
            bg=gui_theme.STONE,
            fg=gui_theme.INK,
            insertbackground=gui_theme.INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=gui_theme.BORDER,
            highlightcolor=gui_theme.MOSS,
            font=self._fonts.body,
        )
        self._link_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self._link_entry.bind("<Return>", lambda event: self.use_link())
        self._link_button = ctk.CTkButton(
            link_inner,
            text="Use link",
            command=self.use_link,
            font=self._fonts.small_bold,
            **gui_theme.button_style("secondary"),
        )
        self._link_button.pack(side="right", padx=(10, 0))

        bottom = ctk.CTkFrame(shell, fg_color="transparent")
        bottom.pack(fill="x", pady=(12, 0))
        self._use_button = ctk.CTkButton(
            bottom,
            text="Rebuild map",
            command=self.use_selected,
            font=self._fonts.body_bold,
            **gui_theme.button_style("primary"),
        )
        self._use_button.pack(side="left")
        self._open_button = ctk.CTkButton(
            bottom,
            text="Open in browser",
            command=self.open_selected,
            font=self._fonts.small_bold,
            **gui_theme.button_style("secondary"),
        )
        self._open_button.pack(side="left", padx=(8, 0))
        self._copy_button = ctk.CTkButton(
            bottom,
            text="Copy link",
            command=self.copy_selected,
            font=self._fonts.small_bold,
            **gui_theme.button_style("secondary"),
        )
        self._copy_button.pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            bottom,
            text="Back",
            command=self.back,
            font=self._fonts.small,
            **gui_theme.button_style("ghost"),
        ).pack(side="right")
        self._status_label = ctk.CTkLabel(
            bottom,
            text="",
            font=self._fonts.small,
            text_color=gui_theme.MUTED,
        )
        self._status_label.pack(side="left", padx=(12, 0))

        log_card = ctk.CTkFrame(
            shell,
            fg_color=gui_theme.CARD,
            corner_radius=20,
            border_width=1,
            border_color=gui_theme.BORDER,
        )
        log_card.pack(fill="x", pady=(12, 0))
        ctk.CTkLabel(
            log_card,
            text="ACTIVITY",
            font=self._fonts.small_bold,
            text_color=gui_theme.MUTED,
        ).pack(anchor="w", padx=16, pady=(12, 0))
        self._log_text = ctk.CTkTextbox(
            log_card,
            height=110,
            state="disabled",
            wrap="word",
            fg_color=gui_theme.STONE,
            text_color=gui_theme.INK,
            corner_radius=16,
            border_width=0,
            font=self._fonts.mono,
        )
        self._log_text.pack(fill="x", padx=16, pady=(8, 14))

    def _resize_shell(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._shell.pack_configure(
            padx=gui_theme.content_padding(
                event.width, max_width=1040, scale=self._ui_scale
            )
        )

    def _update_buttons(self) -> None:
        has_track = self._state is not None and bool(
            self._state.artist.strip() or self._state.title.strip()
        )
        ready = "normal" if has_track and not self._busy else "disabled"
        self._refresh_button.configure(state=ready)
        self._search_button.configure(state=ready)
        selected = bool(self._tree.selection()) and not self._busy
        choice = "normal" if selected else "disabled"
        self._use_button.configure(state=choice)
        self._open_button.configure(state=choice)
        self._copy_button.configure(state=choice)
        self._link_button.configure(state=ready)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def _log(self, message: str) -> None:
        self._log_text.configure(state="normal")
        self._log_text.insert("end", f"{message}\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _fill_table(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for rank, candidate in enumerate(self._candidates, start=1):
            row = candidate_row_values(rank, candidate, self._in_use_video_id)
            tags = ("in_use",) if candidate.video_id == self._in_use_video_id else ()
            self._tree.insert("", "end", iid=str(rank), values=row, tags=tags)

    def _selected_index(self) -> int | None:
        selection = self._tree.selection()
        if not selection:
            return None
        return int(selection[0])

    def _selected_candidate(self) -> Candidate | None:
        index = self._selected_index()
        if index is None or not 1 <= index <= len(self._candidates):
            return None
        return self._candidates[index - 1]

    def refresh_track(self) -> None:
        try:
            self._state = PlayerBridge().current()
        except RuntimeError as exc:
            self._state = None
            self._log(str(exc))
        state = self._state
        track_id = make_track_id(state.artist, state.title) if state is not None else None
        if track_id != self._track_id:
            self._track_id = track_id
            self._candidates = []
            self._in_use_video_id = None
            self._link_var.set("")
        self._track_label.configure(
            text=(
                "No player session"
                if state is None
                else f"{state.artist or 'Unknown artist'} - {state.title or 'Unknown title'} "
                f"({format_length(state.duration_s)})"
            )
        )
        if track_id is not None:
            in_use = load_newest_alignment(paths.alignments_dir(self._root_dir), track_id)
            self._in_use_video_id = in_use[2] if in_use is not None else None
        self._fill_table()
        self._status_label.configure(
            text="Start a song in your player, then search."
            if state is None
            else "Search to see videos for this track."
        )
        self._update_buttons()

    def search(self) -> None:
        state = self._state
        if state is None:
            self._status_label.configure(text="Start a song in your player first.")
            return
        artist, title, duration_s = state.artist, state.title, state.duration_s
        track_id = make_track_id(artist, title)
        self._set_busy(True)
        self._status_label.configure(text="Searching videos...")
        self._log("searching youtube for MVs")

        def work() -> None:
            try:
                candidates = pick_mv_candidates(artist, title, duration_s, root=self._root_dir)
            except RuntimeError as exc:
                self.after(0, self._search_failed, str(exc))
                return
            self.after(0, self._search_done, track_id, candidates)

        threading.Thread(target=work, daemon=True).start()

    def _search_done(self, track_id: str, candidates: list[Candidate]) -> None:
        self._track_id = track_id
        self._candidates = candidates
        self._fill_table()
        if candidates:
            self._tree.selection_set("1")
            self._tree.focus("1")
        self._set_busy(False)
        self._status_label.configure(text=f"{len(candidates)} videos found")

    def _search_failed(self, message: str) -> None:
        self._set_busy(False)
        self._log(message)
        self._status_label.configure(text="Search failed")

    def open_selected(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            self._status_label.configure(text="Select a video first.")
            return
        webbrowser.open(video_url(candidate.video_id))

    def copy_selected(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            self._status_label.configure(text="Select a video first.")
            return
        self.clipboard_clear()
        self.clipboard_append(video_url(candidate.video_id))
        self._status_label.configure(text="Link copied")

    def use_link(self) -> None:
        """Rebuild against a pasted YouTube link or bare video id."""
        text = self._link_var.get().strip()
        if not text:
            self._status_label.configure(text="Paste a YouTube link first.")
            return
        try:
            video_id = parse_video_id(text)
        except ValueError:
            self._status_label.configure(text="That is not a YouTube link.")
            return
        self._rebuild(
            [Candidate(video_id=video_id, title="", duration_s=None, channel="")]
        )

    def use_selected(self) -> None:
        index = self._selected_index()
        if index is None or not self._candidates:
            self._status_label.configure(text="Select a video first.")
            return
        self._rebuild(prioritize(self._candidates, index))

    def _rebuild(self, candidates: list[Candidate]) -> None:
        """Confirm, then record against the given candidates, best first."""
        candidate = candidates[0]
        state = self._state
        if state is None:
            self._status_label.configure(text="Start a song in your player first.")
            return
        question = (
            f"Rebuild the map for {state.artist} - {state.title} using "
            f"{candidate.title or candidate.video_id}?\n\n"
            f"The player pauses, rewinds, and plays the track once "
            f"({format_length(state.duration_s)}); keep it playing untouched until it ends."
        )
        if not messagebox.askyesno("mvsync", question):
            return
        self._set_busy(True)
        self._log(f"scanning with {candidate.video_id}")

        def work() -> None:
            try:
                result = scan_with_candidates(
                    candidates,
                    root=self._root_dir,
                    on_progress=lambda message: self.after(0, self._log, message),
                )
            except (ScanAborted, RuntimeError) as exc:
                self.after(0, self._use_failed, str(exc))
                return
            self.after(0, self._use_done, result)

        threading.Thread(target=work, daemon=True).start()

    def _use_done(self, result: ScanResult) -> None:
        self._log(f"saved a new map for {result.video_id}")
        self._set_busy(False)
        self.refresh_track()
        self.back()

    def _use_failed(self, message: str) -> None:
        self._log(message)
        self._set_busy(False)
        self._status_label.configure(text="Rebuild failed. Check Activity.")

    def back(self) -> None:
        """Leave the picker; the main window's Fix worker finishes when this closes."""
        self._on_close()

    def _on_close(self) -> None:
        if self._busy and not messagebox.askyesno(
            "mvsync", "A scan is running; closing now aborts it. Close anyway?"
        ):
            return
        self.destroy()


def run_gui(root: Path | None = None) -> int:
    """Open the main window and run its mainloop."""
    window = MainWindow(root=root)
    window.mainloop()
    return 0


def run_fixer(*, root: Path | None = None) -> int:
    """Open the picker window and run its mainloop."""
    window = FixerWindow(root=root)
    window.mainloop()
    return 0
