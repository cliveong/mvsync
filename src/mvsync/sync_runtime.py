"""M2 play: follow loop driving a muted mpv window from the Spotify master clock.

The alignment built by ``scan`` maps Spotify positions to video positions;
this loop polls SMTC at a few hertz, extrapolates the Spotify position between
updates (SMTC is coarse), and lets the planner decide between pausing, seeking,
and a small rate nudge. With no url the stored alignment for the current track
is reused; an unscanned track plays its found MV roughly right away while
being recorded in the background, so the next listen is fully synced. The loop
follows playlist transitions the same way: a scanned track swaps in its own
synced video, an unscanned one falls back to the rough mode. Hotkeys: q quit,
r resync, [ / ] trim the offset by 50 ms, p toggle Spotify playback. A changed
offset is persisted on exit.
"""

from __future__ import annotations

import contextlib
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from mvsync import devlog, paths, session
from mvsync.align_store import (
    load_alignment,
    load_newest_alignment,
    make_track_id,
    save_alignment,
)
from mvsync.clock import extrapolate
from mvsync.match import BACKWARD_JUMP_S, END_EPS_S, FORWARD_JUMP_S
from mvsync.mpv_ctl import MpvController, MpvError
from mvsync.mv_audio import fetch_video_mp4, parse_video_id
from mvsync.mv_search import Candidate, candidate_lines, pick_mv_candidates, prioritize, video_url
from mvsync.radio import RadioRecording, wait_for_fits
from mvsync.spotify_smtc import SpotifyBridge, SpotifyState
from mvsync.sync_planner import PlannerConfig, SyncState, plan, target_video_s
from mvsync.timeline import Alignment

POLL_S = 0.25
OFFSET_STEP_S = 0.05
RATE_EPSILON = 1e-4
TRACK_END_EPS_S = 1.0
QUEUE_END_GRACE_S = 5.0
MPV_START_TIMEOUT_S = 90.0

HOTKEYS = "q quit | r resync | [ ] offset -/+ 50ms | p spotify play/pause"


@dataclass(frozen=True)
class PreparedTrack:
    """A track's video resolved off the follow loop, ready to be swapped in."""

    alignment: Alignment | None
    offset_s: float
    video_file: Path | None
    candidates: list[Candidate]
    elapsed_s: float = 0.0
    error: str | None = None


class TrackPrep:
    """Resolve a track's video in the background; the follow loop only polls."""

    def __init__(self, work: Callable[[threading.Event], PreparedTrack]) -> None:
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._result: PreparedTrack | None = None
        self._thread = threading.Thread(target=self._run, args=(work,), name="mv-prepare")
        self._thread.daemon = True
        self._thread.start()

    def _run(self, work: Callable[[threading.Event], PreparedTrack]) -> None:
        started = time.monotonic()
        try:
            result = work(self._cancel)
        except Exception as exc:  # noqa: BLE001 - a prep failure must not end playback
            devlog.log("prepare_exception", error=str(exc), traceback=traceback.format_exc())
            result = PreparedTrack(None, 0.0, None, [], time.monotonic() - started, str(exc))
        if result.elapsed_s == 0.0:
            result = replace(result, elapsed_s=time.monotonic() - started)
        self._result = result
        self._done.set()
        devlog.log(
            "prepare_done",
            elapsed_s=round(result.elapsed_s, 2),
            ok=result.video_file is not None,
            error=result.error,
        )

    def cancel(self) -> None:
        self._cancel.set()

    def poll(self) -> PreparedTrack | None:
        """The finished result, or None while the worker is still running."""
        return self._result if self._done.is_set() else None


def run_play(
    url: str | None,
    *,
    root: Path | None = None,
    poll_s: float = POLL_S,
    pick: int | None = None,
    keys: Callable[[], str] | None = None,
) -> int:
    """Follow the playlist, taking hotkeys from ``keys`` (the console by default)."""
    try:
        return _run_play(
            url,
            root=root,
            poll_s=poll_s,
            pick=pick,
            keys=_read_key if keys is None else keys,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"[play] failed: {exc}")
        return 1


