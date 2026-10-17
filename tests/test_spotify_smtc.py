"""Tests for the SMTC adapter's defensive handling of missing fields."""

from __future__ import annotations

import datetime


def make_bridge(monkeypatch, session):
    from mvsync import spotify_smtc

    bridge = spotify_smtc.SpotifyBridge.__new__(spotify_smtc.SpotifyBridge)
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
    from mvsync import spotify_smtc

    monkeypatch.setattr(spotify_smtc, "_is_playing", lambda playback: True)
    bridge = make_bridge(monkeypatch, FullSession())
    state = bridge.current()
    assert state is not None
    assert (state.title, state.artist) == ("Song", "Artist")
    assert state.duration_s == 180.0
    assert state.position_s == 10.0
    assert state.playing is True
