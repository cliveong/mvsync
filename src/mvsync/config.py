"""Persisted settings (JSON, atomic), stored in ``data/config.json``."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from mvsync import paths

DEFAULT_CAPTURE_MODE = "spotify"
CAPTURE_MODES = ("spotify", "device")


def _load(root: Path | None) -> dict:
    try:
        data = json.loads(paths.config_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_capture_mode(root: Path | None = None) -> str:
    """The persisted capture mode, or the default when the file is unusable."""
    mode = _load(root).get("capture_mode")
    return mode if mode in CAPTURE_MODES else DEFAULT_CAPTURE_MODE


def load_dev_log(root: Path | None = None) -> bool:
    """Whether the opt-in developer log is switched on in the config file."""
    return _load(root).get("dev_log") is True


def set_capture_mode(mode: str, root: Path | None = None) -> None:
    """Validate and atomically persist the capture mode."""
    if mode not in CAPTURE_MODES:
        raise ValueError(f"unknown capture mode: {mode!r}")
    path = paths.config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"capture_mode": mode}, indent=2)
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
