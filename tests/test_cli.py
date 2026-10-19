"""Tests for the doctor report, with fakes standing in for hardware probes."""

from __future__ import annotations

import sys
import types
from pathlib import Path

from mvsync import capture, cli, config, paths, player_smtc


class _FakeBridge:
    def current(self):
        return None


class _FakeLoopback:
    name = "fake speaker"


def _fake_tool(name: str, root: Path | None = None) -> Path:
    return Path("tools") / name


def _fake_librosa(monkeypatch) -> None:
    module = types.ModuleType("librosa")
    module.__version__ = "0.0-fake"
    monkeypatch.setitem(sys.modules, "librosa", module)


def _fake_doctor(
    monkeypatch,
    *,
    supported: bool = True,
    pid: int | None = 4242,
    mode: str = "spotify",
) -> None:
    from mvsync import capture_proc

    monkeypatch.setattr(player_smtc, "PlayerBridge", _FakeBridge)
    monkeypatch.setattr(capture, "default_loopback", lambda: _FakeLoopback())
    monkeypatch.setattr(paths, "find_tool", _fake_tool)
    monkeypatch.setattr(config, "load_capture_mode", lambda root=None: mode)
    monkeypatch.setattr(capture_proc, "is_supported", lambda: supported)
    monkeypatch.setattr(capture_proc, "resolve_spotify_pid", lambda: pid)
    _fake_librosa(monkeypatch)


def test_doctor_reports_the_active_capture_backend(capsys, monkeypatch):
    _fake_doctor(monkeypatch, supported=True, pid=4242, mode="spotify")

    cli.doctor()

    out = capsys.readouterr().out
    assert "[doctor] capture:" in out
    assert "mode=spotify" in out
    assert "pid=4242" in out
    assert "backend=process loopback" in out
    assert "[doctor] loopback device:" in out


def test_doctor_reports_device_fallback_without_a_spotify_session(capsys, monkeypatch):
    _fake_doctor(monkeypatch, supported=True, pid=None)

    cli.doctor()

    out = capsys.readouterr().out
    assert "no audio session" in out
    assert "backend=device loopback" in out


def test_doctor_reports_unsupported_process_loopback(capsys, monkeypatch):
    _fake_doctor(monkeypatch, supported=False, pid=None)

    cli.doctor()

    out = capsys.readouterr().out
    assert "[doctor] capture:" in out
    assert "supported=False" in out
    assert "backend=device loopback" in out


def test_doctor_capture_probe_failure_does_not_raise(capsys, monkeypatch):
    _fake_doctor(monkeypatch)
    from mvsync import capture_proc

    def boom() -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(capture_proc, "is_supported", boom)

    cli.doctor()

    out = capsys.readouterr().out
    assert "[doctor] capture:" in out
    assert "boom" in out
    assert "backend=unknown" in out


def test_doctor_reports_device_mode_even_with_a_spotify_session(capsys, monkeypatch):
    _fake_doctor(monkeypatch, supported=True, pid=4242, mode="device")

    cli.doctor()

    out = capsys.readouterr().out
    assert "mode=device" in out
    assert "backend=device loopback" in out


def test_doctor_uses_the_shared_backend_decision(capsys, monkeypatch):
    _fake_doctor(monkeypatch, supported=True, pid=4242)

    monkeypatch.setattr(capture, "select_backend", lambda mode, supported, pid: "device")

    cli.doctor()

    out = capsys.readouterr().out
    assert "backend=device loopback" in out


def test_doctor_reports_when_process_capture_is_not_installed(capsys, monkeypatch):
    _fake_doctor(monkeypatch)
    import mvsync

    monkeypatch.delattr(mvsync, "capture_proc", raising=False)
    monkeypatch.setitem(sys.modules, "mvsync.capture_proc", None)

    cli.doctor()

    out = capsys.readouterr().out
    assert "[doctor] capture:" in out
    assert "process loopback unavailable" in out
    assert "backend=device loopback" in out
