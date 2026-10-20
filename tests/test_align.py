import pytest

from mvsync.align import anchors_and_skips_from_path, anchors_skips_and_holds_from_path, looks_sane
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
        (5.0, 15.0, "internal"),
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
        (3.0, 8.0, "internal"),
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


def test_skip_ends_at_first_honest_frame():
    path = [
        (0, 0),
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 4),
        (4, 6),
        (4, 8),
        (5, 10),
        (6, 12),
    ]
    _, skips = anchors_and_skips_from_path(
        path, spotify_hop_s=1.0, video_hop_s=1.0, video_duration_s=12.0
    )
    assert [(s.video_start_s, s.video_end_s, s.reason) for s in skips] == [
        (4.0, 10.0, "internal")
    ]


def test_fragmented_talk_keeps_full_skip_span():
    path = [
        (0, 0),
        (2, 2),
        (4, 4),
        (6, 6),
        (8, 8),
        (10, 10),
        (12, 12),
        (12, 13),
        (12, 14),
        (13, 15),
        (13, 16),
        (13, 17),
        (15, 18),
        (17, 20),
        (19, 22),
        (21, 24),
    ]
    costs = [0.0] * 7 + [0.3, 0.3, 0.1, 0.3, 0.3, 0.0, 0.0, 0.0, 0.0]
    anchors, skips = anchors_and_skips_from_path(
        path,
        spotify_hop_s=0.5,
        video_hop_s=0.5,
        video_duration_s=12.0,
        step_costs=costs,
    )
    assert [(s.video_start_s, s.video_end_s, s.reason) for s in skips] == [
        (6.0, 9.0, "internal")
    ]
    assert all(not (6.0 < anchor.video_s < 9.0) for anchor in anchors)
    assert (9.0, 6.0) in [(anchor.video_s, anchor.spotify_s) for anchor in anchors]
    alignment = Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=12.0,
        track_duration_s=9.0,
        anchors=anchors,
        skips=skips,
    )
    assert [segment.rate for segment in alignment.segments] == pytest.approx([1.0, 1.0])


def test_short_low_cost_stall_on_music_is_repaired():
    path = [
        (0, 0),
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 4),
        (4, 5),
        (4, 6),
        (5, 7),
        (6, 8),
        (7, 9),
        (8, 10),
    ]
    costs = [0.0] * len(path)
    anchors, skips = anchors_and_skips_from_path(
        path,
        spotify_hop_s=1.0,
        video_hop_s=1.0,
        video_duration_s=10.0,
        step_costs=costs,
    )
    assert skips == []
    assert [
        (anchor.video_s, anchor.spotify_s)
        for anchor in anchors
        if anchor.video_s in (4.0, 6.0, 8.0)
    ] == [(4.0, 4.0), (6.0, 6.0), (8.0, 8.0)]
    alignment = Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=10.0,
        track_duration_s=10.0,
        anchors=anchors,
        skips=skips,
    )
    assert alignment.segments[0].rate == pytest.approx(1.0)


def test_stall_only_run_is_not_a_gap_when_costs_are_present():
    path = [
        (0, 0),
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 4),
        (4, 5),
        (4, 6),
        (5, 7),
        (6, 8),
        (7, 9),
        (8, 10),
    ]
    costs = [0.0] * len(path)
    _, skips = anchors_and_skips_from_path(
        path,
        spotify_hop_s=1.0,
        video_hop_s=1.0,
        video_duration_s=10.0,
        step_costs=costs,
    )
    assert skips == []

    _, pure_skips = anchors_and_skips_from_path(
        path, spotify_hop_s=1.0, video_hop_s=1.0, video_duration_s=10.0
    )
    assert [(s.video_start_s, s.video_end_s, s.reason) for s in pure_skips] == [
        (4.0, 7.0, "internal")
    ]


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


def test_internal_track_cut_becomes_a_hold():
    path = [
        (0, 0),
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 4),
        (5, 4),
        (6, 4),
        (7, 5),
        (8, 5),
        (9, 5),
        (10, 6),
        (11, 6),
        (12, 6),
        (13, 7),
        (14, 8),
        (15, 9),
        (16, 10),
        (17, 11),
        (18, 12),
    ]
    anchors, skips, holds = anchors_skips_and_holds_from_path(
        path,
        spotify_hop_s=1.0,
        video_hop_s=1.0,
        video_duration_s=12.0,
        track_duration_s=18.0,
    )
    assert [(hold.spotify_start_s, hold.spotify_end_s, hold.video_s) for hold in holds] == [
        (4.0, 10.0, 4.0)
    ]
    assert skips == []
    assert (4.0, 4.0) in [(anchor.video_s, anchor.spotify_s) for anchor in anchors]
    assert (6.0, 12.0) in [(anchor.video_s, anchor.spotify_s) for anchor in anchors]
    alignment = Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=12.0,
        track_duration_s=18.0,
        anchors=anchors,
        skips=skips,
        holds=holds,
    )
    assert alignment.spotify_to_video(4.0) == pytest.approx(4.0)
    assert alignment.spotify_to_video(8.0) == pytest.approx(4.0)
    assert alignment.spotify_to_video(10.0) == pytest.approx(4.0)
    assert alignment.spotify_to_video(11.0) == pytest.approx(5.0)
    assert alignment.spotify_to_video(12.0) == pytest.approx(6.0)
    assert alignment.spotify_to_video(18.0) == pytest.approx(12.0)
    assert [segment.rate for segment in alignment.segments] == pytest.approx([1.0, 0.0, 1.0])
    assert looks_sane(alignment, 18.0)


def test_tail_compression_is_repaired_not_treated_as_a_cut():
    path = [(index, index) for index in range(17)] + [(18, 17)]
    anchors, skips, holds = anchors_skips_and_holds_from_path(
        path,
        spotify_hop_s=1.0,
        video_hop_s=1.0,
        video_duration_s=30.0,
        track_duration_s=18.0,
    )
    assert holds == []
    assert (anchors[-1].video_s, anchors[-1].spotify_s) == pytest.approx((18.0, 18.0))
    assert [(span.video_start_s, span.video_end_s, span.reason) for span in skips] == [
        (18.0, 30.0, "trailing")
    ]
    alignment = Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=30.0,
        track_duration_s=18.0,
        anchors=anchors,
        skips=skips,
    )
    assert alignment.segments[0].rate == pytest.approx(1.0)
    assert alignment.spotify_to_video(17.5) == pytest.approx(17.5)


def test_two_gaps_sharing_an_honest_frame_keep_both_boundaries():
    path = [
        (0, 0),
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 4),
        (4, 5),
        (4, 6),
        (5, 7),
        (5, 8),
        (5, 9),
        (6, 10),
        (7, 11),
    ]
    anchors, _ = anchors_and_skips_from_path(
        path,
        spotify_hop_s=1.0,
        video_hop_s=1.0,
        video_duration_s=12.0,
    )
    assert (10.0, 4.0) in [(anchor.video_s, anchor.spotify_s) for anchor in anchors]
