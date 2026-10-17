"""Tests for the mpv controller adapter without starting a real player."""

from __future__ import annotations

from pathlib import Path


def test_start_passes_keep_open(monkeypatch, tmp_path):
    import mvsync.mpv_ctl as mpv_ctl

    captured: dict[str, object] = {}

    class FakeMPV:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("python_mpv_jsonipc.MPV", FakeMPV)
    monkeypatch.setattr(mpv_ctl.paths, "find_tool", lambda name, root=None: Path("mpv.exe"))

    mpv_ctl.MpvController(root=tmp_path).start()

    assert captured["keep_open"] is True


def test_eof_reached_reads_the_property(tmp_path):
    import mvsync.mpv_ctl as mpv_ctl

    class FakeInner:
        eof_reached = True

    controller = mpv_ctl.MpvController(root=tmp_path)
    controller._mpv = FakeInner()

    assert controller.eof_reached is True
