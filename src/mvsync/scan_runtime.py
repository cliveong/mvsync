"""M1 scan: record one Spotify track end to end and build the alignment map.

The flow drives Spotify over SMTC: pause, rewind to the start, record the
system output (loopback) while the track plays through, abort cleanly on
pause/seek/track change, trim the pre-roll silence, and run the DSP alignment
of the recording against the cached MV audio. The result is saved under
``data/alignments`` and later reused by ``run_play``; the recording itself is
deleted with the map (or on abort) so ``data/recordings`` does not grow.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mvsync import devlog, paths, session
from mvsync.align_store import (
    alignment_path,
    make_track_id,
    prune_other_alignments,
    save_alignment,
)
from mvsync.capture import SILENT_PEAK_DBFS, make_recorder
from mvsync.clock import extrapolate
from mvsync.match import BACKWARD_JUMP_S, END_EPS_S, FORWARD_JUMP_S, align_against_candidates
from mvsync.mv_audio import fetch_video_mp4, parse_video_id, wav_duration_s
from mvsync.mv_search import Candidate, candidate_lines, pick_mv_candidates, prioritize, video_url
from mvsync.radio import _discard
from mvsync.spotify_smtc import SpotifyBridge, SpotifyState

POLL_S = 0.25
TRIM_SAFETY_S = 0.05
START_GRACE_S = 5.0
STOP_GRACE_S = 3.0

_DEBUG = os.environ.get("MVSYNC_SCAN_DEBUG") == "1"


class ScanAborted(RuntimeError):
    pass


@dataclass(frozen=True)
class ScanResult:
    """The video chosen for the track and the map that was saved."""

    video_id: str
    path: Path


def run_scan(url: str | None, *, root: Path | None = None, pick: int | None = None) -> int:
    log_path = devlog.configure(root)
    if log_path is not None:
        print(f"[log] writing {log_path}")
    devlog.log("scan_start", url=url, pick=pick)
    try:
        session.claim(root)
        try:
            _run_scan(url, root=root, pick=pick)
        finally:
            session.release(root)
    except (RuntimeError, ValueError) as exc:
        print(f"[scan] aborted: {exc}")
        return 1
    return 0


def _run_scan(
    url: str | None, *, root: Path | None = None, pick: int | None = None
) -> None:
    bridge = SpotifyBridge()
    state = bridge.current()
    if state is None:
        raise ScanAborted("no Spotify session; open the desktop app and start the track")
    if not state.artist.strip() and not state.title.strip():
        raise ScanAborted("Spotify reports no track metadata; start the track first")
    track_id = make_track_id(state.artist, state.title)
    print(f"[scan] track: {state.artist} - {state.title} ({state.duration_s:.1f}s)")
    if url:
        candidates = [
            Candidate(video_id=parse_video_id(url), title="", duration_s=None, channel="")
        ]
    else:
        print("[scan] searching youtube for the MV")
        candidates = pick_mv_candidates(state.artist, state.title, state.duration_s, root=root)
    for line in candidate_lines(candidates):
        print(f"[scan] {line}")
    if not url and pick is not None:
        candidates = prioritize(candidates, pick)
        print(f"[scan] using candidate {pick}: {candidates[0].video_id}")
    elif not url:
        print("[scan] you can still switch the MV at the ready prompt (type its number)")

    position = _pause_and_rewind(bridge, lambda message: None)

    input_prompt = (
        "[scan] press Enter to start recording and playback, "
        "or type a candidate number / url to change the MV: "
    )
    while True:
        try:
            answer = input(input_prompt)
        except EOFError:
            break
        if _apply_prompt_answer(candidates, answer):
            break
    _record_and_fit(
        candidates,
        bridge=bridge,
        state=state,
        track_id=track_id,
        position=position,
        root=root,
        on_progress=lambda message: None,
    )


def _pause_and_rewind(bridge: SpotifyBridge, on_progress: Callable[[str], None]) -> float:
    """Pause playback, rewind to the start, and report the resulting position."""
    state = bridge.current()
    if state is not None and state.playing:
        print("[scan] pausing Spotify")
        on_progress("pausing")
        bridge.toggle_play_pause()
        if not _wait_until(lambda: _playback_stopped(bridge), 3.0):
            raise ScanAborted("could not pause Spotify")
    bridge.seek(0.0)
    position = _wait_start_position(bridge)
    if position > 2.0:
        print(f"[scan] warning: Spotify reports {position:.1f}s, scanning from there")
    return position


def _record_and_fit(
    candidates: list[Candidate],
    *,
    bridge: SpotifyBridge,
    state: SpotifyState,
    track_id: str,
    position: float,
    root: Path | None,
    on_progress: Callable[[str], None],
) -> ScanResult:
    """Record one listen of the track, fit it against the candidates, and save the map."""
    rec_path = paths.recordings_dir(root) / f"{candidates[0].video_id}__{track_id}.wav"
    print(f"[scan] recording to {rec_path.name}")
    try:
        recorder = make_recorder(rec_path, root=root)
    except RuntimeError as exc:
        raise ScanAborted(str(exc)) from exc
    try:
        recorder.start()
        on_progress("recording")
        if not _wait_until(lambda: recorder.ready_epoch is not None, 3.0):
            recorder.stop()
            raise ScanAborted("the loopback device did not start")
        play_press = time.time()
        bridge.toggle_play_pause()
        print("[scan] playing; press nothing in Spotify until the track ends")
        try:
            _record_until_end(bridge, track_id, state.duration_s, play_press)
        finally:
            stop_wall = time.time()
            recorder.stop()

        if recorder.peak_db < SILENT_PEAK_DBFS:
            if getattr(recorder, "backend", "device") == "spotify":
                hint = "Spotify had no audio stream (app closed, or paused with nothing rendering)"
            else:
                hint = "check that Spotify plays through the default output device"
            raise ScanAborted(f"recording looks silent ({recorder.peak_db:.1f} dBFS peak); {hint}")

        ready = recorder.ready_epoch if recorder.ready_epoch is not None else play_press
        wall_s = stop_wall - ready
        captured_s = wav_duration_s(rec_path)
        print(f"[scan] captured {captured_s:.2f}s across {wall_s:.2f}s of wall time")
        if wall_s - captured_s > 0.5:
            print(
                f"[scan] warning: about {(wall_s - captured_s) * 1000:.0f} ms of audio was lost "
                "to a device hiccup; rescan if the sync looks off"
            )
        trim_s = max(0.0, play_press - ready - TRIM_SAFETY_S)
        print(f"[scan] peak {recorder.peak_db:.1f} dBFS; trimming {trim_s:.2f}s pre-roll")
        print("[scan] fitting the recording against the candidates (this takes a moment)")
        on_progress("fitting")
        result = align_against_candidates(
            rec_path,
            candidates,
            track_id=track_id,
            track_duration_s=state.duration_s,
            spotify_offset_s=trim_s,
            root=root,
        )
        if result is None:
            raise ScanAborted("no candidate matched the recording; rerun with the youtube url")
        alignment, video_id = result

        if rec_path.name != f"{video_id}__{track_id}.wav":
            renamed = rec_path.with_name(f"{video_id}__{track_id}.wav")
            with contextlib.suppress(OSError):
                os.replace(rec_path, renamed)
                rec_path = renamed

        print("[scan] fetching the MV video (cached after the first run)")
        video_file = fetch_video_mp4(video_url(video_id), root=root)
        print(f"[scan] MV video: {video_file.name} ({video_file.stat().st_size / 1e6:.0f} MB)")

        for span in alignment.skips:
            print(
                f"[scan] skip {span.reason}: video "
                f"{span.video_start_s:.1f}s - {span.video_end_s:.1f}s"
            )
        for segment in alignment.segments:
            print(
                f"[scan] segment video {segment.video_start_s:.1f}s - "
                f"{segment.video_end_s:.1f}s, rate {segment.rate:.3f}"
            )
        if any(abs(segment.rate - 1.0) > 0.05 for segment in alignment.segments):
            print("[scan] warning: a segment rate deviates from 1.0; listen for drift")

        store = paths.ensure_dir(paths.alignments_dir(root))
        if alignment_path(store, video_id, track_id).is_file():
            print("[scan] overwriting the existing alignment")
        saved = save_alignment(store, alignment, offset_s=-position)
        print(f"[scan] saved {saved.name}")
        removed = prune_other_alignments(store, track_id, video_id)
        if removed:
            print(f"[scan] removed {len(removed)} superseded alignment(s)")
        on_progress("saved")
        return ScanResult(video_id=video_id, path=saved)
    finally:
        _discard(rec_path)


def scan_with_candidates(
    candidates: list[Candidate],
    *,
    root: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ScanResult:
    """Record one listen of the current track and fit it against candidates in order."""
    notify = on_progress or (lambda message: None)
    session.claim(root)
    try:
        bridge = SpotifyBridge()
        state = bridge.current()
        if state is None or (not state.artist.strip() and not state.title.strip()):
            raise ScanAborted("no Spotify session with track metadata")
        track_id = make_track_id(state.artist, state.title)
        position = _pause_and_rewind(bridge, notify)
        rec_path = paths.recordings_dir(root) / f"{candidates[0].video_id}__{track_id}.wav"
        try:
            return _record_and_fit(
                candidates,
                bridge=bridge,
                state=state,
                track_id=track_id,
                position=position,
                root=root,
                on_progress=notify,
            )
        finally:
            _discard(rec_path)
    finally:
        session.release(root)


def _apply_prompt_answer(candidates: list[Candidate], answer: str) -> bool:
    """Apply a typed answer at the ready prompt; True starts the scan.

    An empty answer accepts the current pick. A number (as printed in the
    candidate list) reorders in place, a YouTube url (or bare video id)
    replaces the candidates entirely.
    """
    answer = answer.strip()
    if not answer:
        return True
    if answer.isdigit():
        try:
            candidates[:] = prioritize(candidates, int(answer))
        except ValueError as exc:
            print(f"[scan] {exc}")
            return False
        print(f"[scan] using candidate {answer}: {candidates[0].video_id}")
        return False
    try:
        video_id = parse_video_id(answer)
    except ValueError:
        print(f"[scan] expecting a candidate number, a youtube url, or a video id: {answer!r}")
        return False
    candidates[:] = [Candidate(video_id=video_id, title="", duration_s=None, channel="")]
    print(f"[scan] using video {video_id}")
    return False


def _wait_until(predicate: Callable[[], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _playback_stopped(bridge: SpotifyBridge) -> bool:
    state = bridge.current()
    return state is not None and not state.playing


def _wait_start_position(bridge: SpotifyBridge) -> float:
    position = 0.0
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        state = bridge.current()
        if state is not None:
            position = state.position_s
            if position < 2.0:
                break
        time.sleep(0.1)
    return position


def _record_until_end(
    bridge: SpotifyBridge, track_id: str, duration_s: float, play_press: float
) -> None:
    """Watch the track play through, aborting on pauses, seeks, or track changes.

    Seek detection compares raw positions across timeline refreshes only. Between
    refreshes the reported position is stale, and the refresh after playback
    starts follows a paused, rewound timeline entry, so the first refresh is
    always rebased without checking. Resumes after any observed stop are rebased
    too, since the elapsed wall time then no longer matches the advance.
    """
    deadline = play_press + duration_s + 30.0
    max_position = 0.0
    started = False
    saw_stopped = True
    last_raw: float | None = None
    last_epoch: float | None = None
    while True:
        now = time.time()
        if now > deadline:
            raise ScanAborted("timed out waiting for the track to finish")
        state = bridge.current()
        if state is None:
            raise ScanAborted("Spotify session disappeared while recording")
        if make_track_id(state.artist, state.title) != track_id:
            if max_position >= duration_s - END_EPS_S:
                return
            raise ScanAborted("track changed before the end; nothing was saved")
        if state.updated_epoch_s != last_epoch:
            elapsed = state.updated_epoch_s - (last_epoch or state.updated_epoch_s)
            if last_raw is not None and not saw_stopped and elapsed > 0.0:
                advance = state.position_s - last_raw
                # At the very end the timeline can reset for the next track
                # before the metadata switches, so ignore jumps there.
                near_end = max_position >= duration_s - END_EPS_S
                if advance < -BACKWARD_JUMP_S and not near_end:
                    raise ScanAborted("Spotify moved backwards; scan the track without seeking")
                if advance > elapsed + FORWARD_JUMP_S and not near_end:
                    raise ScanAborted("Spotify jumped forward; scan the track without seeking")
            last_raw = state.position_s
            last_epoch = state.updated_epoch_s
            saw_stopped = False
        position = extrapolate(state.position_s, state.updated_epoch_s, now, state.playing)
        if _DEBUG:
            print(
                f"[scan] poll raw={state.position_s:7.2f} age={now - state.updated_epoch_s:5.2f} "
                f"playing={int(state.playing)} est={position:7.2f} max={max_position:7.2f}"
            )
        if state.playing:
            started = True
            max_position = max(max_position, position)
        else:
            saw_stopped = True
            if started and now - play_press > STOP_GRACE_S:
                if max_position >= duration_s - END_EPS_S:
                    return
                raise ScanAborted("playback stopped before the track ended; nothing was saved")
            elif not started and now - play_press > START_GRACE_S:
                raise ScanAborted("playback did not start; check the Spotify window")
        time.sleep(POLL_S)
