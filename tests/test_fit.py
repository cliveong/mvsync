import time
import wave
from pathlib import Path

import pytest

from mvsync.align import looks_sane
from mvsync.match import align_against_candidates
from mvsync.mv_search import Candidate
from mvsync.radio import RadioWatch, _capture_lost_audio
from mvsync.spotify_smtc import SpotifyState
from mvsync.sync_runtime import _rough_jump_delta
from mvsync.timeline import Alignment, Anchor, SkipSpan


def make_alignment(
    count: int, *, rate: float = 1.0, span_s: float = 160.0, track_duration_s: float = 200.0
) -> Alignment:
    step = span_s / max(1, count - 1)
    anchors = [
        Anchor(video_s=index * step * rate, spotify_s=index * step) for index in range(count)
    ]
    return Alignment(
        video_id="video",
        track_id="track",
        video_duration_s=span_s * rate + 20.0,
        track_duration_s=track_duration_s,
        anchors=anchors,
        skips=[],
    )


def test_looks_sane_accepts_dense_uniform_alignment():
    assert looks_sane(make_alignment(20), 200.0)


def test_looks_sane_rejects_too_few_anchors():
    assert not looks_sane(make_alignment(3), 200.0)


def test_looks_sane_rejects_low_coverage():
    assert not looks_sane(make_alignment(10, span_s=20.0), 200.0)


def test_looks_sane_rejects_wild_rates():
    assert not looks_sane(make_alignment(10, rate=1.5), 200.0)


def test_looks_sane_accepts_an_edited_mv_with_a_sane_majority():
    anchors = [
        Anchor(video_s=6.0, spotify_s=0.0),
        Anchor(video_s=58.0, spotify_s=52.0),
        Anchor(video_s=110.0, spotify_s=104.0),
        Anchor(video_s=162.0, spotify_s=156.0),
        Anchor(video_s=170.0, spotify_s=160.0),
        Anchor(video_s=190.0, spotify_s=184.5),
        Anchor(video_s=210.0, spotify_s=209.0),
    ]
    alignment = Alignment(
        video_id="video",
        track_id="track",
        video_duration_s=220.0,
        track_duration_s=209.0,
        anchors=anchors,
        skips=[SkipSpan(video_start_s=162.5, video_end_s=169.5, reason="internal")],
    )
    assert looks_sane(alignment, 209.0)


def test_looks_sane_rejects_an_edited_mv_when_most_rates_are_off():
    anchors = [
        Anchor(video_s=5.0, spotify_s=0.0),
        Anchor(video_s=15.0, spotify_s=10.0),
        Anchor(video_s=25.0, spotify_s=20.0),
        Anchor(video_s=45.0, spotify_s=40.0),
        Anchor(video_s=101.0, spotify_s=140.0),
        Anchor(video_s=165.0, spotify_s=240.0),
    ]
    alignment = Alignment(
        video_id="video",
        track_id="track",
        video_duration_s=200.0,
        track_duration_s=240.0,
        anchors=anchors,
        skips=[SkipSpan(video_start_s=27.0, video_end_s=42.0, reason="internal")],
    )
    assert not looks_sane(alignment, 240.0)


def _write_silent_wav(path: Path, seconds: float = 0.5) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\x00\x00" * int(22050 * seconds))
    return path


def test_align_against_candidates_retries_until_sane(tmp_path):
    recording = _write_silent_wav(tmp_path / "rec.wav")
    sane = make_alignment(20)
    bad = make_alignment(2)
    seen: list[str] = []

    def fake_fetch(url: str) -> Path:
        return _write_silent_wav(tmp_path / f"{url.rsplit('=', 1)[1]}.wav")

    def fake_compute(rec_path, video_wav, **kwargs):
        seen.append(kwargs["video_id"])
        return bad if kwargs["video_id"] == "bad" else sane

    candidates = [Candidate("bad", "wrong", 100.0, ""), Candidate("ok", "right", 100.0, "")]
    result = align_against_candidates(
        recording,
        candidates,
        track_id="track",
        track_duration_s=200.0,
        spotify_offset_s=0.0,
        root=tmp_path,
        fetch_wav=fake_fetch,
        compute=fake_compute,
    )
    assert result is not None
    alignment, video_id = result
    assert video_id == "ok"
    assert alignment.anchors == sane.anchors
    assert seen == ["bad", "ok"]


