"""Tkinter windows: the everyday main window, plus the MV picker behind Fix MV."""

from __future__ import annotations

import contextlib
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from functools import partial
from pathlib import Path
from tkinter import messagebox, ttk

from mvsync import paths
from mvsync.align_store import load_newest_alignment, make_track_id
from mvsync.gui_console import KeySource, LogSink
from mvsync.mv_audio import parse_video_id
from mvsync.mv_search import Candidate, pick_mv_candidates, prioritize, video_url
from mvsync.scan_runtime import ScanAborted, ScanResult, scan_with_candidates
from mvsync.spotify_smtc import SpotifyBridge, SpotifyState

MAIN_ACTIONS = ("play", "scan", "fix", "doctor")
ACTION_LABELS = {"play": "Play", "scan": "Scan", "fix": "Fix MV", "doctor": "Doctor"}
HOTKEYS_BY_KEYSYM = {"q": "q", "r": "r", "bracketleft": "[", "bracketright": "]", "p": "p"}
FIX_COMMAND = ("-m", "mvsync", "fix")
CLOSE_STOP_TIMEOUT_S = 10.0
LOG_PUMP_MS = 250


def append_log(widget: tk.Text, lines: list[str]) -> None:
    """Append whole lines to a read-only text pane and scroll to the newest."""
    widget.configure(state="normal")
    for line in lines:
        widget.insert("end", line)
    widget.see("end")
    widget.configure(state="disabled")


def drain_lines(sink: LogSink | None) -> list[str]:
    """The complete lines a worker has printed so far, taken out of its buffer."""
    return sink.drain() if sink is not None else []


def format_length(seconds: float | None) -> str:
    """Minutes and seconds, or "?" for an unknown duration."""
    if seconds is None:
        return "?"
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def candidate_row_values(
    rank: int, candidate: Candidate, in_use_video_id: str | None
) -> tuple[str, str, str, str, str]:
    """One Treeview row: number, title, channel, length, in-use status."""
    status = "in use" if candidate.video_id == in_use_video_id else ""
    return (
        str(rank),
        candidate.title,
        candidate.channel,
        format_length(candidate.duration_s),
        status,
    )


