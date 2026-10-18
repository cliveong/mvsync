"""Tests for the scan runtime playback monitor.

The monitor is IO-bound in production (SMTC polling at 0.25s), so these tests
drive it with a fake wall clock and a bridge whose states are computed from
that clock. This exercises the seek/pause/track-change detection without
touching Spotify or waiting real time.
"""

from __future__ import annotations

import math

import pytest

from mvsync.align_store import make_track_id
from mvsync.mv_search import Candidate
from mvsync.scan_runtime import ScanAborted, _apply_prompt_answer, _record_until_end
from mvsync.spotify_smtc import SpotifyState
from mvsync.timeline import Alignment, Anchor

PLAY_PRESS = 1000.0
DURATION = 20.0
CADENCE_S = 4.5
TRACK_ID = make_track_id("Artist", "Title")


class FakeClock:
    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeBridge:
    def __init__(self, clock: FakeClock, state_fn) -> None:
        self.clock = clock
        self.state_fn = state_fn

    def current(self) -> SpotifyState:
        return self.state_fn(self.clock.now)


def make_state(
    position_s: float,
    updated_epoch_s: float,
    playing: bool,
    title: str = "Title",
    artist: str = "Artist",
) -> SpotifyState:
    return SpotifyState(
        title=title,
        artist=artist,
        duration_s=DURATION,
        position_s=position_s,
        updated_epoch_s=updated_epoch_s,
        playing=playing,
    )


def cadence_epoch(now: float, base: float = PLAY_PRESS) -> float:
    return base + math.floor((now - base) / CADENCE_S) * CADENCE_S


def run_monitor(monkeypatch, state_fn, duration: float = DURATION) -> None:
    clock = FakeClock(PLAY_PRESS)
    bridge = FakeBridge(clock, state_fn)
    monkeypatch.setattr("mvsync.scan_runtime.time", clock)
    _record_until_end(bridge, TRACK_ID, duration, PLAY_PRESS)


def test_playthrough_completes(monkeypatch):
    def state_fn(now):
        elapsed = now - PLAY_PRESS
        if elapsed >= DURATION:
            return make_state(DURATION, now, False)
        return make_state(elapsed, cadence_epoch(now), True)

    run_monitor(monkeypatch, state_fn)


def test_forward_seek_aborts(monkeypatch):
    def state_fn(now):
        elapsed = now - PLAY_PRESS
        position = elapsed if elapsed < 8.0 else elapsed + 30.0
        return make_state(position, cadence_epoch(now), True)

    with pytest.raises(ScanAborted, match="jumped forward"):
        run_monitor(monkeypatch, state_fn, duration=999.0)


def test_backward_seek_aborts(monkeypatch):
    def state_fn(now):
        elapsed = now - PLAY_PRESS
        position = elapsed if elapsed < 8.0 else max(0.0, elapsed - 15.0)
        return make_state(position, cadence_epoch(now), True)

    with pytest.raises(ScanAborted, match="moved backwards"):
        run_monitor(monkeypatch, state_fn)


def test_track_change_aborts(monkeypatch):
    def state_fn(now):
        elapsed = now - PLAY_PRESS
        title = "Title" if elapsed < 5.0 else "Other"
        return make_state(elapsed, cadence_epoch(now), True, title=title)

    with pytest.raises(ScanAborted, match="track changed"):
        run_monitor(monkeypatch, state_fn)


def test_playback_never_starts_aborts(monkeypatch):
    def state_fn(now):
        return make_state(0.0, PLAY_PRESS, False)

    with pytest.raises(ScanAborted, match="did not start"):
        run_monitor(monkeypatch, state_fn)


def test_slow_first_refresh_does_not_abort(monkeypatch):
    """The first timeline refresh after playback starts must be rebased.

    Spotify can keep serving the pre-play (paused) timeline entry for several
    seconds. A naive delta check sees the refreshed position jump and aborts.
    """

    def state_fn(now):
        elapsed = now - PLAY_PRESS
        if elapsed >= DURATION:
            return make_state(DURATION, now, False)
        if now < PLAY_PRESS + 3.0:
            return make_state(0.0, PLAY_PRESS - 2.0, True)
        epoch = PLAY_PRESS + 3.0 + math.floor((elapsed - 3.0) / CADENCE_S) * CADENCE_S
        return make_state(elapsed, epoch, True)

    run_monitor(monkeypatch, state_fn)


