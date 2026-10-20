"""Integration checks for the librosa DTW layer with synthetic audio.

These validate the orientation of ``librosa.sequence.dtw(subseq=True)`` output
(query = Spotify, sequence = video) and the warp-path to anchors/skips
conversion, without any network or external tools. Slow-ish (chroma + DTW), so
they carry the ``dsp`` marker.
"""

import numpy as np
import pytest

from mvsync.align import compute_alignment_from_audio, looks_sane

SR = 22050

pytestmark = pytest.mark.dsp


def _chord(freqs: tuple[float, ...], seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return sum(np.sin(2.0 * np.pi * f * t) for f in freqs) / len(freqs)


def _progression(roots: list[float], seconds_per_chord: float = 2.0) -> np.ndarray:
    return np.concatenate(
        [_chord((root, root * 1.26, root * 1.5), seconds_per_chord) for root in roots]
    )


def _align(spotify: np.ndarray, video: np.ndarray):
    return compute_alignment_from_audio(
        spotify,
        video,
        video_id="vid",
        track_id="trk",
        video_duration_s=len(video) / SR,
        track_duration_s=len(spotify) / SR,
    )


def test_subsequence_alignment_locates_leading_and_trailing_skips():
    spotify = _progression([261.63, 293.66, 329.63, 349.23])  # C D E F, 8 s
    leading = _progression([440.0, 493.88, 523.25], 1.0)  # A B C5, 3 s
    trailing = _progression([207.65, 233.08, 246.94], 1.5)  # low cluster, 4.5 s
    video = np.concatenate([leading, spotify, trailing])

    alignment = _align(spotify, video)

    assert alignment.spotify_to_video(0.0) == pytest.approx(3.0, abs=0.5)
    assert alignment.spotify_to_video(4.0) == pytest.approx(7.0, abs=0.5)

    leading_spans = [span for span in alignment.skips if span.reason == "leading"]
    assert leading_spans
    assert leading_spans[0].video_end_s == pytest.approx(3.0, abs=0.5)

    trailing_spans = [span for span in alignment.skips if span.reason == "trailing"]
    assert trailing_spans
    # DTW tail compression can consume up to about a second before the true
    # match end, so the trailing boundary is only approximate.
    assert alignment.video_duration_s - trailing_spans[0].video_start_s == pytest.approx(
        4.5, abs=1.1
    )


def test_recording_longer_than_video_keeps_orientation():
    music = _progression([261.63, 293.66, 329.63, 349.23])  # C D E F, 8 s
    tail = _progression([392.0, 440.0, 493.88], 1.5)  # G A B, 4.5 s outro
    spotify = np.concatenate([music, tail])  # 12.5 s recording outlives the MV
    video = music  # 8 s MV

    alignment = _align(spotify, video)

    assert alignment.spotify_to_video(0.0) == pytest.approx(0.0, abs=0.5)
    assert alignment.spotify_to_video(4.0) == pytest.approx(4.0, abs=0.6)
    assert alignment.spotify_to_video(11.0) >= 7.0
    assert alignment.anchors[-1].video_s <= alignment.video_duration_s


def test_internal_gap_becomes_skip_span():
    rng = np.random.default_rng(7)
    part_a = _progression([261.63, 293.66, 329.63])  # C D E, 6 s
    part_b = _progression([349.23, 392.0, 440.0])  # F G A, 6 s
    spotify = np.concatenate([part_a, part_b])  # 12 s of continuous music
    talk = 0.3 * rng.standard_normal(int(4.5 * SR))  # 4.5 s, chroma-flat noise
    video = np.concatenate([part_a, talk, part_b])

    alignment = _align(spotify, video)

    internal = [span for span in alignment.skips if span.reason == "internal"]
    assert len(internal) == 1
    span = internal[0]
    # Chroma boundaries on a noise gap land within about a second of the
    # true edit points; the test asserts gross structure, not frame accuracy.
    assert span.video_start_s == pytest.approx(6.0, abs=1.1)
    assert span.video_end_s == pytest.approx(10.5, abs=1.2)

    assert alignment.spotify_to_video(4.0) == pytest.approx(4.0, abs=0.4)
    assert alignment.spotify_to_video(6.1) == pytest.approx(10.5, abs=0.9)


def test_track_cut_becomes_a_hold():
    roots = [261.63, 293.66, 329.63, 349.23, 392.0, 440.0, 493.88, 523.25, 261.63]
    music = _progression(roots)  # 18 s
    video = np.concatenate([music[: int(6 * SR)], music[int(12 * SR) :]])  # 6..12 removed

    alignment = _align(music, video)

    assert len(alignment.holds) == 1
    hold = alignment.holds[0]
    assert hold.spotify_start_s == pytest.approx(4.0, abs=0.8)
    assert hold.spotify_end_s == pytest.approx(10.2, abs=0.8)
    assert hold.video_s == pytest.approx(4.1, abs=0.8)
    assert not [span for span in alignment.skips if span.reason == "internal"]
    assert alignment.spotify_to_video(8.0) == pytest.approx(4.1, abs=0.8)
    assert alignment.spotify_to_video(12.0) == pytest.approx(5.9, abs=0.8)
    assert alignment.spotify_to_video(14.0) == pytest.approx(8.0, abs=0.8)
    assert looks_sane(alignment, len(music) / SR)


def test_repeated_video_tail_is_repaired():
    roots = [261.63, 293.66, 329.63, 349.23, 392.0, 440.0, 493.88, 523.25, 261.63]
    music = _progression(roots)  # 18 s
    video = np.concatenate([music, music[int(6 * SR) :]])  # 30 s, B+C repeated

    alignment = _align(music, video)

    trailing = [span for span in alignment.skips if span.reason == "trailing"]
    assert trailing
    assert trailing[0].video_start_s == pytest.approx(18.0, abs=0.4)
    assert alignment.anchors[-1].video_s == pytest.approx(18.0, abs=0.4)
    assert alignment.anchors[-1].spotify_s == pytest.approx(18.0, abs=0.4)
    assert alignment.segments[0].rate == pytest.approx(1.0, abs=0.03)
    assert alignment.spotify_to_video(17.5) == pytest.approx(17.5, abs=0.4)
    assert looks_sane(alignment, len(music) / SR)


def test_two_talks_keep_gap_boundaries_and_sane_rates():
    roots = [261.63, 293.66, 329.63, 349.23, 392.0, 440.0, 493.88, 523.25]
    spotify = _progression(roots * 2)[: int(15 * SR)]
    rng = np.random.default_rng(7)
    first_talk = 0.3 * rng.standard_normal(int(3 * SR))
    second_talk = 0.3 * rng.standard_normal(int(3 * SR))
    video = np.concatenate(
        [
            spotify[: int(6 * SR)],
            first_talk,
            spotify[int(6 * SR) : int(12 * SR)],
            second_talk,
            spotify[int(12 * SR) :],
        ]
    )

    alignment = _align(spotify, video)

    internal = [span for span in alignment.skips if span.reason == "internal"]
    assert len(internal) == 2
    assert internal[0].video_start_s == pytest.approx(6.0, abs=0.4)
    assert internal[0].video_end_s == pytest.approx(9.0, abs=0.4)
    assert internal[1].video_start_s == pytest.approx(15.0, abs=0.4)
    assert internal[1].video_end_s == pytest.approx(18.0, abs=0.4)
    assert all(segment.rate == pytest.approx(1.0, abs=0.1) for segment in alignment.segments)
    assert looks_sane(alignment, len(spotify) / SR)
    assert alignment.spotify_to_video(6.5) == pytest.approx(9.5, abs=0.2)
    assert alignment.spotify_to_video(12.5) == pytest.approx(18.5, abs=0.2)
    assert alignment.video_to_spotify(9.1) == pytest.approx(6.1, abs=0.2)


def test_two_close_talks_do_not_create_a_spurious_gap():
    roots = [261.63, 293.66, 329.63, 349.23, 392.0, 440.0, 493.88, 523.25]
    spotify = _progression(roots * 2)[: int(14 * SR)]
    rng = np.random.default_rng(7)
    first_talk = 0.3 * rng.standard_normal(int(3 * SR))
    second_talk = 0.3 * rng.standard_normal(int(3 * SR))
    video = np.concatenate(
        [
            spotify[: int(6 * SR)],
            first_talk,
            spotify[int(6 * SR) : int(8 * SR)],
            second_talk,
            spotify[int(8 * SR) :],
        ]
    )

    alignment = _align(spotify, video)

    internal = [span for span in alignment.skips if span.reason == "internal"]
    assert len(internal) == 2
