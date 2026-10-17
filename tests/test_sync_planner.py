import pytest

from mvsync.sync_planner import PlannerConfig, SyncState, plan, with_offset
from mvsync.timeline import Alignment, Anchor, SkipSpan


def plain_alignment() -> Alignment:
    return Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=100.0,
        track_duration_s=100.0,
        anchors=[Anchor(0.0, 0.0), Anchor(100.0, 100.0)],
    )


def skip_alignment() -> Alignment:
    return Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=100.0,
        track_duration_s=100.0,
        anchors=[Anchor(0.0, 0.0), Anchor(100.0, 100.0)],
        skips=[SkipSpan(30.0, 40.0, reason="internal", confidence=1.0)],
    )


def test_paused_spotify_pauses_video():
    action = plan(SyncState(10.0, False, 10.0, False), plain_alignment())
    assert action.pause_video is True


def test_target_past_end_guard_pauses():
    action = plan(SyncState(99.8, True, 99.8, False), plain_alignment())
    assert action.pause_video is True
    assert action.rate == 1.0


def test_video_inside_skip_seeks_to_exit():
    action = plan(SyncState(35.0, True, 35.0, False), skip_alignment())
    assert action.seek_video_to_s == 40.0
    assert action.pause_video is False


def test_target_inside_trailing_skip_snaps_and_pauses():
    alignment = Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=10.0,
        track_duration_s=8.0,
        anchors=[Anchor(0.0, 0.0), Anchor(4.0, 4.0)],
        skips=[SkipSpan(5.0, 10.0, reason="trailing", confidence=1.0)],
    )
    action = plan(SyncState(6.0, True, 10.0, False), alignment)
    assert action.pause_video is True
    assert action.rate == 1.0
    assert action.seek_video_to_s is None


def test_paused_video_resumes_with_seek():
    action = plan(SyncState(50.0, True, 49.0, True), plain_alignment())
    assert action.pause_video is False
    assert action.seek_video_to_s == 50.0


def test_small_error_nudges_rate():
    action = plan(SyncState(50.0, True, 49.8, False), plain_alignment())
    assert action.seek_video_to_s is None
    assert action.rate == pytest.approx(1.01)


def test_tiny_error_is_noop():
    action = plan(SyncState(50.0, True, 49.99, False), plain_alignment())
    assert action.rate == 1.0
    assert action.seek_video_to_s is None


def test_large_error_seeks():
    action = plan(SyncState(50.0, True, 48.0, False), plain_alignment())
    assert action.seek_video_to_s == 50.0
    assert action.rate == 1.0


def test_rate_nudge_is_capped():
    config = PlannerConfig(rate_gain=1.0, max_rate_delta=0.05, seek_threshold_s=10.0)
    action = plan(SyncState(50.0, True, 49.0, False), plain_alignment(), config=config)
    assert action.rate == pytest.approx(1.05)


def test_offset_shifts_target():
    action = plan(SyncState(50.0, True, 50.0, False), plain_alignment(), offset_s=2.0)
    assert action.seek_video_to_s == 52.0


def test_with_offset_matches_plan_offset():
    alignment = plain_alignment()
    materialized = with_offset(alignment, 2.0)
    for position in (0.0, 10.0, 50.0, 90.0):
        assert materialized.spotify_to_video(position) == pytest.approx(
            alignment.spotify_to_video(position, 2.0)
        )
