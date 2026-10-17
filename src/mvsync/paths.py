"""Filesystem layout and external tool resolution.

Stdlib only: this module is imported both by the runtime package and by
``bootstrap.py`` before the project venv exists. Every path is derived from
the project root, so the folder stays movable.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_TOOL_CANDIDATES: dict[str, tuple[tuple[str, ...], ...]] = {
    "yt-dlp": (("tools", "yt-dlp.exe"),),
    "mpv": (("tools", "mpv", "mpv.exe"),),
    "ffmpeg": (("tools", "ffmpeg", "bin", "ffmpeg.exe"),),
    "ffprobe": (("tools", "ffmpeg", "bin", "ffprobe.exe"),),
}


def project_root() -> Path:
    """Repository root, derived from this file's location (move-safe)."""
    return Path(__file__).resolve().parents[2]


def src_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "src"


def tools_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "tools"


def data_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "data"


def cache_dir(root: Path | None = None) -> Path:
    return data_dir(root) / "cache"


def recordings_dir(root: Path | None = None) -> Path:
    return data_dir(root) / "recordings"


def alignments_dir(root: Path | None = None) -> Path:
    return data_dir(root) / "alignments"


def mpv_config_dir(root: Path | None = None) -> Path:
    return tools_dir(root) / "mpv-config"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_tool(name: str, root: Path | None = None) -> Path | None:
    """Resolve an external tool: project tools/ first, then PATH."""
    base = root if root is not None else project_root()
    for relative in _TOOL_CANDIDATES.get(name, ()):
        candidate = base.joinpath(*relative)
        if candidate.is_file():
            return candidate
    which = shutil.which(name) or shutil.which(f"{name}.exe")
    return Path(which) if which else None