def test_frozen_timeline_with_stepped_positions_does_not_abort(monkeypatch):
    """Position steps inside one timeline entry must never be treated as seeks.

    If the reported update time goes stale while the position keeps stepping,
    comparing extrapolated values across polls produces fake jump deltas.
    """

    def state_fn(now):
        elapsed = now - PLAY_PRESS
        if elapsed >= DURATION:
            return make_state(DURATION, now, False)
        step = math.floor(elapsed / CADENCE_S) * CADENCE_S
        return make_state(step, PLAY_PRESS, True)

    run_monitor(monkeypatch, state_fn)


def test_backward_reset_at_track_end_completes(monkeypatch):
    """The next track's timeline (position reset) can arrive before the metadata
    switches; that end-of-track race must finish successfully, not abort.
    """

    def state_fn(now):
        elapsed = now - PLAY_PRESS
        if elapsed >= DURATION:
            if elapsed < DURATION + 1.0:
                return make_state(0.0, now, True)
            return make_state(0.0, now, True, title="Other")
        return make_state(elapsed, cadence_epoch(now), True)

    run_monitor(monkeypatch, state_fn)


def test_brief_pause_then_resume_completes(monkeypatch):
    def state_fn(now):
        elapsed = now - PLAY_PRESS
        if elapsed >= DURATION:
            return make_state(DURATION, now, False)
        if 1.0 <= elapsed < 2.0:
            return make_state(1.0, PLAY_PRESS + 1.0, False)
        if elapsed >= 2.0:
            epoch = PLAY_PRESS + 2.0 + math.floor((elapsed - 2.0) / CADENCE_S) * CADENCE_S
            return make_state(elapsed - 1.0, epoch, True)
        return make_state(elapsed, cadence_epoch(now), True)

    run_monitor(monkeypatch, state_fn)


def _prompt_candidates() -> list[Candidate]:
    return [
        Candidate(video_id=str(index), title=f"t{index}", duration_s=None, channel="")
        for index in range(3)
    ]


def test_prompt_answer_enter_accepts_current_pick():
    candidates = _prompt_candidates()
    assert _apply_prompt_answer(candidates, "   ") is True
    assert [candidate.video_id for candidate in candidates] == ["0", "1", "2"]


def test_prompt_answer_number_prioritizes():
    candidates = _prompt_candidates()
    assert _apply_prompt_answer(candidates, "3") is False
    assert [candidate.video_id for candidate in candidates] == ["2", "0", "1"]


def test_prompt_answer_bad_number_keeps_order():
    candidates = _prompt_candidates()
    assert _apply_prompt_answer(candidates, "9") is False
    assert [candidate.video_id for candidate in candidates] == ["0", "1", "2"]


def test_prompt_answer_url_replaces_candidates():
    candidates = _prompt_candidates()
    assert _apply_prompt_answer(candidates, "https://youtu.be/ulr0muQKjk0") is False
    assert [candidate.video_id for candidate in candidates] == ["ulr0muQKjk0"]


def test_prompt_answer_bare_video_id_replaces_candidates():
    candidates = _prompt_candidates()
    assert _apply_prompt_answer(candidates, "ulr0muQKjk0") is False
    assert [candidate.video_id for candidate in candidates] == ["ulr0muQKjk0"]


def test_prompt_answer_garbage_keeps_order():
    candidates = _prompt_candidates()
    assert _apply_prompt_answer(candidates, "hello world") is False
    assert [candidate.video_id for candidate in candidates] == ["0", "1", "2"]


class StepClock(FakeClock):
    def monotonic(self) -> float:
        return self.now


class ScanBridge:
    def __init__(self, clock: StepClock, toggles: list[float]) -> None:
        self.clock = clock
        self.toggles = toggles

    def current(self) -> SpotifyState:
        if not self.toggles:
            return make_state(0.0, self.clock.now, True)
        if len(self.toggles) == 1:
            return make_state(0.0, self.clock.now, False)
        play_press = self.toggles[1]
        elapsed = self.clock.now - play_press
        if elapsed >= DURATION:
            return make_state(DURATION, self.clock.now, False)
        return make_state(elapsed, cadence_epoch(self.clock.now, play_press), True)

    def toggle_play_pause(self) -> None:
        self.toggles.append(self.clock.now)

    def seek(self, seconds: float) -> None:
        pass


