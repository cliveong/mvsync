import pytest

from mvsync.timeline import Alignment, Anchor, SkipSpan


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
