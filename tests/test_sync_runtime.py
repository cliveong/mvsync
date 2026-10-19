"""Tests for the play loop without real mpv, SMTC, audio devices, or network."""

from __future__ import annotations

import time

from mvsync import paths
from mvsync.align_store import make_track_id, save_alignment
from mvsync.mv_search import Candidate
from mvsync.player_smtc import PlayerState
from mvsync.timeline import Alignment, Anchor


class FakeMpv:
    def __init__(
        self,
        *,
        paused: bool = False,
        path: str = "",
        duration=None,
        time_pos=0.0,
        eof_reached: bool = False,
        fullscreen: bool = False,
    ):
        self.paused = paused
        self.path = path
        self.duration = duration
        self.time_pos = time_pos
        self.eof_reached = eof_reached
        self.fullscreen = fullscreen
        self.calls: list[tuple] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def load(self, path) -> None:
        self.calls.append(("load", str(path)))
        self.fullscreen = False

    def set_paused(self, value: bool) -> None:
        self.calls.append(("pause", value))
        self.paused = value

    def set_fullscreen(self, value: bool) -> None:
        self.calls.append(("fullscreen", value))
        self.fullscreen = value

    def seek_absolute(self, seconds: float) -> None:
        self.calls.append(("seek", seconds))

    def set_rate(self, rate: float) -> None:
        self.calls.append(("rate", rate))


class FakeBridge:
    def __init__(self, states: list[PlayerState | None]):
        self._states = list(states)

    def current(self):
        return self._states.pop(0) if self._states else None

    def toggle_play_pause(self) -> bool:
        return True


class StubRadio:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.candidates = list(kwargs.get("candidates") or [])
        self.started = False
        self.finished = False

    def start(self, *, position_s: float, wall: float) -> None:
        self.started = True

    def observe(self, state, now: float) -> None:
        pass

    def set_candidates(self, candidates) -> None:
        self.candidates = list(candidates)

    def finish(self) -> None:
        self.finished = True


def make_state(
    *,
    artist: str = "Artist",
    title: str = "Song",
    duration_s: float = 180.0,
    position_s: float = 30.0,
    playing: bool = True,
    updated_epoch_s: float | None = None,
) -> PlayerState:
    return PlayerState(
        artist=artist,
        title=title,
        duration_s=duration_s,
        position_s=position_s,
        updated_epoch_s=updated_epoch_s if updated_epoch_s is not None else time.time(),
        playing=playing,
    )


def make_alignment(video_id: str, track_id: str) -> Alignment:
    return Alignment(
        video_id=video_id,
        track_id=track_id,
        video_duration_s=200.0,
        track_duration_s=180.0,
        anchors=[Anchor(video_s=5.0, spotify_s=0.0), Anchor(video_s=185.0, spotify_s=180.0)],
    )


def save_stored(tmp_path, video_id: str, track_id: str) -> None:
    store = paths.ensure_dir(paths.alignments_dir(tmp_path))
    save_alignment(store, make_alignment(video_id, track_id), offset_s=0.0)


def boom_fetch(url, *, root=None):
    raise RuntimeError("download failed")


def frozen_calls(mpv: FakeMpv) -> list[tuple]:
    return [call for call in mpv.calls if call == ("pause", False)]


def test_stored_fetch_failure_freezes_and_keeps_session(tmp_path, monkeypatch):
    import mvsync.sync_runtime as sr

    state = make_state()
    track_id = make_track_id(state.artist, state.title)
    save_stored(tmp_path, "vidx", track_id)
    mpv = FakeMpv()
    monkeypatch.setattr(sr, "MpvController", lambda root: mpv)
    monkeypatch.setattr(sr, "PlayerBridge", lambda: FakeBridge([state, state, state]))
    monkeypatch.setattr(sr, "fetch_video_mp4", boom_fetch)

    rc = sr.run_play(None, root=tmp_path, poll_s=0.01)

    assert rc == 0
    assert mpv.paused is True
    assert frozen_calls(mpv) == []


