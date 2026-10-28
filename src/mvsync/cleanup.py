"""Startup housekeeping: drop stale recordings and redundant cached wavs."""

from __future__ import annotations

import time
from pathlib import Path

from mvsync import devlog, paths, session

RETENTION_DAYS = 7
SECONDS_PER_DAY = 24 * 60 * 60
MIN_VIDEO_BYTES = 1_000_000


def stale_recordings(
    recordings: Path,
    *,
    now: float | None = None,
    days: int = RETENTION_DAYS,
) -> list[Path]:
    """Recordings older than the retention window, newest last."""
    cutoff = (time.time() if now is None else now) - days * SECONDS_PER_DAY
    return sorted(
        path
        for path in recordings.glob("*.wav")
        if path.is_file() and path.stat().st_mtime < cutoff
    )


def redundant_wavs(cache: Path) -> list[Path]:
    """Cached wavs whose matching mkv can regenerate them."""
    return sorted(
        path
        for path in cache.glob("*.wav")
        if path.is_file() and _has_video(cache / f"{path.stem}.mkv")
    )


def _has_video(video: Path) -> bool:
    return video.is_file() and video.stat().st_size > MIN_VIDEO_BYTES


def _remove(files: list[Path]) -> tuple[int, int]:
    removed = 0
    freed = 0
    for path in files:
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        removed += 1
        freed += size
    return removed, freed


def run_startup_cleanup(
    root: Path | None = None,
    *,
    now: float | None = None,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Remove stale recordings and redundant cache wavs; return count and bytes.

    Runs on main-window launch. A live session lock skips the cleanup, and a
    cleanup failure must never block the window.
    """
    try:
        session.claim(root)
    except RuntimeError as exc:
        print(f"[cleanup] skipped: {exc}")
        return 0, 0
    try:
        recordings = stale_recordings(paths.recordings_dir(root), now=now)
        wavs = redundant_wavs(paths.cache_dir(root))
        if dry_run:
            for path in recordings:
                print(f"[cleanup] would remove stale recording {path.name}")
            for path in wavs:
                print(f"[cleanup] would remove cached wav {path.name}")
            return len(recordings) + len(wavs), sum(
                path.stat().st_size for path in recordings + wavs
            )
        recording_count, recording_bytes = _remove(recordings)
        wav_count, wav_bytes = _remove(wavs)
        count = recording_count + wav_count
        freed = recording_bytes + wav_bytes
        if count:
            print(
                f"[cleanup] removed {recording_count} stale recording(s), "
                f"{wav_count} cached wav(s), {freed / (1024 * 1024):.1f} MB"
            )
            devlog.log(
                "cleanup",
                recordings=recording_count,
                wavs=wav_count,
                bytes=freed,
            )
        return count, freed
    finally:
        session.release(root)