def test_align_against_candidates_gives_up(tmp_path):
    recording = _write_silent_wav(tmp_path / "rec.wav")

    def fake_compute(rec_path, video_wav, **kwargs):
        return make_alignment(2)

    result = align_against_candidates(
        recording,
        [Candidate("one", "", None, ""), Candidate("two", "", None, "")],
        track_id="track",
        track_duration_s=200.0,
        spotify_offset_s=0.0,
        root=tmp_path,
        fetch_wav=lambda url: _write_silent_wav(tmp_path / f"{url.rsplit('=', 1)[1]}.wav"),
        compute=fake_compute,
    )
    assert result is None


def make_state(position_s: float, updated_epoch_s: float, playing: bool) -> SpotifyState:
    return SpotifyState(
        title="title",
        artist="artist",
        duration_s=200.0,
        position_s=position_s,
        updated_epoch_s=updated_epoch_s,
        playing=playing,
    )


def test_watch_clean_progress_stays_clean():
    watch = RadioWatch(200.0)
    for index in range(10):
        epoch = 100.0 + index * 4.5
        watch.observe(4.5 * index, epoch, True, epoch)
    assert not watch.dirty


def test_watch_backward_seek_is_dirty():
    watch = RadioWatch(200.0)
    watch.observe(40.0, 100.0, True, 100.0)
    watch.observe(5.0, 104.5, True, 104.5)
    assert watch.dirty


def test_watch_forward_jump_is_dirty():
    watch = RadioWatch(200.0)
    watch.observe(40.0, 100.0, True, 100.0)
    watch.observe(120.0, 104.5, True, 104.5)
    assert watch.dirty


def test_watch_short_pause_is_ignored():
    watch = RadioWatch(200.0)
    watch.observe(40.0, 100.0, True, 100.0)
    watch.observe(40.0, 100.0, False, 101.0)
    watch.observe(40.1, 100.5, True, 102.0)
    assert not watch.dirty


def test_watch_long_pause_is_dirty():
    watch = RadioWatch(200.0)
    watch.observe(40.0, 100.0, True, 100.0)
    watch.observe(40.0, 100.0, False, 101.0)
    watch.observe(40.0, 100.0, False, 104.0)
    assert watch.dirty


def test_watch_tolerates_end_of_track_reset():
    watch = RadioWatch(200.0)
    watch.observe(198.5, 100.0, True, 100.0)
    watch.observe(0.5, 104.5, True, 104.5)
    assert not watch.dirty


def test_watch_covered_requires_near_full_span():
    watch = RadioWatch(200.0)
    assert watch.covered(195.0, 2.0)
    assert not watch.covered(150.0, 2.0)


def test_watch_observes_state_dataclass():
    watch = RadioWatch(200.0)
    watch.observe(0.0, 100.0, True, 100.0)
    watch.observe(4.0, 104.5, True, 104.5)
    assert not watch.dirty


def test_load_stored_returns_alignment_and_offset(tmp_path):
    from mvsync.align_store import load_newest_alignment, save_alignment

    alignment = make_alignment(10, span_s=90.0, track_duration_s=100.0)
    save_alignment(tmp_path, alignment, offset_s=0.25)
    result = load_newest_alignment(tmp_path, alignment.track_id)
    assert result is not None
    loaded, offset_s, video_id = result
    assert video_id == alignment.video_id
    assert offset_s == pytest.approx(0.25)
    assert loaded.anchors == alignment.anchors


def test_load_stored_missing_returns_none(tmp_path):
    from mvsync.align_store import load_newest_alignment

    assert load_newest_alignment(tmp_path, "nope") is None


def test_load_stored_skips_corrupt_alignment(tmp_path):
    import os
    import time

    from mvsync.align_store import load_newest_alignment, save_alignment

    good = make_alignment(10, span_s=90.0, track_duration_s=100.0)
    save_alignment(tmp_path, good, offset_s=0.0)
    corrupt = tmp_path / f"othervid__{good.track_id}.json"
    corrupt.write_text("{not valid json", encoding="utf-8")
    stamp = time.time() + 10
    os.utime(corrupt, (stamp, stamp))
    result = load_newest_alignment(tmp_path, good.track_id)
    assert result is not None
    _, _, video_id = result
    assert video_id == good.video_id