def test_start_track_stored_fetch_failure_freezes_and_stays_video_less(tmp_path, monkeypatch):
    import mvsync.sync_runtime as sr

    state = make_state()
    track_id = make_track_id(state.artist, state.title)
    save_stored(tmp_path, "vidx", track_id)
    store = paths.ensure_dir(paths.alignments_dir(tmp_path))
    mpv = FakeMpv()
    monkeypatch.setattr(sr, "fetch_video_mp4", boom_fetch)
    monkeypatch.setattr(sr, "RadioRecording", StubRadio)

    alignment, offset_s, radio, frozen = sr._start_track(state, mpv, store, tmp_path)

    assert alignment is None
    assert offset_s == 0.0
    assert radio is None
    assert frozen is True
    assert mpv.paused is True


def test_start_track_fetch_failure_keeps_recording_and_freezes(tmp_path, monkeypatch):
    import mvsync.sync_runtime as sr

    state = make_state()
    store = paths.ensure_dir(paths.alignments_dir(tmp_path))
    mpv = FakeMpv()
    monkeypatch.setattr(sr, "fetch_video_mp4", boom_fetch)
    monkeypatch.setattr(sr, "RadioRecording", StubRadio)
    monkeypatch.setattr(
        sr,
        "pick_mv_candidates",
        lambda *args, **kwargs: [Candidate("vid1", "t", 180.0, "c")],
    )

    alignment, offset_s, radio, frozen = sr._start_track(state, mpv, store, tmp_path)

    assert alignment is None
    assert offset_s == 0.0
    assert isinstance(radio, StubRadio)
    assert radio.started is True
    assert frozen is True
    assert mpv.paused is True


def test_follow_keeps_frozen_without_video(tmp_path):
    import mvsync.sync_runtime as sr

    state = make_state(playing=True)
    mpv = FakeMpv()
    bridge = FakeBridge([state, state])

    rc = sr._follow(
        bridge,
        mpv,
        make_track_id(state.artist, state.title),
        None,
        0.0,
        tmp_path,
        0.01,
        radio=None,
        frozen=True,
    )

    assert rc == 0
    assert frozen_calls(mpv) == []
    assert mpv.paused is True


def test_follow_rough_mode_unpauses_when_playing(tmp_path, monkeypatch):
    import mvsync.sync_runtime as sr

    state = make_state(playing=True)
    mpv = FakeMpv(paused=True)
    bridge = FakeBridge([state, state])
    radio = StubRadio()

    rc = sr._follow(
        bridge,
        mpv,
        make_track_id(state.artist, state.title),
        None,
        0.0,
        tmp_path,
        0.01,
        radio=radio,
        frozen=False,
    )

    assert rc == 0
    assert ("pause", False) in mpv.calls
    assert radio.started is True


def test_follow_rough_mode_holds_the_last_frame_at_video_end(tmp_path):
    import mvsync.sync_runtime as sr

    state = make_state(playing=True)
    mpv = FakeMpv(paused=True, eof_reached=True, duration=180.0, time_pos=180.0)
    bridge = FakeBridge([state, state])

    rc = sr._follow(
        bridge,
        mpv,
        make_track_id(state.artist, state.title),
        None,
        0.0,
        tmp_path,
        0.01,
        radio=None,
        frozen=False,
    )

    assert rc == 0
    assert frozen_calls(mpv) == []
    assert mpv.paused is True


def test_track_wrapped_detects_repeat_one_restart():
    import mvsync.sync_runtime as sr

    assert sr._track_wrapped(179.6, 0.4, 180.0) is True


def test_track_wrapped_ignores_normal_progress_and_mid_track_seeks():
    import mvsync.sync_runtime as sr

    assert sr._track_wrapped(10.0, 12.0, 180.0) is False
    assert sr._track_wrapped(40.0, 5.0, 180.0) is False
    assert sr._track_wrapped(179.6, 0.4, 0.0) is False


def test_follow_finishes_recording_on_repeat_one(tmp_path):
    import mvsync.sync_runtime as sr

    states = [
        make_state(position_s=179.0),
        make_state(position_s=0.5),
        make_state(position_s=1.0),
    ]
    mpv = FakeMpv()
    radio = StubRadio()

    rc = sr._follow(
        FakeBridge(states),
        mpv,
        make_track_id("Artist", "Song"),
        None,
        0.0,
        tmp_path,
        0.01,
        radio=radio,
        frozen=False,
    )

    assert rc == 0
    assert radio.finished is True
    assert any(call[0] == "seek" and call[1] < 2.0 for call in mpv.calls)


