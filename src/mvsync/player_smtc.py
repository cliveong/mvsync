"""Windows SMTC adapter: observe and control any media player session.

Uses the Windows.Media.Control WinRT API, the same session that feeds the
media-key flyout. No Spotify API, no cookies, no account access. By default
the first playing session is followed; a configured match (an app id
substring) pins the bridge to one player.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

from mvsync import config


@dataclass(frozen=True)
class PlayerState:
    title: str
    artist: str
    duration_s: float
    position_s: float
    updated_epoch_s: float
    playing: bool


@dataclass(frozen=True)
class PlayerSession:
    app_id: str
    title: str
    artist: str
    playing: bool


_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_LOCK = threading.Lock()


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
    with _LOOP_LOCK:
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


class PlayerBridge:
    """Thin adapter so the sync runtime depends on our API, not winsdk."""

    def __init__(self, match: str | None = None) -> None:
        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as SessionManager,
            )
        except ImportError as exc:
            raise RuntimeError("winsdk is not installed; run bootstrap.py") from exc
        self._manager = _resolve(SessionManager.request_async())
        self._match = config.load_player() if match is None else match

    def _sessions(self) -> list:
        return list(self._manager.get_sessions())

    def _choose(self, sessions: list):
        if self._match:
            wanted = self._match.lower()
            for session in sessions:
                app_id = (session.source_app_user_model_id or "").lower()
                if wanted in app_id:
                    return session
            return None
        playing = None
        for session in sessions:
            playback = session.get_playback_info()
            if playback is not None and _is_playing(playback):
                playing = session
                break
        if playing is not None:
            return playing
        return sessions[0] if sessions else None

    def _session(self):
        return self._choose(self._sessions())

    def selected_app_id(self) -> str | None:
        """The app id of the session the bridge currently follows, or None."""
        session = self._session()
        if session is None:
            return None
        return session.source_app_user_model_id or None

    def session_available(self) -> bool:
        return self._session() is not None

    def sessions(self) -> list[PlayerSession]:
        """Every SMTC session with its metadata, for the player picker."""
        result: list[PlayerSession] = []
        for session in self._sessions():
            properties = _resolve(session.try_get_media_properties_async())
            playback = session.get_playback_info()
            result.append(
                PlayerSession(
                    app_id=session.source_app_user_model_id or "",
                    title=(properties.title or "") if properties is not None else "",
                    artist=(properties.artist or "") if properties is not None else "",
                    playing=_is_playing(playback) if playback is not None else False,
                )
            )
        return result

    def current(self) -> PlayerState | None:
        session = self._session()
        if session is None:
            return None
        properties = _resolve(session.try_get_media_properties_async())
        timeline = session.get_timeline_properties()
        playback = session.get_playback_info()
        return PlayerState(
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