def test_load_stored_roundtrips_video_id_with_double_underscore(tmp_path):
    from mvsync.align_store import load_newest_alignment, save_alignment

    base = make_alignment(10, span_s=90.0, track_duration_s=100.0)
    awkward = Alignment(
        video_id="ab__cd",
        track_id=base.track_id,
        video_duration_s=base.video_duration_s,
        track_duration_s=base.track_duration_s,
        anchors=base.anchors,
        skips=base.skips,
    )
    save_alignment(tmp_path, awkward, offset_s=0.0)
    result = load_newest_alignment(tmp_path, awkward.track_id)
    assert result is not None
    _, _, video_id = result
    assert video_id == "ab__cd"


def test_capture_lost_audio_flags_gaps():
    assert not _capture_lost_audio(195.0, 195.6)
    assert not _capture_lost_audio(195.0, 193.5)
    assert _capture_lost_audio(195.0, 175.0)


def test_rough_jump_delta_none_for_normal_progress():
    assert _rough_jump_delta(10.0, 100.0, 12.0, 102.0, 200.0) is None


def test_rough_jump_delta_flags_backward_seek():
    assert _rough_jump_delta(30.0, 100.0, 5.0, 100.5, 200.0) == pytest.approx(-25.0)


def test_rough_jump_delta_flags_forward_seek():
    assert _rough_jump_delta(10.0, 100.0, 30.5, 100.5, 200.0) == pytest.approx(20.5)


def test_rough_jump_delta_ignores_end_of_track_reset():
    assert _rough_jump_delta(199.5, 100.0, 0.5, 104.5, 200.0) is None


def _write_lock(tmp_path, pid: str):
    lock = tmp_path / "data" / "session.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(pid, encoding="utf-8")
    return lock


def test_pid_alive_tracks_real_processes():
    import os

    from mvsync.session import _pid_alive

    assert _pid_alive(os.getpid())
    assert not _pid_alive(0)


def test_session_claim_stale_lock_is_stolen(tmp_path, monkeypatch):
    import os

    from mvsync import session

    monkeypatch.setattr(session, "_pid_alive", lambda pid: False)
    lock = _write_lock(tmp_path, "999999")
    session.claim(tmp_path)
    assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
    session.release(tmp_path)
    assert not lock.exists()


def test_session_claim_refuses_live_lock(tmp_path, monkeypatch):
    from mvsync import session

    monkeypatch.setattr(session, "_pid_alive", lambda pid: True)
    _write_lock(tmp_path, "424242")
    with pytest.raises(RuntimeError, match="another session"):
        session.claim(tmp_path)


def test_session_claim_allows_own_pid(tmp_path, monkeypatch):
    import os

    from mvsync import session

    monkeypatch.setattr(session, "_pid_alive", lambda pid: True)
    _write_lock(tmp_path, str(os.getpid()))
    session.claim(tmp_path)
    session.release(tmp_path)


def test_is_locked_is_false_without_a_lock_file(tmp_path):
    from mvsync import session

    assert session.is_locked(tmp_path) is False


def test_is_locked_is_true_for_another_live_pid(tmp_path, monkeypatch):
    from mvsync import session

    _write_lock(tmp_path, "424242")
    monkeypatch.setattr(session, "_pid_alive", lambda pid: True)
    assert session.is_locked(tmp_path) is True


def test_is_locked_ignores_our_own_pid(tmp_path, monkeypatch):
    import os

    from mvsync import session

    _write_lock(tmp_path, str(os.getpid()))
    monkeypatch.setattr(session, "_pid_alive", lambda pid: True)
    assert session.is_locked(tmp_path) is False


def test_is_locked_ignores_a_dead_pid(tmp_path, monkeypatch):
    from mvsync import session

    _write_lock(tmp_path, "424242")
    monkeypatch.setattr(session, "_pid_alive", lambda pid: False)
    assert session.is_locked(tmp_path) is False