def _run_play(
    url: str | None,
    *,
    root: Path | None,
    poll_s: float,
    pick: int | None,
    keys: Callable[[], str],
) -> int:
    log_path = devlog.configure(root)
    if log_path is not None:
        print(f"[log] writing {log_path}")
    devlog.log("play_start", url=url, poll_s=poll_s, pick=pick)
    bridge = SpotifyBridge()
    state = bridge.current()
    if state is None:
        print("[play] no Spotify session; open the desktop app and start the track")
        return 1
    track_id = make_track_id(state.artist, state.title)
    print(f"[play] {state.artist} - {state.title}")
    store = paths.alignments_dir(root)
    alignment: Alignment | None = None
    offset_s = 0.0
    video_id: str | None = None
    if url:
        video_id = parse_video_id(url)
        stored = load_alignment(store, video_id, track_id)
        if stored is not None:
            alignment, offset_s = stored
    else:
        stored = load_newest_alignment(store, track_id)
        if stored is not None:
            alignment, offset_s, video_id = stored

    mpv = MpvController(root)
    session.claim(root)
    try:
        mpv.start()
    except MpvError as exc:
        session.release(root)
        print(f"[play] {exc}")
        return 1
    radio: RadioRecording | None = None
    frozen = False
    try:
        print(f"[play] {HOTKEYS}")
        if alignment is not None:
            print(f"[play] {len(alignment.anchors)} anchors, {len(alignment.skips)} skips")
            print("[play] preparing the video (fetching once if not cached)")
            try:
                video_file = fetch_video_mp4(video_url(video_id), root=root)
            except RuntimeError as exc:
                print(f"[play] could not fetch the video ({exc}); continuing without a video")
                _freeze_video(mpv)
                alignment = None
                offset_s = 0.0
                frozen = True
            else:
                print("[play] loading the video in mpv (muted)")
                mpv.load(str(video_file))
                if not _wait_media_loaded(mpv, video_file):
                    print("[play] mpv did not start playback in time")
                    return 1
        else:
            _, _, radio, frozen = _start_track(
                state, mpv, store, root, pick=pick, url=url, announce=False
            )
        return _follow(
            bridge,
            mpv,
            track_id,
            alignment,
            offset_s,
            root,
            poll_s,
            radio=radio,
            frozen=frozen,
            keys=keys,
        )
    finally:
        mpv.stop()
        session.release(root)
        wait_for_fits()