class FakeRecorder:
    instances: list[FakeRecorder] = []

    def __init__(self, path, clock: StepClock) -> None:
        self.path = path
        self.clock = clock
        self.ready_epoch: float | None = None
        self.peak_db = -30.0
        FakeRecorder.instances.append(self)

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"RIFF")
        self.ready_epoch = self.clock.now

    def stop(self) -> None:
        pass


VIDEO_ID = "ulr0muQKjk0"
SCAN_URL = f"https://youtu.be/{VIDEO_ID}"


def run_scan(monkeypatch, tmp_path, *, align, fetch=None):
    import builtins

    from mvsync import scan_runtime, session

    clock = StepClock(PLAY_PRESS)
    toggles: list[float] = []
    bridge = ScanBridge(clock, toggles)
    FakeRecorder.instances = []
    monkeypatch.setattr("mvsync.scan_runtime.time", clock)
    monkeypatch.setattr(scan_runtime, "SpotifyBridge", lambda: bridge)
    monkeypatch.setattr(
        scan_runtime,
        "make_recorder",
        lambda path, *, root=None, mode=None: FakeRecorder(path, clock),
    )
    monkeypatch.setattr(scan_runtime, "wav_duration_s", lambda path: DURATION)
    monkeypatch.setattr(scan_runtime, "align_against_candidates", align)
    if fetch is not None:
        monkeypatch.setattr(scan_runtime, "fetch_video_mp4", fetch)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")

    try:
        return scan_runtime.run_scan(SCAN_URL, root=tmp_path)
    finally:
        session.release(tmp_path)


def recording_path(tmp_path):
    from mvsync import paths

    return paths.recordings_dir(tmp_path) / f"{VIDEO_ID}__{TRACK_ID}.wav"


def test_scan_aborts_on_blank_metadata(tmp_path, monkeypatch, capsys):
    from mvsync import scan_runtime

    class BlankBridge:
        def current(self) -> SpotifyState:
            return make_state(0.0, PLAY_PRESS, False, title="", artist="")

        def toggle_play_pause(self) -> None:
            pass

        def seek(self, seconds: float) -> None:
            pass

    import builtins

    monkeypatch.setattr(scan_runtime, "SpotifyBridge", lambda: BlankBridge())
    monkeypatch.setattr(
        scan_runtime,
        "make_recorder",
        lambda path, *, root=None, mode=None: (_ for _ in ()).throw(
            RuntimeError("recorder should not start")
        ),
    )
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")

    rc = scan_runtime.run_scan(SCAN_URL, root=tmp_path)

    assert rc == 1
    assert "no track metadata" in capsys.readouterr().out


def test_scan_deletes_recording_on_abort(tmp_path, monkeypatch):
    def align(*args, **kwargs):
        raise ScanAborted("no candidate matched")

    rc = run_scan(monkeypatch, tmp_path, align=align)

    assert rc == 1
    assert not recording_path(tmp_path).exists()


def test_scan_deletes_recording_after_saving_its_map(tmp_path, monkeypatch):
    from mvsync import paths
    from mvsync.align_store import alignment_path

    alignment = Alignment(
        video_id=VIDEO_ID,
        track_id=TRACK_ID,
        video_duration_s=40.0,
        track_duration_s=DURATION,
        anchors=[Anchor(video_s=0.0, spotify_s=0.0), Anchor(video_s=20.0, spotify_s=20.0)],
    )
    video_file = tmp_path / "vid.mp4"
    video_file.write_bytes(b"x" * 32)

    rc = run_scan(
        monkeypatch,
        tmp_path,
        align=lambda *args, **kwargs: (alignment, VIDEO_ID),
        fetch=lambda url, root=None: video_file,
    )

    assert rc == 0
    assert not recording_path(tmp_path).exists()
    store = paths.ensure_dir(paths.alignments_dir(tmp_path))
    assert alignment_path(store, VIDEO_ID, TRACK_ID).is_file()