def test_is_locked_only_reads_and_never_creates_the_lock_file(tmp_path):
    from mvsync import session

    assert session.is_locked(tmp_path) is False
    assert not (tmp_path / "data").exists()


def test_lock_holder_reports_the_live_foreign_pid(tmp_path, monkeypatch):
    from mvsync import session

    _write_lock(tmp_path, "424242")
    monkeypatch.setattr(session, "_pid_alive", lambda pid: True)
    assert session.lock_holder(tmp_path) == 424242


def test_lock_refusal_message_names_the_pid_and_the_lock_file():
    from mvsync.session import lock_refusal_message

    message = lock_refusal_message(424242)
    assert "another session is already running (pid 424242)" in message
    assert "session.lock" in message


def test_radio_recording_ignores_observations_before_start(tmp_path):
    from mvsync.mv_search import Candidate
    from mvsync.radio import RadioRecording

    radio = RadioRecording(
        rec_path=tmp_path / "x.wav",
        candidates=[Candidate(video_id="v", title="", duration_s=None, channel="")],
        track_id="t",
        artist="a",
        title="s",
        track_duration_s=100.0,
        root=tmp_path,
    )
    assert not radio.started
    state = SpotifyState(
        title="s",
        artist="a",
        duration_s=100.0,
        position_s=0.0,
        updated_epoch_s=1.0,
        playing=False,
    )
    radio.observe(state, now=1000.0)
    radio.observe(state, now=1010.0)
    radio.finish()
    assert not (tmp_path / "x.wav").exists()


class SilentRecorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.peak_db = -90.0
        self.ready_epoch = time.time()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class StopFailingRecorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.peak_db = -20.0
        self.ready_epoch = time.time()
        self.stop_calls = 0

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"RIFF")

    def stop(self) -> None:
        self.stop_calls += 1
        raise RuntimeError("capture thread failed")


def test_first_listen_discards_when_the_recorder_cannot_start(tmp_path, monkeypatch):
    from mvsync import paths
    from mvsync import radio as radio_module

    rec_path = paths.ensure_dir(tmp_path / "rec") / "a.wav"
    recording = radio_module.RadioRecording(
        rec_path=rec_path,
        candidates=[],
        track_id="t",
        artist="a",
        title="t",
        track_duration_s=100.0,
        root=tmp_path,
    )
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("no spotify process")

    monkeypatch.setattr(radio_module, "make_recorder", fail)
    recording.start(position_s=0.0, wall=time.time())
    assert recording.started is False
    assert len(calls) == 1
    recording.finish()
    assert not rec_path.exists()


def test_first_listen_logs_a_capture_start_failure_once(tmp_path, monkeypatch, capsys):
    from mvsync import paths
    from mvsync import radio as radio_module

    rec_path = paths.ensure_dir(tmp_path / "rec") / "a.wav"
    recording = radio_module.RadioRecording(
        rec_path=rec_path,
        candidates=[],
        track_id="t",
        artist="a",
        title="t",
        track_duration_s=100.0,
        root=tmp_path,
    )
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("no spotify process")

    monkeypatch.setattr(radio_module, "make_recorder", fail)
    recording.start(position_s=0.0, wall=time.time())
    recording.start(position_s=0.0, wall=time.time())
    recording.start(position_s=0.0, wall=time.time())
    out = capsys.readouterr().out
    assert recording.started is False
    assert len(calls) == 1
    assert out.count("cannot record this listen") == 1
    assert "no spotify process" in out
    recording.finish()
    assert not rec_path.exists()


def test_first_listen_discards_a_silent_recording(tmp_path, monkeypatch):
    from mvsync import paths
    from mvsync import radio as radio_module

    rec_path = paths.ensure_dir(tmp_path / "rec") / "a.wav"
    recording = radio_module.RadioRecording(
        rec_path=rec_path,
        candidates=[],
        track_id="t",
        artist="a",
        title="t",
        track_duration_s=100.0,
        root=tmp_path,
    )
    calls = []

    def make(*args, **kwargs):
        calls.append((args, kwargs))
        return SilentRecorder(rec_path)

    monkeypatch.setattr(radio_module, "make_recorder", make)
    recording.start(position_s=0.0, wall=time.time())
    assert recording.started is True
    assert len(calls) == 1
    recording.finish()
    assert not rec_path.exists()


