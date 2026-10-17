"""Windows SMTC adapter: observe and control the Spotify desktop client.

Uses the Windows.Media.Control WinRT API, the same session that feeds the
media-key flyout. No Spotify API, no cookies, no account access.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class SpotifyState:
    title: str
    artist: str
    duration_s: float
    position_s: float
    updated_epoch_s: float
    playing: bool


_LOOP: asyncio.AbstractEventLoop | None = None


async def _await_operation(operation):
    return await operation


def _resolve(value):
    """Synchronously settle a WinRT IAsyncOperation if needed.

    winsdk (pywinrt) async operations are awaitable only; a private event loop
    is driven to completion so the rest of the app can stay synchronous.
    """
    if value is None:
        return None
    if not hasattr(value, "__await__"):
        return value
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(_await_operation(value))


def _seconds(delta) -> float:
    return delta.total_seconds() if delta is not None else 0.0


def _epoch(moment) -> float:
    return moment.timestamp() if moment is not None else time.time()


def _is_playing(playback_info) -> bool:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
    )

    return playback_info.playback_status == Status.PLAYING


class SpotifyBridge:
    """Thin adapter so the sync runtime depends on our API, not winsdk."""

    def __init__(self) -> None:
        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as SessionManager,
            )
        except ImportError as exc:
            raise RuntimeError("winsdk is not installed; run bootstrap.py") from exc
        self._manager = _resolve(SessionManager.request_async())

    def _session(self):
        for session in self._manager.get_sessions():
            app_id = (session.source_app_user_model_id or "").lower()
            if "spotify" in app_id:
                return session
        return None

    def session_available(self) -> bool:
        return self._session() is not None

    def current(self) -> SpotifyState | None:
        session = self._session()
        if session is None:
            return None
        properties = _resolve(session.try_get_media_properties_async())
        timeline = session.get_timeline_properties()
        playback = session.get_playback_info()
        return SpotifyState(
            title=(properties.title or "") if properties is not None else "",
            artist=(properties.artist or "") if properties is not None else "",
            duration_s=_seconds(timeline.end_time) if timeline is not None else 0.0,
            position_s=_seconds(timeline.position) if timeline is not None else 0.0,
            updated_epoch_s=(
                _epoch(timeline.last_updated_time) if timeline is not None else time.time()
            ),
            playing=_is_playing(playback) if playback is not None else False,
        )

    def toggle_play_pause(self) -> bool:
        session = self._session()
        if session is None:
            return False
        return bool(_resolve(session.try_toggle_play_pause_async()))

    def seek(self, seconds: float) -> bool:
        session = self._session()
        if session is None:
            return False
        ticks = int(seconds * 10_000_000)
        return bool(_resolve(session.try_change_playback_position_async(ticks)))
