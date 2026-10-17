import wave

import pytest

from mvsync import mv_audio


def write_wav(path, frames=22050, framerate=22050):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        handle.writeframes(b"\x00\x00" * frames)


def test_parse_video_id_watch_url():
    assert mv_audio.parse_video_id("https://www.youtube.com/watch?v=jNQXAC9IVRw") == "jNQXAC9IVRw"


def test_parse_video_id_variants():
    assert mv_audio.parse_video_id("https://youtu.be/jNQXAC9IVRw?t=5") == "jNQXAC9IVRw"
    assert mv_audio.parse_video_id("https://www.youtube.com/shorts/jNQXAC9IVRw") == "jNQXAC9IVRw"
    assert mv_audio.parse_video_id("https://www.youtube.com/embed/jNQXAC9IVRw") == "jNQXAC9IVRw"


def test_parse_video_id_rejects_garbage():
    with pytest.raises(ValueError):
        mv_audio.parse_video_id("https://example.com/not-a-video")


def test_wav_duration_reads_frames(tmp_path):
    path = tmp_path / "tone.wav"
    write_wav(path)
    assert mv_audio.wav_duration_s(path) == pytest.approx(1.0)


def test_fetch_video_wav_reuses_cache(tmp_path):
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)
    cached = cache / "jNQXAC9IVRw.wav"
    write_wav(cached, frames=1000)
    assert mv_audio.fetch_video_wav("https://youtu.be/jNQXAC9IVRw", root=tmp_path) == cached