def test_start_track_prints_candidates_when_picking(tmp_path, monkeypatch, capsys):
    import mvsync.sync_runtime as sr

    state = make_state()
    store = paths.ensure_dir(paths.alignments_dir(tmp_path))
    mpv = FakeMpv()
    monkeypatch.setattr(sr, "fetch_video_mp4", boom_fetch)
    monkeypatch.setattr(sr, "RadioRecording", StubRadio)
    monkeypatch.setattr(
        sr,
        "pick_mv_candidates",
        lambda *args, **kwargs: [
            Candidate("vid1", "first", 180.0, "c1"),
            Candidate("vid2", "second", 181.0, "c2"),
        ],
    )

    sr._start_track(state, mpv, store, tmp_path, pick=2)

    out = capsys.readouterr().out
    assert "[play] candidate 1: c1: first (vid1)" in out
    assert "[play] candidate 2: c2: second (vid2)" in out


def test_start_track_skips_blank_metadata(tmp_path, monkeypatch):
    import mvsync.sync_runtime as sr

    state = make_state(artist="", title="", duration_s=0.0, position_s=0.0)
    store = paths.ensure_dir(paths.alignments_dir(tmp_path))
    mpv = FakeMpv()
    searched = []

    def fake_pick(*args, **kwargs):
        searched.append(args)
        raise RuntimeError("search should not run")

    monkeypatch.setattr(sr, "pick_mv_candidates", fake_pick)
    monkeypatch.setattr(sr, "RadioRecording", StubRadio)

    alignment, offset_s, radio, frozen = sr._start_track(state, mpv, store, tmp_path)

    assert searched == []
    assert alignment is None and radio is None
    assert frozen is True


def test_run_play_releases_the_session_lock(tmp_path, monkeypatch):
    import mvsync.sync_runtime as sr

    state = make_state()
    monkeypatch.setattr(sr, "MpvController", lambda root: FakeMpv())
    monkeypatch.setattr(sr, "PlayerBridge", lambda: FakeBridge([state]))
    monkeypatch.setattr(
        sr, "pick_mv_candidates", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("x"))
    )

    assert sr.run_play(None, root=tmp_path, poll_s=0.01) == 0
    assert not (paths.data_dir(tmp_path) / "session.lock").exists()


def test_play_reads_hotkeys_from_the_injected_source(tmp_path, monkeypatch):
    from mvsync import sync_runtime as sr

    pushed = ["q"]

    def keys():
        return pushed.pop(0) if pushed else ""

    monkeypatch.setattr(sr, "MpvController", lambda root: FakeMpv())
    # One state is enough: _follow polls keys() at the top of the loop, before it reads
    # the session, so the injected "q" ends the run before the vanished session does.
    monkeypatch.setattr(sr, "PlayerBridge", lambda: FakeBridge([make_state()]))
    # The real search raises instead of returning an empty list, which _start_track needs.
    monkeypatch.setattr(
        sr, "pick_mv_candidates", lambda *a, **k: [Candidate("vid1", "t", 180.0, "c")]
    )
    monkeypatch.setattr(sr, "fetch_video_mp4", boom_fetch)
    monkeypatch.setattr(sr, "RadioRecording", StubRadio)

    assert sr.run_play(None, root=tmp_path, poll_s=0.01, keys=keys) == 0
    assert pushed == []


class FakePrep:
    """A stand-in for TrackPrep whose polls return a fixed sequence."""

    def __init__(self, results):
        self.results = list(results)
        self.cancelled = False

    def poll(self):
        return self.results.pop(0) if self.results else None

    def cancel(self):
        self.cancelled = True


def test_swap_video_keeps_fullscreen(tmp_path, monkeypatch):
    from pathlib import Path

    import mvsync.sync_runtime as sr

    mpv = FakeMpv(fullscreen=True)
    monkeypatch.setattr(sr, "_wait_media_loaded", lambda *args, **kwargs: True)

    sr._swap_video(mpv, Path("v.mkv"))

    assert mpv.fullscreen is True
    assert ("fullscreen", True) in mpv.calls