class MainWindow(tk.Tk):
    """Everyday window: play, scan, fix, and doctor over one live log pane."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self.title("mvsync")
        self.minsize(680, 560)
        self._root_dir = root
        self._state: SpotifyState | None = None
        self._keys = KeySource()
        self._busy = False
        self._closing = False
        self._action: str | None = None
        self._sink: LogSink | None = None
        self._pump_after: str | None = None
        self._worker: threading.Thread | None = None
        self._buttons: dict[str, ttk.Button] = {}
        self._key_consumers: tuple[tk.Misc, ...] = ()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self.refresh_track)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self._track_label = ttk.Label(top, text="no Spotify session")
        self._track_label.pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Refresh track", command=self.refresh_track).pack(side="right")

        actions = ttk.Frame(self, padding=(8, 0))
        actions.pack(fill="x")
        for action in MAIN_ACTIONS:
            button = ttk.Button(
                actions,
                text=ACTION_LABELS[action],
                command=partial(self._dispatch, action),
            )
            button.pack(side="left", padx=4)
            self._buttons[action] = button
        ttk.Button(actions, text="Quit", command=self._on_close).pack(side="right", padx=4)

        url_row = ttk.Frame(self, padding=(8, 8))
        url_row.pack(fill="x")
        self._url_var = tk.StringVar()
        self._url_entry = ttk.Entry(url_row, textvariable=self._url_var)
        self._url_entry.pack(side="left", fill="x", expand=True)
        self._url_entry.bind("<Return>", lambda event: self.use_url())
        self._key_consumers = (self._url_entry,)
        ttk.Button(url_row, text="Use this URL", command=self.use_url).pack(side="left", padx=4)

        self._hotkey_row = ttk.Frame(self, padding=(8, 0))
        for key in HOTKEYS_BY_KEYSYM.values():
            button = ttk.Button(self._hotkey_row, text=key, command=partial(self._push_key, key))
            button.pack(side="left", padx=2)

        self._status_label = ttk.Label(self, text="idle", padding=(8, 6))
        self._status_label.pack(fill="x")

        log_row = ttk.Frame(self, padding=(8, 0))
        log_row.pack(fill="both", expand=True)
        self._log_text = tk.Text(log_row, height=12, state="disabled", wrap="word", takefocus=0)
        scrollbar = ttk.Scrollbar(log_row, orient="vertical", command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._log_text.pack(side="left", fill="both", expand=True)

        self._log("[gui] ready")
        self.bind("<KeyPress>", self._on_key)
        self._log_text.bind("<KeyPress>", self._on_key)

    def _dispatch(self, action: str) -> None:
        """Button entry point: every MAIN_ACTIONS button carries its own action name."""
        if action == "doctor":
            self.doctor()
        elif action == "scan":
            self.scan()
        elif action == "fix":
            self.fix()
        elif action == "play":
            self.play()

    def _log(self, message: str) -> None:
        append_log(self._log_text, [f"{message}\n"])

    def _set_status(self, text: str) -> None:
        self._status_label.configure(text=text)

    def _update_buttons(self) -> None:
        for action, button in self._buttons.items():
            stoppable = action == "play" and self._action == "play"
            button.configure(state="normal" if stoppable or not self._busy else "disabled")

    def _set_hotkeys_visible(self, visible: bool) -> None:
        if visible:
            self._hotkey_row.pack(fill="x", padx=8)
        else:
            self._hotkey_row.pack_forget()

    def _push_key(self, key: str) -> None:
        """Queue one play hotkey; mpv holds the focus, so buttons feed the key source."""
        self._keys.push(key)

    def _on_key(self, event: tk.Event) -> str | None:
        """Match keysym, never event.char: a non-US layout reports jamo for [ and ].

        A widget that consumes the key itself (the URL entry) keeps it: Tk runs the
        toplevel binding after the class binding, so typing there would otherwise
        quit play, resync, toggle Spotify, or trim the offset.
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

    def refresh_track(self) -> None:
        try:
            self._state = SpotifyBridge().current()
        except RuntimeError as exc:
            self._state = None
            self._log(str(exc))
        state = self._state
        self._track_label.configure(
            text=(
                "no Spotify session"
                if state is None
                else f"{state.artist} - {state.title} ({format_length(state.duration_s)})"
            )
        )

    def _run(self, action: str, worker: Callable[[], int]) -> None:
        """Run one operation on a worker thread, capturing stdout into the log pane."""
        if self._busy:
            self._log("[gui] another operation is already running")
            return
        self._busy = True
        self._action = action
        self._sink = LogSink()
        sink = self._sink
        self._update_buttons()
        self._set_status(f"{action} running...")
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
        self._set_hotkeys_visible(False)
        self._update_buttons()
        self._set_status("idle")
        if action == "scan":
            self.refresh_track()

    def play(self) -> None:
        """Start the follow loop, or stop the running one (the Play button is Stop)."""
        if self._action == "play":
            self._keys.push("q")
            return
        self._reset_keys()
        self._run("play", self._play_worker)

    def _play_worker(self) -> int:
        from mvsync import sync_runtime

        return sync_runtime.run_play(None, root=self._root_dir, keys=self._keys.poll)

    def scan(self) -> None:
        self._start_scan(self._url_var.get().strip())

    def use_url(self) -> None:
        url = self._url_var.get().strip()
        if not url:
            self._log("[gui] type a youtube url first")
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
            raise RuntimeError("no Spotify session; open the desktop app and start the track")
        return pick_mv_candidates(state.artist, state.title, state.duration_s, root=self._root_dir)

    def doctor(self) -> None:
        self._run("doctor", self._doctor_worker)

    def _doctor_worker(self) -> int:
        from mvsync.cli import doctor

        return doctor()

    def fix(self) -> None:
        """Open the picker in its own process: a second Tk root in a thread hangs."""
        self._run("fix", self._fix_worker)

    def _fix_worker(self) -> int:
        return subprocess.Popen([sys.executable, *FIX_COMMAND]).wait()

    def _on_close(self) -> None:
        if self._busy and not messagebox.askyesno(
            "mvsync",
            f"A {self._action or 'session'} is running; closing now stops it. Close anyway?",
        ):
            return
        self._closing = True
        self._cancel_pump()
        if self._action == "play":
            self._stop_play()
        self.destroy()

    def _stop_play(self) -> None:
        """Ask the follow loop to quit and wait for its mpv and session cleanup."""
        self._keys.push("q")
        if self._worker is not None:
            self._worker.join(timeout=CLOSE_STOP_TIMEOUT_S)


