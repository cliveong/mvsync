"""Map a selected media player's SMTC app id onto a running executable."""

from __future__ import annotations


def guess_player_exe(app_id: str) -> str | None:
    """The first running executable whose stem appears in ``app_id``."""
    needle = (app_id or "").strip().lower()
    if not needle:
        return None
    try:
        import psutil

        for process in psutil.process_iter(["name"]):
            name = (process.info.get("name") or "").lower()
            stem = name.removesuffix(".exe")
            if stem and stem in needle:
                return name
    except Exception:
        return None
    return None
