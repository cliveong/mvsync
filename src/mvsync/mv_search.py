"""Find a track's music video via yt-dlp's YouTube search.

Fully automatic: the top-ranked candidate wins when it looks plausible (title,
channel, and length within a window around the track duration). Live takes,
remixes, lyric videos, and audio uploads are penalized so a wrong pick is
unlikely, and the caller prints the choice so mistakes are visible.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mvsync import paths

SEARCH_RESULTS = 8
SEARCH_TIMEOUT_S = 60.0
MIN_LENGTH_SLACK_S = 45.0
MAX_LENGTH_SLACK_S = 150.0
MV_KEYWORDS = (
    "mv",
    "m/v",
    "official video",
    "official music video",
    "music video",
    "뮤직비디오",
    "official mv",
)
ARTIST_TITLE_BONUS = 3.0
ARTIST_CHANNEL_BONUS = 2.0
BAD_KEYWORDS = (
    "lyric",
    "audio",
    "live",
    "remix",
    "cover",
    "instrumental",
    "teaser",
    "behind",
    "reaction",
    "dance practice",
    "performance video",
    "loop",
    "1 hour",
    "sped up",
    "slowed",
    "shorts",
    "subs",
    "subbed",
    "color coded",
    "colour coded",
    "romanization",
    "romanized",
    "hangul",
)


@dataclass(frozen=True)
class Candidate:
    video_id: str
    title: str
    duration_s: float | None
    channel: str


def parse_search_output(text: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = entry.get("id")
        if not video_id:
            continue
        duration = entry.get("duration")
        candidates.append(
            Candidate(
                video_id=str(video_id),
                title=str(entry.get("title") or ""),
                duration_s=float(duration) if isinstance(duration, (int, float)) else None,
                channel=str(entry.get("channel") or entry.get("uploader") or ""),
            )
        )
    return candidates


def search_candidates(query: str, *, root: Path | None = None) -> list[Candidate]:
    ytdlp = paths.find_tool("yt-dlp", root)
    if ytdlp is None:
        raise RuntimeError("yt-dlp is required; run bootstrap.py first")
    try:
        proc = subprocess.run(
            [str(ytdlp), "--dump-json", "--flat-playlist", f"ytsearch{SEARCH_RESULTS}:{query}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SEARCH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("the MV search timed out") from exc
    if proc.returncode != 0:
        tail = " | ".join((proc.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"the MV search failed (rc={proc.returncode}): {tail}")
    return parse_search_output(proc.stdout or "")


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _score(candidate: Candidate, track_duration_s: float, artist: str, title: str) -> float:
    text = candidate.title.lower()
    score = 0.0
    if title.strip() and title.lower() in text:
        score += 2.0
    if artist.strip() and artist.lower() in text:
        score += ARTIST_TITLE_BONUS
    if candidate.channel and artist.strip() and artist.lower() in candidate.channel.lower():
        score += ARTIST_CHANNEL_BONUS
    if any(_has_word(text, keyword) for keyword in MV_KEYWORDS):
        score += 2.0
    score -= 2.0 * sum(1 for keyword in BAD_KEYWORDS if _has_word(text, keyword))
    if _has_word(text, "official") or _has_word(candidate.channel.lower(), "official"):
        score += 1.0
    if candidate.duration_s is not None:
        target = track_duration_s + 20.0
        score += 2.0 - min(2.0, abs(candidate.duration_s - target) / 45.0)
    return score


def rank_candidates(
    candidates: list[Candidate], track_duration_s: float, artist: str, title: str
) -> list[Candidate]:
    return sorted(
        candidates,
        key=lambda candidate: _score(candidate, track_duration_s, artist, title),
        reverse=True,
    )


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _length_eligible(candidates: list[Candidate], track_duration_s: float) -> list[Candidate]:
    low = track_duration_s - MIN_LENGTH_SLACK_S
    high = track_duration_s + MAX_LENGTH_SLACK_S
    return [
        candidate
        for candidate in candidates
        if candidate.duration_s is None or low <= candidate.duration_s <= high
    ]


def pick_best(
    candidates: list[Candidate], track_duration_s: float, artist: str, title: str
) -> Candidate | None:
    eligible = _length_eligible(candidates, track_duration_s)
    if not eligible:
        return None
    return rank_candidates(eligible, track_duration_s, artist, title)[0]


def pick_mv_candidates(
    artist: str, title: str, track_duration_s: float, *, root: Path | None = None
) -> list[Candidate]:
    """Ranked, length-plausible candidates; the top pick is printed for transparency."""
    found = search_candidates(f"{artist} {title} MV", root=root)
    ranked = rank_candidates(
        _length_eligible(found, track_duration_s), track_duration_s, artist, title
    )
    if not ranked:
        raise RuntimeError("no plausible MV found; pass the youtube url explicitly")
    best = ranked[0]
    detail = f"{best.channel}: {best.title}" if best.channel else best.title
    print(f"[search] {detail} ({best.video_id})")
    return ranked


def candidate_lines(candidates: list[Candidate]) -> list[str]:
    """One line per candidate, numbered 1-based as used by prompts and --pick."""
    lines: list[str] = []
    for rank, candidate in enumerate(candidates, start=1):
        detail = (
            f"{candidate.channel}: {candidate.title}" if candidate.channel else candidate.title
        )
        lines.append(f"candidate {rank}: {detail or '(given url)'} ({candidate.video_id})")
    return lines


def prioritize(candidates: list[Candidate], index: int | None) -> list[Candidate]:
    """Move the chosen candidate (1-based, as printed) to the front.

    The rest stay behind it as fallback, so a bad forced pick still falls back
    to the automatic ranking.
    """
    if index is None:
        return candidates
    if not 1 <= index <= len(candidates):
        raise ValueError(f"candidate {index} does not exist; {len(candidates)} were found")
    chosen = candidates[index - 1]
    return [chosen] + [candidate for candidate in candidates if candidate is not chosen]