def _follow(
    bridge: SpotifyBridge,
    mpv: MpvController,
    track_id: str,
    alignment: Alignment | None,
    offset_s: float,
    root: Path | None,
    poll_s: float,
    radio: RadioRecording | None = None,
    frozen: bool = False,
    keys: Callable[[], str] | None = None,
) -> int:
    """Poll Spotify and drive mpv from the planner; ``keys`` supplies the hotkeys."""
    config = PlannerConfig()
    store = paths.alignments_dir(root)
    keys = _read_key if keys is None else keys
    last_rate = 1.0
    offset = offset_s
    offset_dirty = False
    idle_since: float | None = None
    rough_prev_position: float | None = None
    rough_prev_wall: float | None = None
    rough_prev_video: float | None = None
    prep: TrackPrep | None = None
    try:
        while True:
            key = keys()
            now = time.time()
            smtc_started = time.monotonic()
            state = bridge.current()
            smtc_ms = round((time.monotonic() - smtc_started) * 1000.0, 1)
            if state is None:
                print("[play] Spotify session disappeared; stopping")
                break

            new_track_id = make_track_id(state.artist, state.title)
            if new_track_id != track_id:
                if radio is not None:
                    radio.finish()
                    radio = None
                if prep is not None:
                    prep.cancel()
                    prep = None
                if offset_dirty:
                    save_alignment(paths.ensure_dir(store), alignment, offset_s=offset)
                    print(f"[play] saved offset {offset:+.2f}s")
                    offset_dirty = False
                devlog.log(
                    "track",
                    old=track_id,
                    new=new_track_id,
                    position_s=round(state.position_s, 3),
                )
                track_id, alignment, offset, radio, prep = _begin_track(
                    state, mpv, store, root
                )
                frozen = prep is None and alignment is None
                last_rate = 1.0
                idle_since = None
                rough_prev_position = rough_prev_wall = rough_prev_video = None
                continue

            devlog.log(
                "poll",
                track_id=track_id,
                raw_s=round(state.position_s, 3),
                duration_s=round(state.duration_s, 3),
                playing=state.playing,
                smtc_ms=smtc_ms,
            )

            if key == "q":
                break
            if key in ("[", "]"):
                offset += OFFSET_STEP_S if key == "]" else -OFFSET_STEP_S
                offset_dirty = alignment is not None
                print(f"[play] offset {offset:+.2f}s")
            elif key == "p":
                bridge.toggle_play_pause()

            position = extrapolate(state.position_s, state.updated_epoch_s, now, state.playing)
            if radio is not None and not radio.started and state.playing:
                radio.start(position_s=position, wall=now)
            if radio is not None:
                radio.observe(state, now)
            if prep is not None:
                prepared = prep.poll()
                if prepared is None:
                    _freeze_video(mpv)
                    time.sleep(poll_s)
                    continue
                prep = None
                alignment, offset, frozen, radio = _apply_prepared(
                    mpv, prepared, radio, position
                )
            if key == "r":
                if alignment is not None:
                    target = target_video_s(alignment, position, offset)
                else:
                    target = max(0.0, position)
                mpv.seek_absolute(target)
                print(f"[play] resynced to {target:.1f}s")

            try:
                video_position = mpv.time_pos
                video_eof = mpv.eof_reached
            except Exception:  # noqa: BLE001 - mpv was closed from its own window
                print("[play] mpv closed; stopping")
                break
            if alignment is None:
                if rough_prev_position is not None and _track_wrapped(
                    rough_prev_position, position, state.duration_s
                ):
                    if radio is not None:
                        radio.finish()
                        radio = None
                    rough_prev_position = rough_prev_wall = rough_prev_video = None
                    target = max(0.0, position)
                    mpv.seek_absolute(target)
                    print(f"[play] followed the repeat (rough mode) to {target:.0f}s")
                    devlog.log("rough_seek", reason="repeat", target_s=round(target, 2))
                elif (
                    video_position is not None
                    and rough_prev_position is not None
                    and rough_prev_wall is not None
                    and rough_prev_video is not None
                ):
                    delta = _rough_jump_delta(
                        rough_prev_position,
                        rough_prev_wall,
                        position,
                        now,
                        state.duration_s,
                    )
                    if delta is not None:
                        target = max(0.0, rough_prev_video + delta)
                        mpv.seek_absolute(target)
                        print(f"[play] followed your seek (rough mode) to {target:.0f}s")
                        devlog.log("rough_seek", reason="seek", target_s=round(target, 2))
                wanted_pause = frozen or not state.playing or video_eof
                if mpv.paused != wanted_pause:
                    mpv.set_paused(wanted_pause)
                    devlog.log(
                        "rough_pause",
                        paused=wanted_pause,
                        spotify_s=round(position, 3),
                        video_s=None if video_position is None else round(video_position, 3),
                    )
                rough_prev_position = position
                rough_prev_wall = now
                rough_prev_video = video_position
            elif video_position is not None:
                rough_prev_position = rough_prev_wall = rough_prev_video = None
                action = plan(
                    SyncState(position, state.playing, video_position, mpv.paused),
                    alignment,
                    offset,
                    config,
                )
                devlog.log(
                    "plan",
                    spotify_s=round(position, 3),
                    video_s=round(video_position, 3),
                    paused=mpv.paused,
                    pause=action.pause_video,
                    seek=action.seek_video_to_s,
                    rate=action.rate,
                )
                last_rate = _apply(mpv, action, last_rate)

            track_duration = (
                alignment.track_duration_s if alignment is not None else state.duration_s
            )
            if not state.playing and position >= track_duration - TRACK_END_EPS_S:
                if idle_since is None:
                    idle_since = now
                elif now - idle_since > QUEUE_END_GRACE_S:
                    print("[play] playback stopped at the end; done")
                    break
            else:
                idle_since = None
            time.sleep(poll_s)
    except KeyboardInterrupt:
        print()
    finally:
        if radio is not None:
            radio.finish()
        if prep is not None:
            prep.cancel()
        if offset_dirty:
            save_alignment(paths.ensure_dir(store), alignment, offset_s=offset)
            print(f"[play] saved offset {offset:+.2f}s")
    return 0


