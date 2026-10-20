"""Find a track's music video via yt-dlp's YouTube search.

Fully automatic: the top-ranked candidate wins when it looks plausible (title,
channel, and length within a window around the track duration). Title matches
outrank everything else, and artist evidence in the title, channel, or
description separates different songs with the same title; live takes, remixes,
lyric videos, and unofficial audio uploads are penalized, so a wrong pick is
unlikely. B-sides without an MV fall back to their official audio upload. The
caller prints the choice so mistakes are visible.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from mvsync import devlog, paths

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
    "lyrics",
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
VARIANT_KEYWORDS = (
    "remix",
    "cover",
    "instrumental",
    "live",
    "lyric",
    "lyrics",
    "reaction",
    "dance practice",
    "performance",
    "sped up",
    "slowed",
)
TITLE_DECORATIONS = (
    *MV_KEYWORDS,
    "official",
    "audio",
    "lyric",
    "lyrics",
    "live",
    "remix",
    "cover",
    "instrumental",
    "performance",
    "sped up",
    "slowed",
)
_FEATURED_RE = re.compile(
    r"[\(\[\{][^\)\]\}]*\b(?:feat|ft|featuring)\b[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)
_SEGMENT_SPLIT_RE = re.compile(r"""[()\[\]{}"'“”‘’|/『』「」]|\s+[–—:-]\s+""")


@dataclass(frozen=True)
class Candidate:
    video_id: str
    title: str
    duration_s: float | None
    channel: str
    description: str = ""


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
                description=str(entry.get("description") or ""),
            )
        )
    return candidates


def search_candidates(query: str, *, root: Path | None = None) -> list[Candidate]:
    ytdlp = paths.find_tool("yt-dlp", root)
    if ytdlp is None:
        raise RuntimeError(
            "yt-dlp is required; install it from https://github.com/yt-dlp/yt-dlp/releases"
        )
    started = time.monotonic()
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
        devlog.log("search_failed", query=query, reason="timeout")
        raise RuntimeError("the MV search timed out") from exc
    elapsed_s = round(time.monotonic() - started, 2)
    if proc.returncode != 0:
        tail = " | ".join((proc.stderr or "").strip().splitlines()[-3:])
        devlog.log("search_failed", query=query, rc=proc.returncode, elapsed_s=elapsed_s)
        raise RuntimeError(f"the MV search failed (rc={proc.returncode}): {tail}")
    candidates = parse_search_output(proc.stdout or "")
    devlog.log(
        "search_done",
        query=query,
        elapsed_s=elapsed_s,
        results=len(candidates),
    )
    return candidates


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def _token_list(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _without_artist(tokens: list[str], artist: str) -> list[str]:
    artist_tokens = _token_list(artist)
    if not artist_tokens:
        return tokens
    result: list[str] = []
    index = 0
    while index < len(tokens):
        if tokens[index:index + len(artist_tokens)] == artist_tokens:
            index += len(artist_tokens)
        else:
            result.append(tokens[index])
            index += 1
    return result


def _title_tokens(text: str, artist: str, *, strip_decorations: bool = True) -> list[str]:
    cleaned = _FEATURED_RE.sub(" ", text)
    if strip_decorations:
        for keyword in TITLE_DECORATIONS:
            cleaned = re.sub(rf"\b{re.escape(keyword)}\b", " ", cleaned, flags=re.IGNORECASE)
    return _without_artist(_token_list(cleaned), artist)


def _title_views(title: str, artist: str) -> list[list[str]]:
    base = _FEATURED_RE.sub(" ", title)
    views = [
        _title_tokens(title, artist),
        _title_tokens(title, artist, strip_decorations=False),
    ]
    for segment in _SEGMENT_SPLIT_RE.split(base):
        views.append(_title_tokens(segment, artist))
        views.append(_title_tokens(segment, artist, strip_decorations=False))
    return [view for view in views if view]


def _title_matches(candidate: Candidate, artist: str, title: str) -> bool:
    query = _title_tokens(title, "", strip_decorations=False)
    if not query:
        return False
    views = _title_views(candidate.title, artist)
    if len(query) == 1:
        for view in views:
            if view == query:
                return True
            if len(view) == 2 and query[0] in view:
                other = view[0] if view[1] == query[0] else view[1]
                if not other.isascii():
                    return True
        return False
    size = len(query)
    return any(
        view[index:index + size] == query
        for view in views
        for index in range(len(view) - size + 1)
    )


def _artist_matches(candidate: Candidate, artist: str) -> bool:
    needle = _normalized(artist)
    if not needle:
        return False
    text = _normalized(f"{candidate.title} {candidate.channel} {candidate.description}")
    return re.search(rf"\b{re.escape(needle)}\b", text) is not None


def _is_label_upload(candidate: Candidate) -> bool:
    description = candidate.description.lower()
    return "provided to youtube by" in description or "auto-generated by youtube" in description


def _is_official_channel(candidate: Candidate, artist: str) -> bool:
    channel = candidate.channel.lower()
    if not channel:
        return False
    return _has_word(channel, "official") or (
        bool(artist.strip()) and artist.lower() in channel
    )


def _content_priority(candidate: Candidate, artist: str) -> int:
    """MV first, then official audio, then official live, then everything else."""
    text = candidate.title.lower()
    label_upload = _is_label_upload(candidate)
    official = _is_official_channel(candidate, artist) or label_upload
    if any(_has_word(text, keyword) for keyword in VARIANT_KEYWORDS):
        return 1 if official and _has_word(text, "live") else 0
    if any(_has_word(text, keyword) for keyword in MV_KEYWORDS):
        return 3
    if official and (
        label_upload
        or _has_word(text, "audio")
        or _has_word(candidate.channel.lower(), "topic")
    ):
        return 2
    return 0


def _score(candidate: Candidate, track_duration_s: float, artist: str, title: str) -> float:
    text = candidate.title.lower()
    score = 0.0
    if _title_matches(candidate, artist, title):
        score += 2.0
    if artist.strip() and artist.lower() in text:
        score += ARTIST_TITLE_BONUS
    if candidate.channel and artist.strip() and artist.lower() in candidate.channel.lower():
        score += ARTIST_CHANNEL_BONUS
    if any(_has_word(text, keyword) for keyword in MV_KEYWORDS):
        score += 2.0
    channel_official = _is_official_channel(candidate, artist)
    for keyword in BAD_KEYWORDS:
        if not _has_word(text, keyword):
            continue
        if channel_official and keyword == "audio":
            continue
        score -= 1.0 if channel_official and keyword == "live" else 2.0
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
        key=lambda candidate: (
            _title_matches(candidate, artist, title),
            _artist_matches(candidate, artist),
            _content_priority(candidate, artist),
            _score(candidate, track_duration_s, artist, title),
        ),
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
    found = search_candidates(f"{artist} {title}", root=root)
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
