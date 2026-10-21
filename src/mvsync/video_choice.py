"""Persisted rough video choices, stored in ``data/video_choices.json``.

A choice means "use this video for this track on the next listen, even though
there is no map for it yet." It lets the GUI switch a playing session to a new
video immediately and refine the map from a later full listen.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from mvsync import paths

_LOCK = threading.Lock()


def _load(root: Path | None) -> dict[str, str]:
    try:
        data = json.loads(paths.video_choices_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    choices: dict[str, str] = {}
    for track_id, value in data.items():
        if not isinstance(track_id, str) or not isinstance(value, dict):
            continue
        video_id = value.get("video_id")
        if isinstance(video_id, str) and video_id:
            choices[track_id] = video_id
    return choices


def _save(data: dict[str, str], root: Path | None = None) -> None:
    path = paths.video_choices_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        track_id: {"video_id": video_id}
        for track_id, video_id in sorted(data.items())
    }
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            json.dump(payload, tmp, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def load_video_choice(track_id: str, root: Path | None = None) -> str | None:
    """The video chosen for this track, or None when there is no usable choice."""
    with _LOCK:
        return _load(root).get(track_id)


def save_video_choice(track_id: str, video_id: str, root: Path | None = None) -> None:
    """Remember a video for this track until its map is saved."""
    track_id = track_id.strip()
    video_id = video_id.strip()
    if not track_id or not video_id:
        raise ValueError("a video choice needs a track id and a video id")
    with _LOCK:
        data = _load(root)
        data[track_id] = video_id
        _save(data, root)


def clear_video_choice(track_id: str, root: Path | None = None) -> None:
    """Forget any rough choice for this track."""
    with _LOCK:
        data = _load(root)
        if track_id not in data:
            return
        data.pop(track_id)
        _save(data, root)


def clear_video_choice_if_video(
    track_id: str, video_id: str, root: Path | None = None
) -> None:
    """Clear a choice only when it still points at the video that just got a map."""
    with _LOCK:
        data = _load(root)
        if data.get(track_id) != video_id:
            return
        data.pop(track_id)
        _save(data, root)