def _freeze_video(mpv: MpvController) -> None:
    """Stop showing a video that belongs to the previous track."""
    with contextlib.suppress(Exception):  # mpv may already be closed
        mpv.set_paused(True)


def _track_wrapped(prev_position: float, position: float, duration_s: float) -> bool:
    """True when the track timeline fell from the end back to the start (repeat-one)."""
    return (
        duration_s > 0
        and prev_position >= duration_s - END_EPS_S
        and position < END_EPS_S
        and position < prev_position
    )


def _rough_jump_delta(
    prev_position: float,
    prev_wall: float,
    position: float,
    now: float,
    duration_s: float,
) -> float | None:
    """The position delta of an abrupt seek, or None for normal progress.

    End-of-track timeline resets are ignored: the metadata switch handles the
    actual track change a moment later.
    """
    if max(prev_position, position) >= duration_s - END_EPS_S:
        return None
    delta = position - prev_position
    if delta < -BACKWARD_JUMP_S or delta > (now - prev_wall) + FORWARD_JUMP_S:
        return delta
    return None


def _start_track(
    state: SpotifyState,
    mpv: MpvController,
    store: Path,
    root: Path | None,
    *,
    pick: int | None = None,
    url: str | None = None,
    announce: bool = True,
) -> tuple[Alignment | None, float, RadioRecording | None, bool]:
    """Play the track Spotify is on now; return its alignment, radio, and freeze flag.

    A stored alignment swaps in the synced video right away. Otherwise the
    found MV plays roughly while the track is recorded for the next listen.
    When no video can be shown (no MV found, fetch failed) the previous video
    is frozen and the follow loop keeps running: the session survives a broken
    download and picks up the next track normally.
    """
    track_id = make_track_id(state.artist, state.title)
    if not state.artist.strip() and not state.title.strip():
        print("[play] Spotify reports no track metadata; waiting for it to update")
        _freeze_video(mpv)
        return None, 0.0, None, True
    if url is None:
        stored = load_newest_alignment(store, track_id)
        if stored is not None:
            alignment, offset_s, video_id = stored
            if announce:
                print(f"[play] now: {state.artist} - {state.title} (synced)")
            try:
                video_file = fetch_video_mp4(video_url(video_id), root=root)
            except RuntimeError as exc:
                print(f"[play] could not fetch the video ({exc}); continuing without a video")
                _freeze_video(mpv)
                return None, 0.0, None, True
            _swap_video(mpv, video_file)
            return alignment, offset_s, None, False

    if announce:
        print(f"[play] now: {state.artist} - {state.title} (new: playing the MV roughly)")
    else:
        print("[play] new track: playing the MV roughly, synced on the next listen")
    if not state.playing:
        print("[play] Spotify is paused; the first-listen recording starts when playback does")
    if url is not None:
        candidates = [
            Candidate(video_id=parse_video_id(url), title="", duration_s=None, channel="")
        ]
    else:
        try:
            candidates = pick_mv_candidates(state.artist, state.title, state.duration_s, root=root)
        except RuntimeError as exc:
            print(f"[play] no MV found ({exc}); continuing without a video")
            _freeze_video(mpv)
            return None, 0.0, None, True
        if pick is not None:
            for line in candidate_lines(candidates):
                print(f"[play] {line}")
        candidates = prioritize(candidates, pick)

    wall = time.time()
    position = extrapolate(state.position_s, state.updated_epoch_s, wall, state.playing)
    radio = RadioRecording(
        rec_path=paths.ensure_dir(paths.recordings_dir(root))
        / f"{candidates[0].video_id}__{track_id}.wav",
        candidates=candidates,
        track_id=track_id,
        artist=state.artist,
        title=state.title,
        track_duration_s=state.duration_s,
        root=root,
    )
    if state.playing:
        radio.start(position_s=position, wall=wall)
    try:
        video_file = fetch_video_mp4(video_url(candidates[0].video_id), root=root)
    except RuntimeError as exc:
        print(f"[play] could not fetch the video ({exc}); no video for this track")
        _freeze_video(mpv)
        return None, 0.0, radio, True
    seek_to = position + max(0.0, time.time() - wall) if state.playing else position
    _swap_video(mpv, video_file, seek_to=seek_to)
    return None, 0.0, radio, False


