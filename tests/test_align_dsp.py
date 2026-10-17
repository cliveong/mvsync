"""Integration checks for the librosa DTW layer with synthetic audio.

These validate the orientation of ``librosa.sequence.dtw(subseq=True)`` output
(query = Spotify, sequence = video) and the warp-path to anchors/skips
conversion, without any network or external tools. Slow-ish (chroma + DTW), so
they carry the ``dsp`` marker.
"""

import numpy as np
import pytest

from mvsync.align import compute_alignment_from_audio

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
    assert alignment.spotify_to_video(6.0) == pytest.approx(10.5, abs=0.9)
