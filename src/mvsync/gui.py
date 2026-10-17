"""Tkinter MV picker: see the ranked candidates for the current track and rebuild its map."""

from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

from mvsync import paths
from mvsync.align_store import load_newest_alignment, make_track_id
from mvsync.mv_search import Candidate, pick_mv_candidates, prioritize, video_url
from mvsync.scan_runtime import ScanAborted, ScanResult, scan_with_candidates
from mvsync.spotify_smtc import SpotifyBridge, SpotifyState


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


def run_fixer(*, root: Path | None = None) -> int:
    """Open the picker window and run its mainloop."""
    window = FixerWindow(root=root)
    window.mainloop()
    return 0
