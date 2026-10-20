import pytest

from mvsync.timeline import Alignment, Anchor, HoldSpan, SkipSpan


def linear_alignment() -> Alignment:
    return Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=40.0,
        track_duration_s=25.0,
        anchors=[Anchor(10.0, 0.0), Anchor(20.0, 10.0), Anchor(30.0, 20.0)],
    )


def skipped_alignment() -> Alignment:
    return Alignment(
        video_id="vid",
        track_id="trk",
        video_duration_s=30.0,
        track_duration_s=20.0,
        anchors=[Anchor(0.0, 0.0), Anchor(20.0, 10.0), Anchor(30.0, 20.0)],
        skips=[SkipSpan(10.0, 20.0, reason="internal", confidence=1.0)],
    )


class TestLinearMapping:
    def test_spotify_to_video(self):
        alignment = linear_alignment()
        assert alignment.spotify_to_video(0.0) == pytest.approx(10.0)
        assert alignment.spotify_to_video(5.0) == pytest.approx(15.0)
        assert alignment.spotify_to_video(15.0) == pytest.approx(25.0)

    def test_video_to_spotify(self):
        alignment = linear_alignment()
        assert alignment.video_to_spotify(10.0) == pytest.approx(0.0)
        assert alignment.video_to_spotify(25.0) == pytest.approx(15.0)

    def test_spotify_to_video_clamps_to_video_span(self):
        alignment = linear_alignment()
        assert alignment.spotify_to_video(-15.0) == 0.0
        assert alignment.spotify_to_video(100.0) == 40.0

    def test_offset_shifts_mapping(self):
        alignment = linear_alignment()
        assert alignment.spotify_to_video(5.0, offset_s=2.0) == pytest.approx(17.0)


class TestSegmentRate:
    def test_segment_rate_ignores_biased_edge_anchors(self):
        alignment = Alignment(
            video_id="vid",
            track_id="trk",
            video_duration_s=21.5,
            track_duration_s=14.0,
            anchors=[
                Anchor(8.0, 0.0),
                Anchor(10.0, 2.0),
                Anchor(12.0, 4.0),
                Anchor(14.0, 6.0),
                Anchor(16.0, 8.0),
                Anchor(18.0, 10.0),
                Anchor(20.6, 14.0),
            ],
        )
        assert alignment.segments[0].rate == pytest.approx(1.0, abs=0.01)
        assert alignment.segments[0].rate != pytest.approx(0.9, abs=0.005)


class TestTempoMapping:
    def test_rate_scales_both_directions(self):
        alignment = Alignment(
            video_id="vid",
            track_id="trk",
            video_duration_s=12.0,
            track_duration_s=10.0,
            anchors=[Anchor(0.0, 0.0), Anchor(12.0, 10.0)],
        )
        assert alignment.segments[0].rate == pytest.approx(1.2)
        assert alignment.spotify_to_video(5.0) == pytest.approx(6.0)
        assert alignment.video_to_spotify(6.0) == pytest.approx(5.0)


class TestSkips:
    def test_segments_split_at_skip(self):
        alignment = skipped_alignment()
        assert len(alignment.segments) == 2
        assert alignment.segments[0].video_end_s == pytest.approx(20.0)

    def test_mapping_jumps_forward_across_skip(self):
        alignment = skipped_alignment()
        assert alignment.spotify_to_video(9.9) == pytest.approx(9.9)
        assert alignment.spotify_to_video(10.0) == pytest.approx(20.0)

    def test_skip_lookup(self):
        alignment = skipped_alignment()
        assert alignment.skip_at(15.0) is not None
        assert alignment.skip_exit(15.0) == pytest.approx(20.0)
        assert alignment.skip_at(25.0) is None
        assert alignment.skip_exit(25.0) is None

    def test_skips_are_sorted(self):
        alignment = Alignment(
            video_id="vid",
            track_id="trk",
            video_duration_s=100.0,
            track_duration_s=100.0,
            anchors=[Anchor(0.0, 0.0), Anchor(100.0, 100.0)],
            skips=[SkipSpan(30.0, 40.0), SkipSpan(10.0, 15.0)],
        )
        assert [span.video_start_s for span in alignment.skips] == [10.0, 30.0]
        assert len(alignment.segments) == 2