def _prepare_track(
    state: SpotifyState,
    stored: tuple[Alignment, float, str] | None,
    root: Path | None,
    cancel: threading.Event,
) -> PreparedTrack:
    """Resolve one track's video; runs off the follow loop in the prep worker."""
    started = time.monotonic()
    if stored is not None:
        alignment, offset_s, video_id = stored
        if cancel.is_set():
            return PreparedTrack(alignment, offset_s, None, [], time.monotonic() - started)
        try:
            video_file = fetch_video_mp4(video_url(video_id), root=root)
        except RuntimeError as exc:
            return PreparedTrack(
                alignment, offset_s, None, [], time.monotonic() - started, str(exc)
            )
        devlog.log(
            "fetch_done",
            video_id=video_id,
            mapped=True,
            elapsed_s=round(time.monotonic() - started, 2),
        )
        return PreparedTrack(alignment, offset_s, video_file, [], time.monotonic() - started)

    search_started = time.monotonic()
    try:
        candidates = pick_mv_candidates(state.artist, state.title, state.duration_s, root=root)
    except RuntimeError as exc:
        return PreparedTrack(None, 0.0, None, [], time.monotonic() - started, str(exc))
    devlog.log(
        "search_picked",
        artist=state.artist,
        title=state.title,
        elapsed_s=round(time.monotonic() - search_started, 2),
        candidates=[candidate.video_id for candidate in candidates[:4]],
    )
    if cancel.is_set():
        return PreparedTrack(None, 0.0, None, candidates, time.monotonic() - started)
    try:
        video_file = fetch_video_mp4(video_url(candidates[0].video_id), root=root)
    except RuntimeError as exc:
        return PreparedTrack(None, 0.0, None, candidates, time.monotonic() - started, str(exc))
    devlog.log(
        "fetch_done",
        video_id=candidates[0].video_id,
        mapped=False,
        elapsed_s=round(time.monotonic() - started, 2),
    )
    return PreparedTrack(None, 0.0, video_file, candidates, time.monotonic() - started)


def _begin_track(
    state: SpotifyState,
    mpv: MpvController,
    store: Path,
    root: Path | None,
) -> tuple[str, Alignment | None, float, RadioRecording | None, TrackPrep | None]:
    """Freeze the old video, start recording a first listen, prepare the new video."""
    track_id = make_track_id(state.artist, state.title)
    if not state.artist.strip() and not state.title.strip():
        print("[play] Spotify reports no track metadata; waiting for it to update")
        _freeze_video(mpv)
        return track_id, None, 0.0, None, None
    stored = load_newest_alignment(store, track_id)
    radio: RadioRecording | None = None
    if stored is not None:
        print(f"[play] now: {state.artist} - {state.title} (synced); loading its video")
    else:
        print(f"[play] now: {state.artist} - {state.title} (new: finding the MV)")
        wall = time.time()
        position = extrapolate(state.position_s, state.updated_epoch_s, wall, state.playing)
        radio = RadioRecording(
            rec_path=paths.ensure_dir(paths.recordings_dir(root)) / f"pending__{track_id}.wav",
            candidates=[],
            track_id=track_id,
            artist=state.artist,
            title=state.title,
            track_duration_s=state.duration_s,
            root=root,
        )
        if state.playing:
            radio.start(position_s=position, wall=wall)
    _freeze_video(mpv)
    devlog.log(
        "prepare_start",
        track_id=track_id,
        mapped=stored is not None,
        position_s=round(state.position_s, 3),
    )
    prep = TrackPrep(lambda cancel: _prepare_track(state, stored, root, cancel))
    return track_id, None, 0.0, radio, prep


