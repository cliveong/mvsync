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
import time
from pathlib import Path

from mvsync import paths, session
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


def run_play(
    url: str | None,
    *,
    root: Path | None = None,
    poll_s: float = POLL_S,
    pick: int | None = None,
) -> int:
    try:
        return _run_play(url, root=root, poll_s=poll_s, pick=pick)
    except (RuntimeError, ValueError) as exc:
        print(f"[play] failed: {exc}")
        return 1


def _run_play(
    url: str | None, *, root: Path | None, poll_s: float, pick: int | None
) -> int:
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
            bridge, mpv, track_id, alignment, offset_s, root, poll_s, radio=radio, frozen=frozen
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
) -> int:
    config = PlannerConfig()
    store = paths.alignments_dir(root)
    last_rate = 1.0
    offset = offset_s
    offset_dirty = False
    idle_since: float | None = None
    rough_prev_position: float | None = None
    rough_prev_wall: float | None = None
    rough_prev_video: float | None = None
    try:
        while True:
            key = _read_key()
            now = time.time()
            state = bridge.current()
            if state is None:
                print("[play] Spotify session disappeared; stopping")
                break

            new_track_id = make_track_id(state.artist, state.title)
            if new_track_id != track_id:
                if radio is not None:
                    radio.finish()
                    radio = None
                if offset_dirty:
                    save_alignment(paths.ensure_dir(store), alignment, offset_s=offset)
                    print(f"[play] saved offset {offset:+.2f}s")
                    offset_dirty = False
                track_id = new_track_id
                alignment, offset, radio, frozen = _start_track(state, mpv, store, root)
                last_rate = 1.0
                idle_since = None
                rough_prev_position = rough_prev_wall = rough_prev_video = None
                continue

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
                wanted_pause = frozen or not state.playing or video_eof
                if mpv.paused != wanted_pause:
                    mpv.set_paused(wanted_pause)
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


def _swap_video(
    mpv: MpvController, video_file: Path, *, seek_to: float | None = None
) -> None:
    mpv.load(str(video_file))
    if not _wait_media_loaded(mpv, video_file):
        raise MpvError("mpv did not load the new video in time")
    mpv.set_rate(1.0)
    if seek_to is not None:
        mpv.seek_absolute(max(0.0, seek_to))


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
    return False


def _read_key() -> str:
    import msvcrt

    if msvcrt.kbhit():
        return msvcrt.getwch().lower()
    return ""
