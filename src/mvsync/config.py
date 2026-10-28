"""Persisted settings (JSON, atomic), stored in ``data/config.json``."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

from mvsync import paths

DEFAULT_CAPTURE_MODE = "process"
DEFAULT_MAX_VIDEO_HEIGHT = 1080
MIN_VIDEO_HEIGHT = 144
MAX_VIDEO_HEIGHT = 4320
DEFAULT_RTX_VSR_SCALE = 2.0
MIN_RTX_VSR_SCALE = 1.0
MAX_RTX_VSR_SCALE = 4.0
CAPTURE_MODES = ("process", "device")
LEGACY_CAPTURE_MODES = {"spotify": "process"}


def _load(root: Path | None) -> dict:
    try:
        data = json.loads(paths.config_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict, root: Path | None = None) -> None:
    """Atomically replace the config file with ``data``."""
    path = paths.config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2)
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def normalize_capture_mode(mode: str) -> str:
    """Map a legacy capture-mode name onto its current one."""
    return LEGACY_CAPTURE_MODES.get(mode, mode)


def load_capture_mode(root: Path | None = None) -> str:
    """The persisted capture mode, or the default when the file is unusable."""
    mode = _load(root).get("capture_mode")
    if not isinstance(mode, str):
        return DEFAULT_CAPTURE_MODE
    mode = normalize_capture_mode(mode)
    return mode if mode in CAPTURE_MODES else DEFAULT_CAPTURE_MODE


def load_player(root: Path | None = None) -> str:
    """The persisted player app id, or an empty string."""
    player = _load(root).get("player")
    return player if isinstance(player, str) else ""


def load_player_exe(root: Path | None = None) -> str:
    """The persisted player executable, or an empty string."""
    exe = _load(root).get("player_exe")
    return exe if isinstance(exe, str) else ""


def load_dev_log(root: Path | None = None) -> bool:
    """Whether the opt-in developer log is switched on in the config file."""
    return _load(root).get("dev_log") is True


def load_max_video_height(root: Path | None = None) -> int:
    """The persisted video height cap, or 1080 when unset or invalid."""
    height = _load(root).get("max_video_height")
    if isinstance(height, bool):
        return DEFAULT_MAX_VIDEO_HEIGHT
    if isinstance(height, int) and MIN_VIDEO_HEIGHT <= height <= MAX_VIDEO_HEIGHT:
        return height
    return DEFAULT_MAX_VIDEO_HEIGHT


def load_rtx_vsr(root: Path | None = None) -> bool:
    """Whether the opt-in NVIDIA RTX Video Super Resolution is enabled."""
    return _load(root).get("rtx_vsr") is True


def load_rtx_vsr_scale(root: Path | None = None) -> float:
    """The RTX VSR upscale factor, or 2.0 when unset or invalid."""
    scale = _load(root).get("rtx_vsr_scale")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        return DEFAULT_RTX_VSR_SCALE
    value = float(scale)
    if not math.isfinite(value) or not MIN_RTX_VSR_SCALE < value <= MAX_RTX_VSR_SCALE:
        return DEFAULT_RTX_VSR_SCALE
    return value


def save_player(app_id: str, exe: str, root: Path | None = None) -> None:
    """Persist the player app id and, when known, its executable."""
    data = _load(root)
    data["player"] = app_id
    if exe:
        data["player_exe"] = exe
    else:
        data.pop("player_exe", None)
    _save(data, root)


def set_capture_mode(mode: str, root: Path | None = None) -> None:
    """Validate and atomically persist the capture mode."""
    mode = normalize_capture_mode(mode)
    if mode not in CAPTURE_MODES:
        raise ValueError(f"unknown capture mode: {mode!r}")
    data = _load(root)
    data["capture_mode"] = mode
    _save(data, root)