class TestHolds:
    def hold_alignment(self) -> Alignment:
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

    def test_hold_freezes_video_then_resumes(self):
        alignment = self.hold_alignment()
        assert alignment.spotify_to_video(4.0) == pytest.approx(4.0)
        assert alignment.spotify_to_video(8.0) == pytest.approx(4.0)
        assert alignment.spotify_to_video(10.0) == pytest.approx(4.0)
        assert alignment.spotify_to_video(11.0) == pytest.approx(5.0)
        assert alignment.spotify_to_video(12.0) == pytest.approx(6.0)
        assert alignment.spotify_to_video(18.0) == pytest.approx(12.0)

    def test_hold_segments_have_zero_rate(self):
        alignment = self.hold_alignment()
        assert [segment.rate for segment in alignment.segments] == pytest.approx([1.0, 0.0, 1.0])

    def test_hold_lookup_is_end_exclusive(self):
        alignment = self.hold_alignment()
        assert alignment.hold_at(6.0) is not None
        assert alignment.hold_at(3.0) is None
        assert alignment.hold_at(10.0) is None

    def test_video_to_spotify_at_hold_returns_exit(self):
        alignment = self.hold_alignment()
        assert alignment.video_to_spotify(4.0) == pytest.approx(10.0)

    def test_holds_are_sorted(self):
        alignment = Alignment(
            video_id="vid",
            track_id="trk",
            video_duration_s=20.0,
            track_duration_s=20.0,
            anchors=[Anchor(0.0, 0.0), Anchor(20.0, 20.0)],
            holds=[HoldSpan(10.0, 12.0, 10.0), HoldSpan(4.0, 6.0, 4.0)],
        )
        assert [hold.spotify_start_s for hold in alignment.holds] == [4.0, 10.0]

    def test_hold_validation(self):
        with pytest.raises(ValueError):
            Alignment(
                video_id="vid",
                track_id="trk",
                video_duration_s=20.0,
                track_duration_s=20.0,
                anchors=[Anchor(0.0, 0.0), Anchor(20.0, 20.0)],
                holds=[HoldSpan(5.0, 5.0, 5.0)],
            )
        with pytest.raises(ValueError):
            Alignment(
                video_id="vid",
                track_id="trk",
                video_duration_s=20.0,
                track_duration_s=20.0,
                anchors=[Anchor(0.0, 0.0), Anchor(20.0, 20.0)],
                holds=[HoldSpan(5.0, 9.0, 5.0), HoldSpan(8.0, 12.0, 8.0)],
            )
        with pytest.raises(ValueError):
            Alignment(
                video_id="vid",
                track_id="trk",
                video_duration_s=20.0,
                track_duration_s=20.0,
                anchors=[Anchor(0.0, 0.0), Anchor(20.0, 20.0)],
                holds=[HoldSpan(5.0, 9.0, 25.0)],
            )
        with pytest.raises(ValueError):
            Alignment(
                video_id="vid",
                track_id="trk",
                video_duration_s=20.0,
                track_duration_s=20.0,
                anchors=[Anchor(0.0, 0.0), Anchor(20.0, 20.0)],
                skips=[SkipSpan(4.0, 8.0, reason="internal")],
                holds=[HoldSpan(10.0, 12.0, 6.0)],
            )


class TestValidation:
    def test_empty_anchors_rejected(self):
        with pytest.raises(ValueError):
            Alignment(
                video_id="vid",
                track_id="trk",
                video_duration_s=10.0,
                track_duration_s=10.0,
                anchors=[],
            )

    def test_non_increasing_anchors_rejected(self):
        with pytest.raises(ValueError):
            Alignment(
                video_id="vid",
                track_id="trk",
                video_duration_s=10.0,
                track_duration_s=10.0,
                anchors=[Anchor(0.0, 0.0), Anchor(0.0, 5.0)],
            )

    def test_non_positive_durations_rejected(self):
        with pytest.raises(ValueError):
            Alignment(
                video_id="vid",
                track_id="trk",
                video_duration_s=0.0,
                track_duration_s=10.0,
                anchors=[Anchor(0.0, 0.0)],
            )

    def test_zero_length_skip_rejected(self):
        with pytest.raises(ValueError):
            Alignment(
                video_id="vid",
                track_id="trk",
                video_duration_s=10.0,
                track_duration_s=10.0,
                anchors=[Anchor(0.0, 0.0), Anchor(10.0, 10.0)],
                skips=[SkipSpan(5.0, 5.0)],
            )