def test_track_prep_poll_does_not_block_while_the_work_runs():
    import threading
    from pathlib import Path

    import mvsync.sync_runtime as sr

    gate = threading.Event()
    result = sr.PreparedTrack(None, 0.0, Path("x.mkv"), [])
    prep = sr.TrackPrep(lambda cancel: (gate.wait(5.0), result)[1])

    assert prep.poll() is None
    gate.set()
    deadline = time.time() + 5.0
    while prep.poll() is None and time.time() < deadline:
        time.sleep(0.01)
    finished = prep.poll()
    assert finished is not None
    assert finished.video_file == Path("x.mkv")


def test_follow_keeps_following_while_the_next_video_prepares(tmp_path, monkeypatch):
    from pathlib import Path

    import mvsync.sync_runtime as sr

    state_a = make_state(artist="A", title="One", position_s=10.0)
    state_b = make_state(artist="B", title="Two", position_s=20.0)
    bridge = FakeBridge([state_a, state_b, state_b, state_b, state_b])
    mpv = FakeMpv(paused=False)
    prepared = sr.PreparedTrack(
        alignment=None,
        offset_s=0.0,
        video_file=Path("b.mkv"),
        candidates=[Candidate("vidb", "b", 200.0, "c")],
    )
    prep = FakePrep([None, None, prepared])
    monkeypatch.setattr(
        sr,
        "_begin_track",
        lambda state, *args, **kwargs: (
            make_track_id(state.artist, state.title),
            None,
            0.0,
            None,
            prep,
        ),
    )
    swaps = []
    monkeypatch.setattr(
        sr, "_swap_video", lambda mpv, path, seek_to=None: swaps.append((path, seek_to))
    )

    rc = sr._follow(
        bridge,
        mpv,
        make_track_id("A", "One"),
        None,
        0.0,
        tmp_path,
        0.01,
        keys=lambda: "",
    )

    assert rc == 0
    assert mpv.calls.count(("pause", True)) >= 2
    assert len(swaps) == 1
    assert swaps[0][0] == Path("b.mkv")
    assert abs(swaps[0][1] - 20.0) < 0.5


def test_follow_cancels_a_pending_prep_when_the_track_changes_again(tmp_path, monkeypatch):
    from pathlib import Path

    import mvsync.sync_runtime as sr

    state_a = make_state(artist="A", title="One", position_s=10.0)
    state_b = make_state(artist="B", title="Two", position_s=20.0)
    state_c = make_state(artist="C", title="Three", position_s=30.0)
    bridge = FakeBridge([state_a, state_b, state_c, state_c, state_c])
    mpv = FakeMpv(paused=False)
    prep_b = FakePrep(
        [None, sr.PreparedTrack(None, 0.0, Path("b.mkv"), [], error=None)]
    )
    prep_c = FakePrep([sr.PreparedTrack(None, 0.0, Path("c.mkv"), [])])

    def begin(state, *args, **kwargs):
        if state.title == "Two":
            return make_track_id(state.artist, state.title), None, 0.0, None, prep_b
        return make_track_id(state.artist, state.title), None, 0.0, None, prep_c

    monkeypatch.setattr(sr, "_begin_track", begin)
    swaps = []
    monkeypatch.setattr(
        sr, "_swap_video", lambda mpv, path, seek_to=None: swaps.append(path)
    )

    rc = sr._follow(
        bridge,
        mpv,
        make_track_id("A", "One"),
        None,
        0.0,
        tmp_path,
        0.01,
        keys=lambda: "",
    )

    assert rc == 0
    assert prep_b.cancelled is True
    assert swaps == [Path("c.mkv")]


