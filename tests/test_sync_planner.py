import pytest

from mvsync.sync_planner import PlannerConfig, SyncState, plan, target_video_s, with_offset
from mvsync.timeline import Alignment, Anchor, HoldSpan, SkipSpan


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


def hold_alignment() -> Alignment:
    return Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=12.0,
        track_duration_s=18.0,
        anchors=[
            Anchor(0.0, 0.0),
            Anchor(4.0, 4.0),
            Anchor(6.0, 12.0),
            Anchor(12.0, 18.0),
        ],
        holds=[HoldSpan(4.0, 10.0, 4.0, reason="track-cut")],
    )


def test_target_inside_hold_is_the_held_frame():
    alignment = hold_alignment()
    assert target_video_s(alignment, 6.0) == pytest.approx(4.0)
    assert target_video_s(alignment, 10.5) == pytest.approx(4.5)


def test_plan_inside_hold_pauses_and_seeks_to_the_cut():
    action = plan(SyncState(6.0, True, 2.0, False), hold_alignment())
    assert action.pause_video is True
    assert action.seek_video_to_s == pytest.approx(4.0)
    assert action.rate == 1.0


def test_plan_inside_hold_does_not_seek_when_already_held():
    action = plan(SyncState(6.0, True, 4.0, True), hold_alignment())
    assert action.pause_video is True
    assert action.seek_video_to_s is None


def test_plan_resumes_after_hold():
    action = plan(SyncState(10.5, True, 4.0, True), hold_alignment())
    assert action.pause_video is False
    assert action.seek_video_to_s == pytest.approx(4.5)


def test_plan_does_not_jump_to_an_early_trailing_skip_exit():
    alignment = Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=30.0,
        track_duration_s=18.0,
        anchors=[Anchor(0.0, 0.0), Anchor(17.0, 17.93)],
        skips=[SkipSpan(17.0, 30.0, reason="trailing", confidence=1.0)],
    )
    action = plan(SyncState(17.5, True, 17.2, False), alignment)
    assert action.seek_video_to_s != 30.0


def test_with_offset_shifts_holds():
    alignment = hold_alignment()
    materialized = with_offset(alignment, 2.0)
    assert [hold.spotify_start_s for hold in materialized.holds] == pytest.approx([2.0])
    for position in (0.0, 5.0, 8.0, 9.0, 12.0):
        assert materialized.spotify_to_video(position) == pytest.approx(
            alignment.spotify_to_video(position, 2.0)
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


def test_plan_follows_local_segment_rate():
    alignment = Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=110.0,
        track_duration_s=100.0,
        anchors=[Anchor(0.0, 0.0), Anchor(108.0, 100.0)],
    )
    action = plan(SyncState(50.0, True, 54.0, False), alignment)
    assert action.rate == pytest.approx(1.08)


def test_plan_correction_is_relative_to_segment_rate():
    alignment = Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=110.0,
        track_duration_s=100.0,
        anchors=[Anchor(0.0, 0.0), Anchor(108.0, 100.0)],
    )
    action = plan(SyncState(50.0, True, 53.8, False), alignment)
    assert action.rate == pytest.approx(1.09)


def test_plan_absolute_rate_clamp():
    alignment = Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=120.0,
        track_duration_s=100.0,
        anchors=[Anchor(0.0, 0.0), Anchor(119.0, 100.0)],
    )
    action = plan(SyncState(50.0, True, 59.0, False), alignment)
    assert action.rate == pytest.approx(1.20)


def test_plan_noop_keeps_segment_rate():
    alignment = Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=110.0,
        track_duration_s=100.0,
        anchors=[Anchor(0.0, 0.0), Anchor(108.0, 100.0)],
    )
    action = plan(SyncState(50.0, True, 54.01, False), alignment)
    assert action.rate == pytest.approx(1.08)


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