def run_scan_with_candidates(monkeypatch, tmp_path, *, align, fetch=None, candidates=None):
    from mvsync import scan_runtime, session

    clock = StepClock(PLAY_PRESS)
    toggles: list[float] = []
    bridge = ScanBridge(clock, toggles)
    FakeRecorder.instances = []
    monkeypatch.setattr("mvsync.scan_runtime.time", clock)
    monkeypatch.setattr(scan_runtime, "SpotifyBridge", lambda: bridge)
    monkeypatch.setattr(
        scan_runtime,
        "make_recorder",
        lambda path, *, root=None, mode=None: FakeRecorder(path, clock),
    )
    monkeypatch.setattr(scan_runtime, "wav_duration_s", lambda path: DURATION)
    monkeypatch.setattr(scan_runtime, "align_against_candidates", align)
    video_file = tmp_path / "vid.mp4"
    video_file.write_bytes(b"x" * 32)
    monkeypatch.setattr(
        scan_runtime, "fetch_video_mp4", fetch or (lambda url, root=None: video_file)
    )
    if candidates is None:
        candidates = [Candidate(VIDEO_ID, "", None, "")]
    try:
        return scan_runtime.scan_with_candidates(candidates, root=tmp_path)
    finally:
        session.release(tmp_path)


def test_scan_with_candidates_records_and_saves(tmp_path, monkeypatch):
    alignment = Alignment(
        video_id=VIDEO_ID,
        track_id=TRACK_ID,
        video_duration_s=40.0,
        track_duration_s=DURATION,
        anchors=[Anchor(video_s=0.0, spotify_s=0.0), Anchor(video_s=20.0, spotify_s=20.0)],
    )
    result = run_scan_with_candidates(
        monkeypatch, tmp_path, align=lambda *args, **kwargs: (alignment, VIDEO_ID)
    )
    assert result.video_id == VIDEO_ID
    assert result.path.is_file()
    assert not recording_path(tmp_path).exists()


def test_scan_with_candidates_prunes_superseded_map(tmp_path, monkeypatch):
    from mvsync import paths
    from mvsync.align_store import alignment_path, save_alignment

    store = paths.ensure_dir(paths.alignments_dir(tmp_path))
    stale = Alignment(
        video_id="stalev",
        track_id=TRACK_ID,
        video_duration_s=40.0,
        track_duration_s=DURATION,
        anchors=[Anchor(video_s=0.0, spotify_s=0.0), Anchor(video_s=20.0, spotify_s=20.0)],
    )
    save_alignment(store, stale)
    alignment = Alignment(
        video_id=VIDEO_ID,
        track_id=TRACK_ID,
        video_duration_s=40.0,
        track_duration_s=DURATION,
        anchors=[Anchor(video_s=0.0, spotify_s=0.0), Anchor(video_s=20.0, spotify_s=20.0)],
    )
    run_scan_with_candidates(
        monkeypatch, tmp_path, align=lambda *args, **kwargs: (alignment, VIDEO_ID)
    )
    assert not alignment_path(store, "stalev", TRACK_ID).exists()
    assert alignment_path(store, VIDEO_ID, TRACK_ID).is_file()


def test_scan_with_candidates_refuses_while_session_busy(tmp_path, monkeypatch):
    from mvsync import paths, scan_runtime

    lock = paths.data_dir(tmp_path) / "session.lock"
    paths.ensure_dir(lock.parent)
    lock.write_text("12345", encoding="utf-8")
    monkeypatch.setattr("mvsync.session._pid_alive", lambda pid: True)

    with pytest.raises(RuntimeError, match="another session is already running"):
        scan_runtime.scan_with_candidates([Candidate(VIDEO_ID, "", None, "")], root=tmp_path)


def test_scan_with_candidates_discards_recording_on_abort(tmp_path, monkeypatch):
    def align(*args, **kwargs):
        raise ScanAborted("no candidate matched")

    with pytest.raises(ScanAborted, match="no candidate matched"):
        run_scan_with_candidates(monkeypatch, tmp_path, align=align)

    assert not recording_path(tmp_path).exists()