def test_follow_survives_a_failed_prep_and_freezes(tmp_path, monkeypatch, capsys):
    import mvsync.sync_runtime as sr

    state_a = make_state(artist="A", title="One", position_s=10.0)
    state_b = make_state(artist="B", title="Two", position_s=20.0)
    bridge = FakeBridge([state_a, state_b, state_b, state_b])
    mpv = FakeMpv(paused=False)
    radio = StubRadio()
    prep = FakePrep([sr.PreparedTrack(None, 0.0, None, [], error="search failed")])
    monkeypatch.setattr(
        sr,
        "_begin_track",
        lambda state, *args, **kwargs: (
            make_track_id(state.artist, state.title),
            None,
            0.0,
            radio,
            prep,
        ),
    )

    rc = sr._follow(
        bridge,
        mpv,
        make_track_id("A", "One"),
        None,
        0.0,
        tmp_path,
        0.01,
        keys=lambda: "",
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert mpv.paused is True
    assert radio.finished is True
    assert "search failed" in out


def test_apply_prepared_seeks_a_mapped_video_to_the_alignment_target(tmp_path, monkeypatch):
    from pathlib import Path

    import mvsync.sync_runtime as sr
    from mvsync.sync_planner import target_video_s

    alignment = make_alignment("v", "track")
    prepared = sr.PreparedTrack(alignment, 0.0, Path("m.mkv"), [])
    sought = {}
    monkeypatch.setattr(
        sr, "_swap_video", lambda mpv, path, seek_to=None: sought.update(to=seek_to)
    )

    result = sr._apply_prepared(FakeMpv(), prepared, None, 100.0)

    assert result[0] is alignment
    assert result[1] == 0.0
    assert result[2] is False
    assert sought["to"] == target_video_s(alignment, 100.0, 0.0)


def test_apply_prepared_hands_the_candidates_to_the_recording(tmp_path, monkeypatch):
    from pathlib import Path

    import mvsync.sync_runtime as sr

    radio = StubRadio()
    prepared = sr.PreparedTrack(
        None, 0.0, Path("b.mkv"), [Candidate("vidb", "b", 200.0, "c")]
    )
    monkeypatch.setattr(sr, "_swap_video", lambda *args, **kwargs: None)

    _, _, frozen, returned = sr._apply_prepared(FakeMpv(), prepared, radio, 12.0)

    assert frozen is False
    assert returned is radio
    assert [candidate.video_id for candidate in radio.candidates] == ["vidb"]



def test_begin_track_starts_recording_before_the_search(tmp_path, monkeypatch):
    from pathlib import Path

    import mvsync.sync_runtime as sr

    state = make_state(artist="New", title="Track", duration_s=180.0, position_s=2.0)
    store = paths.ensure_dir(paths.alignments_dir(tmp_path))
    mpv = FakeMpv(paused=False)
    started = []

    class RecordingStub(StubRadio):
        def start(self, *, position_s, wall):
            started.append(position_s)
            super().start(position_s=position_s, wall=wall)

    monkeypatch.setattr(sr, "RadioRecording", RecordingStub)
    monkeypatch.setattr(
        sr, "pick_mv_candidates", lambda *a, **k: [Candidate("vid", "t", 180.0, "c")]
    )
    monkeypatch.setattr(sr, "fetch_video_mp4", lambda url, root=None: Path("vid.mkv"))

    track_id, alignment, offset, radio, prep = sr._begin_track(state, mpv, store, tmp_path)

    assert alignment is None and offset == 0.0
    assert isinstance(radio, RecordingStub)
    assert radio.kwargs["rec_path"].name == f"pending__{track_id}.wav"
    assert started
    assert mpv.paused is True

    deadline = time.time() + 5.0
    while prep.poll() is None and time.time() < deadline:
        time.sleep(0.01)
    prepared = prep.poll()
    assert prepared is not None
    assert prepared.video_file == Path("vid.mkv")
    assert [candidate.video_id for candidate in prepared.candidates] == ["vid"]


def test_begin_track_prepares_a_mapped_track_without_a_recording(tmp_path, monkeypatch):
    from pathlib import Path

    import mvsync.sync_runtime as sr

    state = make_state()
    track_id = make_track_id(state.artist, state.title)
    save_stored(tmp_path, "vidx", track_id)
    store = paths.ensure_dir(paths.alignments_dir(tmp_path))
    mpv = FakeMpv(paused=False)
    monkeypatch.setattr(sr, "fetch_video_mp4", lambda url, root=None: Path("vidx.mkv"))

    _, alignment, _, radio, prep = sr._begin_track(state, mpv, store, tmp_path)

    assert alignment is None
    assert radio is None
    deadline = time.time() + 5.0
    while prep.poll() is None and time.time() < deadline:
        time.sleep(0.01)
    prepared = prep.poll()
    assert prepared is not None
    assert prepared.alignment is not None
    assert prepared.video_file == Path("vidx.mkv")
