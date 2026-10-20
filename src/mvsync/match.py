"""Fit a recording against the ranked MV candidates and reject wrong picks.

A wrong video pick (a live take, a different song, an interview clip) cannot
match the loopback recording, and ``looks_sane`` catches exactly that: the fit
then retries the next-ranked candidate. Shared by ``scan`` and the first-listen
recording used in playlist mode.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path

from mvsync.align import Alignment, compute_alignment, looks_sane
from mvsync.mv_audio import fetch_video_wav, wav_duration_s
from mvsync.mv_search import Candidate, video_url

MAX_CANDIDATES = 4
BACKWARD_JUMP_S = 1.0
FORWARD_JUMP_S = 2.5
END_EPS_S = 1.5


def align_against_candidates(
    recording_path: Path,
    candidates: list[Candidate],
    *,
    track_id: str,
    track_duration_s: float,
    spotify_offset_s: float,
    full_scan: bool = True,
    coverage_span_s: float | None = None,
    root: Path | None = None,
    fetch_wav: Callable[[str], Path] | None = None,
    compute: Callable[..., Alignment] | None = None,
) -> tuple[Alignment, str] | None:
    """Align the recording against the candidates; the first sane one wins."""
    fetcher = fetch_wav if fetch_wav is not None else partial(fetch_video_wav, root=root)
    compute_fn = compute if compute is not None else compute_alignment
    for candidate in candidates[:MAX_CANDIDATES]:
        video_wav = fetcher(video_url(candidate.video_id))
        alignment = compute_fn(
            recording_path,
            video_wav,
            video_id=candidate.video_id,
            track_id=track_id,
            video_duration_s=wav_duration_s(video_wav),
            track_duration_s=track_duration_s,
            spotify_offset_s=spotify_offset_s,
        )
        if looks_sane(
            alignment,
            track_duration_s,
            coverage_span_s=coverage_span_s,
            full_scan=full_scan,
        ):
            print(f"[fit] candidate {candidate.video_id} matches the recording")
            return alignment, candidate.video_id
        print(f"[fit] candidate {candidate.video_id} does not match the recording")
    return None
