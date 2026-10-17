"""Persistence for alignment maps (JSON, atomic, versioned).

Files live in ``data/alignments/<video_id>__<track_id>.json`` and contain only
relative data, so the project folder stays movable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from mvsync.timeline import Alignment, Anchor, SkipSpan

SCHEMA_VERSION = 1
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def make_track_id(artist: str, title: str) -> str:
    """Stable file-safe identity for a Spotify track from SMTC metadata."""
    raw = f"{artist} {title}".strip().lower()
    slug = _SLUG_RE.sub("-", raw).strip("-")[:60]
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{slug or 'track'}-{digest}"


def alignment_path(store_dir: Path, video_id: str, track_id: str) -> Path:
    return store_dir / f"{video_id}__{track_id}.json"


def alignment_to_dict(alignment: Alignment, offset_s: float = 0.0) -> dict:
    return {
        "version": SCHEMA_VERSION,
        "video_id": alignment.video_id,
        "track_id": alignment.track_id,
        "video_duration_s": alignment.video_duration_s,
        "track_duration_s": alignment.track_duration_s,
        "offset_s": offset_s,
        "anchors": [[anchor.video_s, anchor.spotify_s] for anchor in alignment.anchors],
        "skips": [
            [span.video_start_s, span.video_end_s, span.reason, span.confidence]
            for span in alignment.skips
        ],
    }


def alignment_from_dict(data: dict) -> tuple[Alignment, float]:
    version = data.get("version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported alignment schema version: {version!r}")
    anchors = [Anchor(video_s=float(v), spotify_s=float(s)) for v, s in data["anchors"]]
    skips = [
        SkipSpan(
            video_start_s=float(v0),
            video_end_s=float(v1),
            reason=reason,
            confidence=float(conf),
        )
        for v0, v1, reason, conf in data.get("skips", [])
    ]
    alignment = Alignment(
        video_id=data["video_id"],
        track_id=data["track_id"],
        video_duration_s=float(data["video_duration_s"]),
        track_duration_s=float(data["track_duration_s"]),
        anchors=anchors,
        skips=skips,
    )
    return alignment, float(data.get("offset_s", 0.0))


def save_alignment(
    store_dir: Path, alignment: Alignment, offset_s: float = 0.0
) -> Path:
    store_dir.mkdir(parents=True, exist_ok=True)
    path = alignment_path(store_dir, alignment.video_id, alignment.track_id)
    payload = json.dumps(alignment_to_dict(alignment, offset_s), indent=2)
    handle, tmp_name = tempfile.mkstemp(dir=store_dir, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def load_alignment(store_dir: Path, video_id: str, track_id: str) -> tuple[Alignment, float] | None:
    path = alignment_path(store_dir, video_id, track_id)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return alignment_from_dict(data)


def find_alignments(store_dir: Path, track_id: str) -> list[Path]:
    """All stored alignments for a track, most recently written first."""
    if not store_dir.is_dir():
        return []
    matches = list(store_dir.glob(f"*__{track_id}.json"))
    return sorted(matches, key=lambda path: path.stat().st_mtime, reverse=True)


def video_id_from_alignment_path(path: Path) -> str:
    # rsplit: video ids may contain "__"; track ids never do (make_track_id slug).
    return path.name.rsplit("__", 1)[0]