class FixerWindow(tk.Tk):
    """Pick the right MV for the current Spotify track and rebuild its map."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self.title("mvsync")
        self._root_dir = root
        self._state: SpotifyState | None = None
        self._track_id: str | None = None
        self._candidates: list[Candidate] = []
        self._in_use_video_id: str | None = None
        self._busy = False
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_track()

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self._track_label = ttk.Label(top, text="no Spotify session")
        self._track_label.pack(side="left", fill="x", expand=True)
        self._refresh_button = ttk.Button(
            top, text="Refresh track", command=self.refresh_track
        )
        self._refresh_button.pack(side="right", padx=(8, 0))
        self._search_button = ttk.Button(top, text="Search MVs", command=self.search)
        self._search_button.pack(side="right")

        middle = ttk.Frame(self, padding=(8, 0))
        middle.pack(fill="both", expand=True)
        columns = ("#", "mv", "channel", "length", "status")
        self._tree = ttk.Treeview(middle, columns=columns, show="headings", height=8)
        for column, heading, width in (
            ("#", "#", 40),
            ("mv", "MV", 340),
            ("channel", "Channel", 180),
            ("length", "Length", 60),
            ("status", "Status", 70),
        ):
            self._tree.heading(column, text=heading)
            self._tree.column(column, width=width)
        scrollbar = ttk.Scrollbar(middle, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._tree.bind("<Double-1>", lambda event: self.open_selected())

        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")
        self._open_button = ttk.Button(
            bottom, text="Open in browser", command=self.open_selected
        )
        self._open_button.pack(side="left")
        self._copy_button = ttk.Button(bottom, text="Copy URL", command=self.copy_selected)
        self._copy_button.pack(side="left", padx=4)
        self._use_button = ttk.Button(bottom, text="Use this MV", command=self.use_selected)
        self._use_button.pack(side="left")
        self._status_label = ttk.Label(bottom, text="")
        self._status_label.pack(side="left", padx=8)

        self._log_text = tk.Text(self, height=6, state="disabled", wrap="word")
        self._log_text.pack(fill="x", padx=8, pady=(0, 8))

    def _update_buttons(self) -> None:
        state = "disabled" if self._busy else "normal"
        self._refresh_button.configure(state=state)
        self._search_button.configure(state=state)
        self._use_button.configure(
            state="normal" if self._candidates and not self._busy else "disabled"
        )

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
            self._tree.insert("", "end", iid=str(rank), values=row)

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
            self._state = SpotifyBridge().current()
        except RuntimeError as exc:
            self._state = None
            self._log(str(exc))
        state = self._state
        track_id = make_track_id(state.artist, state.title) if state is not None else None
        if track_id != self._track_id:
            self._track_id = track_id
            self._candidates = []
            self._in_use_video_id = None
        self._track_label.configure(
            text=(
                "no Spotify session"
                if state is None
                else f"{state.artist} - {state.title} ({format_length(state.duration_s)})"
            )
        )
        if track_id is not None:
            in_use = load_newest_alignment(paths.alignments_dir(self._root_dir), track_id)
            self._in_use_video_id = in_use[2] if in_use is not None else None
        self._fill_table()
        self._update_buttons()

    def search(self) -> None:
        state = self._state
        if state is None:
            self._status_label.configure(text="no current track")
            return
        artist, title, duration_s = state.artist, state.title, state.duration_s
        track_id = make_track_id(artist, title)
        self._set_busy(True)
        self._status_label.configure(text="searching...")
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
        self._set_busy(False)
        self._status_label.configure(text=f"{len(candidates)} candidates")

    def _search_failed(self, message: str) -> None:
        self._set_busy(False)
        self._log(message)
        self._status_label.configure(text="search failed")
        messagebox.showerror("mvsync", message)

    def open_selected(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            self._status_label.configure(text="select a candidate first")
            return
        webbrowser.open(video_url(candidate.video_id))

    def copy_selected(self) -> None:
        candidate = self._selected_candidate()
        if candidate is None:
            self._status_label.configure(text="select a candidate first")
            return
        self.clipboard_clear()
        self.clipboard_append(video_url(candidate.video_id))
        self._status_label.configure(text="url copied")

    def use_selected(self) -> None:
        index = self._selected_index()
        state = self._state
        if index is None or state is None or not self._candidates:
            self._status_label.configure(text="select a candidate first")
            return
        candidate = self._candidates[index - 1]
        question = (
            f"Rebuild the map for {state.artist} - {state.title} using "
            f"{candidate.title or candidate.video_id}?\n\n"
            f"Spotify pauses, rewinds, and plays the track once "
            f"({format_length(state.duration_s)}); keep it playing untouched until it ends."
        )
        if not messagebox.askyesno("mvsync", question):
            return
        ordered = prioritize(self._candidates, index)
        self._set_busy(True)
        self._log(f"scanning with {candidate.video_id}")

        def work() -> None:
            try:
                result = scan_with_candidates(
                    ordered,
                    root=self._root_dir,
                    on_progress=lambda message: self.after(0, self._log, message),
                )
            except (ScanAborted, RuntimeError) as exc:
                self.after(0, self._use_failed, str(exc))
                return
            self.after(0, self._use_done, result)

        threading.Thread(target=work, daemon=True).start()

    def _use_done(self, result: ScanResult) -> None:
        messagebox.showinfo("mvsync", f"Saved a new map: {result.video_id}")
        self._set_busy(False)
        self.refresh_track()

    def _use_failed(self, message: str) -> None:
        self._log(message)
        self._set_busy(False)
        messagebox.showerror("mvsync", message)

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
