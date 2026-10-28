import wave

import pytest

from mvsync import mv_audio


def write_wav(path, frames=22050, framerate=22050):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        handle.writeframes(b"\x00\x00" * frames)


def test_video_format_keeps_a_video_fallback():
    assert mv_audio.video_format(1080) == (
        "bestvideo[height<=1080]+bestaudio/bestvideo+bestaudio/best"
    )


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


def fake_mp4_tools(tmp_path, monkeypatch):
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    ytdlp = tmp_path / "tools" / "yt-dlp.exe"
    ffmpeg = tmp_path / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    ytdlp.parent.mkdir(parents=True, exist_ok=True)
    ytdlp.write_bytes(b"x")
    ffmpeg.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.write_bytes(b"x")

    monkeypatch.setattr(
        mv_audio.paths,
        "find_tool",
        lambda name, root=None: {"yt-dlp": ytdlp, "ffmpeg": ffmpeg}[name],
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        (cache / "jNQXAC9IVRw.part.mkv").write_bytes(b"x" * 1_000_001)
        return type("Process", (), {"returncode": 0})()

    monkeypatch.setattr(mv_audio.subprocess, "run", fake_run)
    return cache, calls, ffmpeg


def test_fetch_video_mp4_passes_the_resolved_ffmpeg_to_ytdlp(tmp_path, monkeypatch):
    cache, calls, ffmpeg = fake_mp4_tools(tmp_path, monkeypatch)

    result = mv_audio.fetch_video_mp4("https://youtu.be/jNQXAC9IVRw", root=tmp_path)

    assert result == cache / "jNQXAC9IVRw.mkv"
    selector = calls[0][calls[0].index("-f") + 1]
    assert "height<=1080" in selector
    assert "--ffmpeg-location" in calls[0]
    location = calls[0].index("--ffmpeg-location")
    assert calls[0][location + 1] == str(ffmpeg)


def test_fetch_video_mp4_uses_the_configured_height_cap(tmp_path, monkeypatch):
    config = tmp_path / "data" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('{"max_video_height": 720}', encoding="utf-8")
    _cache, calls, _ffmpeg = fake_mp4_tools(tmp_path, monkeypatch)

    mv_audio.fetch_video_mp4("https://youtu.be/jNQXAC9IVRw", root=tmp_path)

    selector = calls[0][calls[0].index("-f") + 1]
    assert "height<=720" in selector


def test_fetch_video_wav_extracts_from_the_cached_video(tmp_path, monkeypatch):
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)
    video = cache / "jNQXAC9IVRw.mkv"
    video.write_bytes(b"x" * 1_000_001)
    ytdlp = tmp_path / "tools" / "yt-dlp.exe"
    ffmpeg = tmp_path / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    ytdlp.parent.mkdir(parents=True, exist_ok=True)
    ytdlp.write_bytes(b"x")
    ffmpeg.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.write_bytes(b"x")
    monkeypatch.setattr(
        mv_audio.paths,
        "find_tool",
        lambda name, root=None: {"yt-dlp": ytdlp, "ffmpeg": ffmpeg}[name],
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        write_wav(cache / "jNQXAC9IVRw.part.wav", frames=1000)
        return type("Process", (), {"returncode": 0})()

    monkeypatch.setattr(mv_audio.subprocess, "run", fake_run)
    monkeypatch.setattr(
        mv_audio.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("the cached video should be used"),
    )

    result = mv_audio.fetch_video_wav("https://youtu.be/jNQXAC9IVRw", root=tmp_path)

    assert result == cache / "jNQXAC9IVRw.wav"
    assert str(video) in calls[0]
    assert "-vn" in calls[0]


def test_fetch_video_wav_falls_back_when_extraction_fails(
    tmp_path, monkeypatch
):
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)
    (cache / "jNQXAC9IVRw.mkv").write_bytes(b"x" * 1_000_001)
    ytdlp = tmp_path / "tools" / "yt-dlp.exe"
    ffmpeg = tmp_path / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    ytdlp.parent.mkdir(parents=True, exist_ok=True)
    ytdlp.write_bytes(b"x")
    ffmpeg.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.write_bytes(b"x")
    monkeypatch.setattr(
        mv_audio.paths,
        "find_tool",
        lambda name, root=None: {"yt-dlp": ytdlp, "ffmpeg": ffmpeg}[name],
    )
    monkeypatch.setattr(mv_audio, "_extract_wav_from_video", lambda *args: False)
    downloaded = []

    def fake_download(video_url, video_id, cache_dir, ytdlp_path, ffmpeg_path):
        downloaded.append(video_id)
        final = cache_dir / f"{video_id}.wav"
        write_wav(final, frames=1000)
        return final

    monkeypatch.setattr(mv_audio, "_download_wav", fake_download)

    result = mv_audio.fetch_video_wav("https://youtu.be/jNQXAC9IVRw", root=tmp_path)

    assert downloaded == ["jNQXAC9IVRw"]
    assert result == cache / "jNQXAC9IVRw.wav"


def test_extract_wav_from_video_removes_a_failed_part(tmp_path, monkeypatch):
    part = tmp_path / "jNQXAC9IVRw.part.wav"
    part.write_bytes(b"partial")
    monkeypatch.setattr(
        mv_audio.subprocess,
        "run",
        lambda *args, **kwargs: type("Process", (), {"returncode": 1})(),
    )

    ok = mv_audio._extract_wav_from_video(
        tmp_path / "video.mkv", part, tmp_path / "ffmpeg.exe"
    )

    assert ok is False
    assert not part.exists()