def test_failed_capture_stop_does_not_escape_observe(tmp_path, monkeypatch, capsys):
    from mvsync import paths
    from mvsync import radio as radio_module

    rec_path = paths.ensure_dir(tmp_path / "rec") / "a.wav"
    recorder = StopFailingRecorder(rec_path)
    recording = radio_module.RadioRecording(
        rec_path=rec_path,
        candidates=[],
        track_id="t",
        artist="a",
        title="t",
        track_duration_s=100.0,
        root=tmp_path,
    )
    monkeypatch.setattr(radio_module, "make_recorder", lambda *a, **k: recorder)
    recording.start(position_s=0.0, wall=time.time())
    recording.observe(make_state(40.0, 100.0, True), 100.0)
    recording.observe(make_state(5.0, 104.5, True), 104.5)
    out = capsys.readouterr().out
    assert recorder.stop_calls == 1
    assert "capture thread failed" in out
    recording.finish()
    assert recorder.stop_calls == 1
    assert not radio_module._pending_fits
    assert not rec_path.exists()


def test_failed_capture_stop_does_not_escape_finish(tmp_path, monkeypatch, capsys):
    from mvsync import paths
    from mvsync import radio as radio_module

    rec_path = paths.ensure_dir(tmp_path / "rec") / "a.wav"
    recorder = StopFailingRecorder(rec_path)
    recording = radio_module.RadioRecording(
        rec_path=rec_path,
        candidates=[],
        track_id="t",
        artist="a",
        title="t",
        track_duration_s=100.0,
        root=tmp_path,
    )
    monkeypatch.setattr(radio_module, "make_recorder", lambda *a, **k: recorder)
    recording.start(position_s=0.0, wall=time.time())
    recording.finish()
    out = capsys.readouterr().out
    assert recorder.stop_calls == 1
    assert "capture thread failed" in out
    assert not radio_module._pending_fits
    assert not rec_path.exists()


def test_wait_for_fits_joins_pending_threads():
    import threading

    from mvsync import radio

    thread = threading.Thread(target=lambda: None)
    thread.start()
    radio._pending_fits.append(thread)
    radio.wait_for_fits(timeout_s=5.0)
    assert not radio._pending_fits


def test_fit_in_background_deletes_recording_on_success(tmp_path, monkeypatch):
    from mvsync import paths, radio
    from mvsync.align_store import alignment_path

    recording = _write_silent_wav(tmp_path / "rec.wav")
    alignment = make_alignment(20)
    monkeypatch.setattr(
        radio, "align_against_candidates", lambda *args, **kwargs: (alignment, "video")
    )

    radio._fit_in_background(
        recording,
        [Candidate("video", "", None, "")],
        "track",
        "artist - title",
        200.0,
        0.0,
        tmp_path,
    )

    assert not recording.exists()
    store = paths.ensure_dir(paths.alignments_dir(tmp_path))
    assert alignment_path(store, "video", "track").is_file()


def test_fit_in_background_keeps_recording_when_nothing_matches(tmp_path, monkeypatch):
    from mvsync import radio

    recording = _write_silent_wav(tmp_path / "rec.wav")
    monkeypatch.setattr(radio, "align_against_candidates", lambda *args, **kwargs: None)

    radio._fit_in_background(
        recording,
        [Candidate("video", "", None, "")],
        "track",
        "artist - title",
        200.0,
        0.0,
        tmp_path,
    )

    assert recording.exists()


def test_fit_in_background_keeps_recording_when_the_fit_fails(tmp_path, monkeypatch):
    from mvsync import radio

    recording = _write_silent_wav(tmp_path / "rec.wav")

    def boom(*args, **kwargs):
        raise RuntimeError("decoder blew up")

    monkeypatch.setattr(radio, "align_against_candidates", boom)

    radio._fit_in_background(
        recording,
        [Candidate("video", "", None, "")],
        "track",
        "artist - title",
        200.0,
        0.0,
        tmp_path,
    )

    assert recording.exists()
