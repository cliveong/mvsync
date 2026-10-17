import pytest

from mvsync.align import anchors_and_skips_from_path
from mvsync.timeline import Alignment


def test_empty_path_rejected():
    with pytest.raises(ValueError):
        anchors_and_skips_from_path(
            [], spotify_hop_s=1.0, video_hop_s=1.0, video_duration_s=10.0
        )


def test_straight_diagonal_has_no_skips():
    path = [(i, i) for i in range(11)]
    anchors, skips = anchors_and_skips_from_path(
        path, spotify_hop_s=1.0, video_hop_s=1.0, video_duration_s=10.0
    )
    assert skips == []
    assert [(a.video_s, a.spotify_s) for a in anchors] == [
        (0.0, 0.0),
        (2.0, 2.0),
        (4.0, 4.0),
        (6.0, 6.0),
        (8.0, 8.0),
        (10.0, 10.0),
    ]


def test_internal_and_trailing_skips_detected():
    path = (
        [(i, i) for i in range(6)]
        + [(5, video) for video in range(6, 15)]
        + [(6, 15), (7, 16), (8, 17)]
    )
    anchors, skips = anchors_and_skips_from_path(
        path, spotify_hop_s=1.0, video_hop_s=1.0, video_duration_s=20.0
    )
    assert [(s.video_start_s, s.video_end_s, s.reason) for s in skips] == [
        (5.0, 14.0, "internal"),
        (17.0, 20.0, "trailing"),
    ]
    assert [(a.video_s, a.spotify_s) for a in anchors] == [
        (0.0, 0.0),
        (2.0, 2.0),
        (4.0, 4.0),
        (15.0, 5.0),
        (17.0, 7.0),
    ]


def test_leading_skip_detected():
    path = [(i, i + 3) for i in range(8)]
    _, skips = anchors_and_skips_from_path(
        path, spotify_hop_s=1.0, video_hop_s=1.0, video_duration_s=10.0
    )
    assert [(s.video_start_s, s.video_end_s, s.reason) for s in skips] == [
        (0.0, 3.0, "leading")
    ]


def test_short_stall_is_not_a_skip():
    path = [(0, 0), (2, 1), (2, 2), (4, 3)]
    _, skips = anchors_and_skips_from_path(
        path, spotify_hop_s=1.0, video_hop_s=0.5, video_duration_s=1.5
    )
    assert skips == []


def test_boundary_duplicate_spotify_prefers_later_video():
    path = [(0, 0), (2, 2), (4, 4), (4, 6)]
    anchors, skips = anchors_and_skips_from_path(
        path, spotify_hop_s=1.0, video_hop_s=1.0, video_duration_s=6.0
    )
    assert [(s.video_start_s, s.video_end_s) for s in skips] == [(4.0, 6.0)]
    assert [(a.video_s, a.spotify_s) for a in anchors] == [
        (0.0, 0.0),
        (2.0, 2.0),
        (4.0, 4.0),
    ]


def test_match_costs_pin_gap_boundaries_and_reanchor():
    path = [
        (0, 0),
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 4),
        (5, 5),
        (5, 6),
        (5, 7),
        (6, 8),
        (7, 9),
        (8, 10),
    ]
    costs = [0.0, 0.0, 0.0, 0.0, 0.5, 0.5, 0.4, 0.4, 0.0, 0.0, 0.0]
    anchors, skips = anchors_and_skips_from_path(
        path,
        spotify_hop_s=1.0,
        video_hop_s=1.0,
        video_duration_s=12.0,
        step_costs=costs,
    )
    assert [(s.video_start_s, s.video_end_s, s.reason) for s in skips] == [
        (3.0, 7.0, "internal"),
        (10.0, 12.0, "trailing"),
    ]
    alignment = Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=12.0,
        track_duration_s=8.0,
        anchors=anchors,
        skips=skips,
    )
    assert alignment.spotify_to_video(2.5) == pytest.approx(2.5)
    assert alignment.spotify_to_video(3.0) == pytest.approx(8.0)
    assert alignment.video_to_spotify(9.0) == pytest.approx(4.0)


def test_flagged_run_without_stall_is_not_a_gap():
    path = [(index, index) for index in range(6)]
    costs = [0.0, 0.0, 0.0, 0.5, 0.5, 0.5]
    anchors, skips = anchors_and_skips_from_path(
        path,
        spotify_hop_s=1.0,
        video_hop_s=1.0,
        video_duration_s=5.0,
        step_costs=costs,
    )
    assert [(s.video_start_s, s.video_end_s, s.reason) for s in skips] == [
        (2.0, 5.0, "trailing")
    ]
    assert [(a.video_s, a.spotify_s) for a in anchors] == [(0.0, 0.0), (2.0, 2.0)]
