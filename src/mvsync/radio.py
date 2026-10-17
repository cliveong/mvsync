"""First-listen recording: capture unscanned playlist tracks and sync them later.

In playlist mode a new track still plays its (roughly synced) MV while the
loopback records the song. After the track ends the recording is fitted against
the search candidates in a background thread; the winning alignment makes every
later listen fully synced. A successfully fitted recording is deleted; a
failure keeps the wav so the listen can be refitted by hand. Anything that
would poison the mapping (a seek, a real pause, a recording that stopped early)
discards it instead.
"""

from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path

from mvsync import paths
from mvsync.align_store import save_alignment
from mvsync.capture import SILENT_PEAK_DBFS, LoopbackRecorder
from mvsync.match import (
    BACKWARD_JUMP_S,
    END_EPS_S,
    FORWARD_JUMP_S,
    align_against_candidates,
)
from mvsync.mv_audio import wav_duration_s
from mvsync.mv_search import Candidate
from mvsync.spotify_smtc import SpotifyState

PAUSE_DIRTY_S = 2.0
COVERAGE_SLACK_S = 5.0
MAX_CAPTURE_LOSS_S = 2.0

_pending_fits: list[threading.Thread] = []


def wait_for_fits(timeout_s: float = 240.0) -> None:
    """Block until background fits have finished saving their maps."""
    if _pending_fits:
        print("[play] waiting for the background fit (so this song syncs next time)")
    deadline = time.monotonic() + timeout_s
    while _pending_fits:
        _pending_fits.pop(0).join(max(0.0, deadline - time.monotonic()))


def _capture_lost_audio(expected_s: float, captured_s: float) -> bool:
    """True when the capture ran less audio than the capture window implies."""
    return expected_s - captured_s > MAX_CAPTURE_LOSS_S


class RadioWatch:
    """Pure state watcher deciding whether a first-listen recording is usable."""

    def __init__(self, track_duration_s: float, pause_dirty_s: float = PAUSE_DIRTY_S) -> None:
        self.track_duration_s = track_duration_s
        self.pause_dirty_s = pause_dirty_s
        self.dirty = False
        self._last_raw: float | None = None
        self._last_epoch: float | None = None
        self._paused_since: float | None = None
        self._max_position = 0.0

    def observe(
        self, position_s: float, updated_epoch_s: float, playing: bool, now: float
    ) -> None:
        if self.dirty:
            return
        if updated_epoch_s != self._last_epoch:
            elapsed = updated_epoch_s - (self._last_epoch or updated_epoch_s)
            if self._last_raw is not None and elapsed > 0.0:
                advance = position_s - self._last_raw
                near_end = self._max_position >= self.track_duration_s - END_EPS_S
                if not near_end and (
                    advance < -BACKWARD_JUMP_S or advance > elapsed + FORWARD_JUMP_S
                ):
                    self.dirty = True
            self._last_raw = position_s
            self._last_epoch = updated_epoch_s
        if playing:
            self._max_position = max(self._max_position, position_s)
            self._paused_since = None
        elif self._paused_since is None:
            self._paused_since = now
        elif now - self._paused_since > self.pause_dirty_s:
            self.dirty = True

    def covered(self, captured_s: float, offset_s: float) -> bool:
        return offset_s + captured_s >= self.track_duration_s - COVERAGE_SLACK_S


class RadioRecording:
    """Record the current track during its first listen; fit it after it ends."""

    def __init__(
        self,
        *,
        rec_path: Path,
        candidates: list[Candidate],
        track_id: str,
        artist: str,
        title: str,
        track_duration_s: float,
        root: Path | None,
    ) -> None:
        self._rec_path = rec_path
        self._candidates = candidates
        self._track_id = track_id
        self._artist = artist
        self._title = title
        self._track_duration_s = track_duration_s
        self._root = root
        self._recorder = LoopbackRecorder(rec_path)
        self._watch = RadioWatch(track_duration_s)
        self._offset_s = 0.0
        self._start_wall = 0.0
        self._ready_delay = 0.0
        self._started = False
        self._stopped = False
        self._finished = False

    @property
    def started(self) -> bool:
        return self._started

    def start(self, *, position_s: float, wall: float) -> None:
        self._started = True
        self._start_wall = wall
        self._recorder.start()
        deadline = time.monotonic() + 3.0
        while self._recorder.ready_epoch is None and time.monotonic() < deadline:
            time.sleep(0.05)
        ready = self._recorder.ready_epoch if self._recorder.ready_epoch is not None else wall
        self._ready_delay = ready - wall
        self._offset_s = position_s + self._ready_delay

    def observe(self, state: SpotifyState, now: float) -> None:
        if self._finished or not self._started:
            return
        self._watch.observe(state.position_s, state.updated_epoch_s, state.playing, now)
        if self._watch.dirty and not self._stopped:
            self._stopped = True
            self._recorder.stop()

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if not self._started:
            _discard(self._rec_path)
            return
        self._recorder.stop()
        label = f"{self._artist} - {self._title}"
        if self._watch.dirty:
            print(f"[play] discarded the recording of {label}: playback was interrupted")
            _discard(self._rec_path)
            return
        if self._recorder.peak_db < SILENT_PEAK_DBFS:
            print(f"[play] recording of {label} was silent; nothing saved")
            _discard(self._rec_path)
            return
        captured = wav_duration_s(self._rec_path)
        expected = time.time() - self._start_wall - self._ready_delay
        if _capture_lost_audio(expected, captured):
            print(f"[play] recording of {label} lost audio to a capture hiccup; nothing saved")
            _discard(self._rec_path)
            return
        if not self._watch.covered(captured, self._offset_s):
            print(f"[play] recording of {label} stopped early; nothing saved")
            _discard(self._rec_path)
            return
        print(f"[play] captured {label} ({captured:.0f}s of song); fitting it in the background")
        thread = threading.Thread(
            target=_fit_in_background,
            args=(
                self._rec_path,
                self._candidates,
                self._track_id,
                label,
                self._track_duration_s,
                self._offset_s,
                self._root,
            ),
            name="radio-fit",
            daemon=True,
        )
        thread.start()
        _pending_fits.append(thread)


def _fit_in_background(
    rec_path: Path,
    candidates: list[Candidate],
    track_id: str,
    label: str,
    track_duration_s: float,
    offset_s: float,
    root: Path | None,
) -> None:
    """Fit a first-listen recording and save its map.

    ``offset_s`` is the track position when the recording began. The fit keeps
    the recording's own timeline (no pre-roll is cut), so the saved alignment
    needs ``offset_s=-offset_s``: the planner adds that offset to each query,
    turning a track position into a position on the recording's timeline.
    A successful fit deletes the recording; failures keep the wav file so the
    listen can be refitted by hand.
    """
    try:
        result = align_against_candidates(
            rec_path,
            candidates,
            track_id=track_id,
            track_duration_s=track_duration_s,
            spotify_offset_s=0.0,
            root=root,
        )
        if result is None:
            print(
                f"[play] none of the candidate videos matched {label}; "
                f"kept {rec_path.name} for a manual refit"
            )
            return
        alignment, video_id = result
        saved = save_alignment(
            paths.ensure_dir(paths.alignments_dir(root)), alignment, offset_s=-offset_s
        )
        print(f"[play] {label}: aligned to {video_id} ({saved.name}); synced on the next listen")
    except Exception as exc:  # noqa: BLE001 - a background thread must never kill playback
        print(f"[play] could not fit the recording of {label}: {exc}; kept {rec_path.name}")
        return
    _discard(rec_path)


def _discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
