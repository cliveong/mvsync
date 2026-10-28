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
    assert captured["auto_window_resize"] is False
    assert captured["keepaspect_window"] is False


def test_eof_reached_reads_the_property(tmp_path):
    import mvsync.mpv_ctl as mpv_ctl

    class FakeInner:
        eof_reached = True

    controller = mpv_ctl.MpvController(root=tmp_path)
    controller._mpv = FakeInner()

    assert controller.eof_reached is True


def start_captured(monkeypatch, tmp_path, payload):
    import json

    import mvsync.mpv_ctl as mpv_ctl
    from mvsync import paths

    config_path = paths.config_path(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    captured: dict[str, object] = {}

    class FakeMPV:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("python_mpv_jsonipc.MPV", FakeMPV)
    monkeypatch.setattr(mpv_ctl.paths, "find_tool", lambda name, root=None: Path("mpv.exe"))
    mpv_ctl.MpvController(root=tmp_path).start()
    return captured


def test_start_omits_vsr_filter_by_default(monkeypatch, tmp_path):
    captured = start_captured(monkeypatch, tmp_path, {})

    assert "vf" not in captured


def test_start_adds_nvidia_vsr_filter_when_enabled(monkeypatch, tmp_path):
    captured = start_captured(
        monkeypatch,
        tmp_path,
        {"rtx_vsr": True, "rtx_vsr_scale": 1.5},
    )

    assert captured["vf"] == "d3d11vpp=scale=1.5:scaling-mode=nvidia"


def test_start_uses_default_vsr_scale(monkeypatch, tmp_path):
    captured = start_captured(monkeypatch, tmp_path, {"rtx_vsr": True})

    assert captured["vf"] == "d3d11vpp=scale=2:scaling-mode=nvidia"
