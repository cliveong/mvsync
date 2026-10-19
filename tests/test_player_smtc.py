"""Tests for the SMTC adapter's defensive handling of missing fields."""

from __future__ import annotations

import asyncio
import datetime
import sys
import threading
import types


def make_bridge(monkeypatch, session):
    from mvsync import player_smtc

    bridge = player_smtc.PlayerBridge.__new__(player_smtc.PlayerBridge)
    monkeypatch.setattr(bridge, "_session", lambda: session)
    return bridge


class BlankSession:
    def try_get_media_properties_async(self):
        return None

    def get_timeline_properties(self):
        return None

    def get_playback_info(self):
        return None


def test_current_survives_missing_properties_and_timeline(monkeypatch):
    bridge = make_bridge(monkeypatch, BlankSession())
    state = bridge.current()
    assert state is not None
    assert state.title == ""
    assert state.artist == ""
    assert state.duration_s == 0.0
    assert state.position_s == 0.0
    assert not state.playing


class FullSession:
    class Properties:
        title = "Song"
        artist = "Artist"

    class Timeline:
        end_time = datetime.timedelta(seconds=180.0)
        position = datetime.timedelta(seconds=10.0)
        last_updated_time = datetime.datetime(2026, 1, 1, 12, 0, 0)

    def try_get_media_properties_async(self):
        return FullSession.Properties()

    def get_timeline_properties(self):
        return FullSession.Timeline()

    def get_playback_info(self):
        return object()


def test_current_reads_normal_fields(monkeypatch):
    from mvsync import player_smtc

    monkeypatch.setattr(player_smtc, "_is_playing", lambda playback: True)
    bridge = make_bridge(monkeypatch, FullSession())
    state = bridge.current()
    assert state is not None
    assert (state.title, state.artist) == ("Song", "Artist")
    assert state.duration_s == 180.0
    assert state.position_s == 10.0
    assert state.playing is True


class FakeSession:
    def __init__(self, app_id, playing=False):
        self.source_app_user_model_id = app_id
        self._playing = playing

    def get_playback_info(self):
        return self


def test_choose_prefers_a_playing_session(monkeypatch):
    from mvsync import player_smtc

    monkeypatch.setattr(player_smtc, "_is_playing", lambda playback: playback._playing)
    sessions = [FakeSession("one.exe", playing=False), FakeSession("two.exe", playing=True)]
    bridge = player_smtc.PlayerBridge.__new__(player_smtc.PlayerBridge)
    bridge._match = ""
    assert bridge._choose(sessions).source_app_user_model_id == "two.exe"


def test_choose_filters_by_the_configured_match(monkeypatch):
    from mvsync import player_smtc

    monkeypatch.setattr(player_smtc, "_is_playing", lambda playback: playback._playing)
    sessions = [FakeSession("browser.exe", playing=True), FakeSession("vlc.exe", playing=False)]
    bridge = player_smtc.PlayerBridge.__new__(player_smtc.PlayerBridge)
    bridge._match = "vlc"
    assert bridge._choose(sessions).source_app_user_model_id == "vlc.exe"


def test_choose_returns_none_when_nothing_matches(monkeypatch):
    from mvsync import player_smtc

    monkeypatch.setattr(player_smtc, "_is_playing", lambda playback: playback._playing)
    bridge = player_smtc.PlayerBridge.__new__(player_smtc.PlayerBridge)
    bridge._match = "missing"
    assert bridge._choose([FakeSession("vlc.exe")]) is None


def test_selected_app_id_returns_the_chosen_session(monkeypatch):
    bridge = make_bridge(monkeypatch, FakeSession("vlc.exe"))
    assert bridge.selected_app_id() == "vlc.exe"


def test_selected_app_id_is_none_without_a_session(monkeypatch):
    bridge = make_bridge(monkeypatch, None)
    assert bridge.selected_app_id() is None


class FakeManager:
    def __init__(self, sessions=()):
        self._sessions = list(sessions)

    def get_sessions(self):
        return list(self._sessions)


def install_fake_winsdk(monkeypatch, manager):
    from mvsync import player_smtc

    class FakeSessionManager:
        @staticmethod
        def request_async():
            return object()

    control = types.ModuleType("winsdk.windows.media.control")
    control.GlobalSystemMediaTransportControlsSessionManager = FakeSessionManager
    for name, module in (
        ("winsdk", types.ModuleType("winsdk")),
        ("winsdk.windows", types.ModuleType("winsdk.windows")),
        ("winsdk.windows.media", types.ModuleType("winsdk.windows.media")),
        ("winsdk.windows.media.control", control),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(player_smtc, "_resolve", lambda value: manager)


def test_the_constructor_reads_the_configured_player(monkeypatch):
    from mvsync import player_smtc

    install_fake_winsdk(monkeypatch, FakeManager())
    monkeypatch.setattr(player_smtc.config, "load_player", lambda root=None: "vlc.exe")
    assert player_smtc.PlayerBridge()._match == "vlc.exe"


def test_an_explicit_match_overrides_the_configured_player(monkeypatch):
    from mvsync import player_smtc

    install_fake_winsdk(monkeypatch, FakeManager())
    reads = []
    monkeypatch.setattr(
        player_smtc.config, "load_player", lambda root=None: reads.append(root) or "vlc.exe"
    )
    assert player_smtc.PlayerBridge(match="")._match == ""
    assert reads == []


class PickerSession:
    def __init__(self, app_id, properties, playing=False):
        self.source_app_user_model_id = app_id
        self._properties = properties
        self._playing = playing

    def try_get_media_properties_async(self):
        return self._properties

    def get_playback_info(self):
        return self


class ConcurrencyTracker:
    def __init__(self):
        self._guard = threading.Lock()
        self.active = 0
        self.peak = 0

    def enter(self):
        with self._guard:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def leave(self):
        with self._guard:
            self.active -= 1


class SlowAwaitable:
    """A fake WinRT operation that records how many awaits run at once."""

    def __init__(self, tracker, delay=0.2):
        self._tracker = tracker
        self._delay = delay

    def __await__(self):
        return self._wait().__await__()

    async def _wait(self):
        self._tracker.enter()
        try:
            await asyncio.sleep(self._delay)
            return "done"
        finally:
            self._tracker.leave()


def test_concurrent_resolves_are_serialized(monkeypatch):
    from mvsync import player_smtc

    monkeypatch.setattr(player_smtc, "_LOOP", None)
    tracker = ConcurrencyTracker()
    results = []
    errors = []
    start = threading.Barrier(3)

    def resolve():
        start.wait(timeout=5.0)
        try:
            results.append(player_smtc._resolve(SlowAwaitable(tracker)))
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5.0)
    for thread in threads:
        thread.join(timeout=5.0)

    assert errors == []
    assert results == ["done", "done"]
    assert tracker.peak == 1


def test_sessions_reports_every_session_in_order(monkeypatch):
    from mvsync import player_smtc

    class Properties:
        title = "Song"
        artist = "Artist"

    monkeypatch.setattr(player_smtc, "_is_playing", lambda playback: playback._playing)
    full = PickerSession("vlc.exe", Properties(), playing=True)
    blank = PickerSession("browser.exe", None)
    bridge = player_smtc.PlayerBridge.__new__(player_smtc.PlayerBridge)
    bridge._manager = FakeManager([full, blank])
    sessions = bridge.sessions()
    assert [(s.app_id, s.title, s.artist, s.playing) for s in sessions] == [
        ("vlc.exe", "Song", "Artist", True),
        ("browser.exe", "", "", False),
    ]
