"""Fetch the music video's audio as a cached mono WAV (yt-dlp + ffmpeg).

Idempotent: a completed cache file is reused; partial files are swapped in
atomically so an interrupted fetch never leaves a broken cache entry.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from mvsync import config, devlog, paths

SAMPLE_RATE = 22050
VIDEO_ID_RE = re.compile(r"(?:v=|/shorts/|/embed/|youtu\.be/)([A-Za-z0-9_-]{11})")
BARE_ID_RE = re.compile(r"^([A-Za-z0-9_-]{11})$")


def video_format(max_height: int) -> str:
    """The yt-dlp selector that caps the video height, keeping a video fallback."""
    return (
        f"bestvideo[height<={max_height}]+bestaudio/"
        "bestvideo+bestaudio/best"
    )


def parse_video_id(url: str) -> str:
    match = VIDEO_ID_RE.search(url) or BARE_ID_RE.match(url.strip())
    if not match:
        raise ValueError(f"cannot parse a YouTube video id from: {url!r}")
    return match.group(1)


def wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


def _extract_wav_from_video(video: Path, part: Path, ffmpeg: Path) -> bool:
    """Decode the cached MV's audio track into a mono 22.05 kHz wav."""
    try:
        proc = subprocess.run(
            [
                str(ffmpeg),
                "-y",
                "-i",
                str(video),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                str(part),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        proc = None
    if proc is None or proc.returncode != 0 or not part.is_file() or part.stat().st_size <= 44:
        part.unlink(missing_ok=True)
        return False
    return True



def _download_wav(
    video_url: str,
    video_id: str,
    cache: Path,
    ytdlp: Path,
    ffmpeg: Path,
) -> Path:
    """Download the MV audio through yt-dlp and ffmpeg."""
    started = time.monotonic()
    part = cache / f"{video_id}.part.wav"
    with tempfile.TemporaryFile() as ytdlp_err, tempfile.TemporaryFile() as ffmpeg_err:
        ytdlp_proc = subprocess.Popen(
            [
                str(ytdlp),
                "-f",
                "bestaudio/best",
                "--no-playlist",
                "--no-progress",
                "-o",
                "-",
                video_url,
            ],
            stdout=subprocess.PIPE,
            stderr=ytdlp_err,
        )
        ffmpeg_proc = subprocess.Popen(
            [
                str(ffmpeg),
                "-y",
                "-i",
                "pipe:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                str(part),
            ],
            stdin=ytdlp_proc.stdout,
            stdout=subprocess.DEVNULL,
            stderr=ffmpeg_err,
        )
        assert ytdlp_proc.stdout is not None
        ytdlp_proc.stdout.close()
        ffmpeg_proc.wait()
        ytdlp_proc.wait()
        ok = ytdlp_proc.returncode == 0 and ffmpeg_proc.returncode == 0 and part.is_file()
        elapsed_s = round(time.monotonic() - started, 2)
        if not ok:
            ytdlp_err.seek(0)
            ffmpeg_err.seek(0)
            detail = ytdlp_err.read().decode("utf-8", "replace").strip().splitlines()
            detail += ffmpeg_err.read().decode("utf-8", "replace").strip().splitlines()
            tail = " | ".join(detail[-4:])
            part.unlink(missing_ok=True)
            devlog.log(
                "cache_miss_failed",
                kind="wav",
                video_id=video_id,
                elapsed_s=elapsed_s,
            )
            raise RuntimeError(f"fetching MV audio failed (rc={ytdlp_proc.returncode}): {tail}")

    final = cache / f"{video_id}.wav"
    os.replace(part, final)
    devlog.log(
        "cache_miss",
        kind="wav",
        video_id=video_id,
        elapsed_s=elapsed_s,
        source="ytdlp",
    )
    return final


def fetch_video_wav(video_url: str, *, root: Path | None = None) -> Path:
    """Extract or download the MV audio track once and cache it under data/cache/."""
    video_id = parse_video_id(video_url)
    cache = paths.ensure_dir(paths.cache_dir(root))
    final = cache / f"{video_id}.wav"
    if final.is_file() and final.stat().st_size > 44:
        devlog.log("cache_hit", kind="wav", video_id=video_id)
        return final

    ytdlp = paths.find_tool("yt-dlp", root)
    ffmpeg = paths.find_tool("ffmpeg", root)
    if ytdlp is None or ffmpeg is None:
        raise RuntimeError(
            "yt-dlp and ffmpeg are required; install yt-dlp from "
            "https://github.com/yt-dlp/yt-dlp/releases"
        )

    video = cache / f"{video_id}.mkv"
    if video.is_file() and video.stat().st_size > 1_000_000:
        part = cache / f"{video_id}.part.wav"
        started = time.monotonic()
        if _extract_wav_from_video(video, part, ffmpeg):
            os.replace(part, final)
            elapsed_s = round(time.monotonic() - started, 2)
            devlog.log(
                "cache_miss",
                kind="wav",
                video_id=video_id,
                elapsed_s=elapsed_s,
                source="video",
            )
            return final
        devlog.log("wav_from_video_failed", video_id=video_id)
    return _download_wav(video_url, video_id, cache, ytdlp, ffmpeg)


def fetch_video_mp4(
    video_url: str,
    *,
    root: Path | None = None,
    max_height: int | None = None,
) -> Path:
    """Download a compact copy of the MV video once and cache it under data/cache/.

    Streaming the MV through mpv is unreliable (YouTube blocks direct stream
    fetches sporadically) and stalls on every hard seek, so playback always
    uses this local file.
    """
    video_id = parse_video_id(video_url)
    cache = paths.ensure_dir(paths.cache_dir(root))
    final = cache / f"{video_id}.mkv"
    if final.is_file() and final.stat().st_size > 1_000_000:
        devlog.log("cache_hit", kind="video", video_id=video_id, bytes=final.stat().st_size)
        return final

    if max_height is None:
        max_height = config.load_max_video_height(root)
    ytdlp = paths.find_tool("yt-dlp", root)
    if ytdlp is None:
        raise RuntimeError(
            "yt-dlp is required; install it from https://github.com/yt-dlp/yt-dlp/releases"
        )

    ffmpeg = paths.find_tool("ffmpeg", root)
    started = time.monotonic()
    part_template = cache / f"{video_id}.part.%(ext)s"
    command = [
        str(ytdlp),
        "-f",
        video_format(max_height),
        "--merge-output-format",
        "mkv",
        "--no-playlist",
        "--no-progress",
    ]
    if ffmpeg is not None:
        command.extend(["--ffmpeg-location", str(ffmpeg)])
    command.extend(["-o", str(part_template), video_url])
    with tempfile.TemporaryFile() as ytdlp_err:
        proc = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=ytdlp_err,
        )
        elapsed_s = round(time.monotonic() - started, 2)
        produced = [
            path
            for path in cache.glob(f"{video_id}.part.*")
            if not re.search(r"\.f\d+\.", path.name)
        ]
        if proc.returncode != 0 or len(produced) != 1:
            ytdlp_err.seek(0)
            detail = ytdlp_err.read().decode("utf-8", "replace").strip().splitlines()
            tail = " | ".join(detail[-4:])
            for leftover in cache.glob(f"{video_id}.part.*"):
                leftover.unlink(missing_ok=True)
            devlog.log(
                "cache_miss_failed",
                kind="video",
                video_id=video_id,
                elapsed_s=elapsed_s,
            )
            raise RuntimeError(f"fetching MV video failed (rc={proc.returncode}): {tail}")

    os.replace(produced[0], final)
    devlog.log(
        "cache_miss",
        kind="video",
        video_id=video_id,
        elapsed_s=elapsed_s,
        bytes=final.stat().st_size,
    )
    return final