def _apply_prepared(
    mpv: MpvController,
    prepared: PreparedTrack,
    radio: RadioRecording | None,
    position: float,
) -> tuple[Alignment | None, float, bool, RadioRecording | None]:
    """Swap in a prepared video on the loop thread; keep a recording that can fit."""
    if prepared.video_file is None:
        if prepared.error:
            print(f"[play] could not prepare the video ({prepared.error})")
        if radio is not None:
            if prepared.candidates:
                radio.set_candidates(prepared.candidates)
            else:
                radio.finish()
                radio = None
        _freeze_video(mpv)
        devlog.log("prepare_applied", frozen=True, error=prepared.error)
        return None, 0.0, True, radio
    seek_to = position
    if prepared.alignment is not None:
        seek_to = target_video_s(prepared.alignment, position, prepared.offset_s)
    try:
        _swap_video(mpv, prepared.video_file, seek_to=seek_to)
    except Exception as exc:  # noqa: BLE001 - a bad load must not end the session
        print(f"[play] could not show the new video ({exc}); continuing without a video")
        if radio is not None:
            if prepared.candidates:
                radio.set_candidates(prepared.candidates)
            else:
                radio.finish()
                radio = None
        _freeze_video(mpv)
        devlog.log("prepare_applied", frozen=True, error=str(exc))
        return None, 0.0, True, radio
    if radio is not None:
        radio.set_candidates(prepared.candidates)
    print(f"[play] video ready after {prepared.elapsed_s:.1f}s")
    devlog.log(
        "prepare_applied",
        frozen=False,
        seek_s=round(seek_to, 2),
        elapsed_s=round(prepared.elapsed_s, 2),
    )
    return prepared.alignment, prepared.offset_s, False, radio


def _swap_video(
    mpv: MpvController, video_file: Path, *, seek_to: float | None = None
) -> None:
    started = time.monotonic()
    fullscreen: bool | None = None
    with contextlib.suppress(Exception):
        fullscreen = mpv.fullscreen
    mpv.load(str(video_file))
    loaded = _wait_media_loaded(mpv, video_file)
    if fullscreen is not None:
        with contextlib.suppress(Exception):
            if mpv.fullscreen != fullscreen:
                mpv.set_fullscreen(fullscreen)
    if not loaded:
        raise MpvError("mpv did not load the new video in time")
    mpv.set_rate(1.0)
    if seek_to is not None:
        mpv.seek_absolute(max(0.0, seek_to))
    devlog.log(
        "swap",
        file=video_file.name,
        seek_s=None if seek_to is None else round(seek_to, 2),
        fullscreen=fullscreen,
        elapsed_s=round(time.monotonic() - started, 2),
    )


def _apply(mpv: MpvController, action, last_rate: float) -> float:
    if action.pause_video is not None and mpv.paused != action.pause_video:
        mpv.set_paused(action.pause_video)
    if action.seek_video_to_s is not None:
        mpv.seek_absolute(action.seek_video_to_s)
    if action.rate is not None and abs(action.rate - last_rate) > RATE_EPSILON:
        mpv.set_rate(action.rate)
        return action.rate
    return last_rate


def _wait_media_loaded(
    mpv: MpvController, video_file: Path, timeout_s: float = MPV_START_TIMEOUT_S
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if mpv.path.endswith(video_file.name) and mpv.duration is not None:
                return True
        except Exception:  # noqa: BLE001 - mpv still starting or switching media
            pass
        time.sleep(0.2)
    with contextlib.suppress(Exception):
        devlog.log(
            "media_load_timeout",
            file=video_file.name,
            timeout_s=timeout_s,
            mpv_path=str(mpv.path),
            duration=mpv.duration,
        )
    return False


def _read_key() -> str:
    import msvcrt

    if msvcrt.kbhit():
        return msvcrt.getwch().lower()
    return ""
