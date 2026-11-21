"""Tests for the CLI entry points and the doctor report, with fakes standing in."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from mvsync import capture, cli, config, paths, player_smtc


class _FakeBridge:
    def current(self):
        return None

    def selected_app_id(self):
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
    mode: str = "process",
    player: str = "vlc.exe",
    exe: str = "vlc.exe",
) -> None:
    from mvsync import capture_proc

    monkeypatch.setattr(player_smtc, "PlayerBridge", _FakeBridge)
    monkeypatch.setattr(capture, "default_loopback", lambda: _FakeLoopback())
    monkeypatch.setattr(paths, "find_tool", _fake_tool)
    monkeypatch.setattr(config, "load_capture_mode", lambda root=None: mode)
    monkeypatch.setattr(config, "load_player", lambda root=None: player)
    monkeypatch.setattr(config, "load_player_exe", lambda root=None: exe)
    monkeypatch.setattr(capture_proc, "is_supported", lambda: supported)
    monkeypatch.setattr(capture_proc, "resolve_player_pid", lambda exe: pid)
    _fake_librosa(monkeypatch)


def test_doctor_reports_the_active_capture_backend(capsys, monkeypatch):
    _fake_doctor(monkeypatch, supported=True, pid=4242, mode="process")

    cli.doctor()

    out = capsys.readouterr().out
    assert "[doctor] capture:" in out
    assert "mode=process" in out
    assert "player=vlc.exe" in out
    assert "exe=vlc.exe" in out
    assert "pid=4242" in out
    assert "backend=process loopback" in out
    assert "[doctor] loopback device:" in out


def test_doctor_reports_device_fallback_without_a_player_session(capsys, monkeypatch):
    _fake_doctor(monkeypatch, supported=True, pid=None)

    cli.doctor()

    out = capsys.readouterr().out
    assert "no audio session" in out
    assert "backend=device loopback" in out


def test_doctor_reports_when_no_player_executable_is_configured(capsys, monkeypatch):
    _fake_doctor(monkeypatch, supported=True, pid=None, player="", exe="")

    class FailingBridge(_FakeBridge):
        def selected_app_id(self):
            raise RuntimeError("no smtc")

    monkeypatch.setattr(player_smtc, "PlayerBridge", FailingBridge)

    cli.doctor()

    out = capsys.readouterr().out
    assert "exe=none" in out
    assert "no player executable" in out
    assert "backend=device loopback" in out


def test_doctor_uses_the_auto_selected_player(capsys, monkeypatch):
    from mvsync import player_process

    class AutoBridge(_FakeBridge):
        def selected_app_id(self):
            return "vlc"

    _fake_doctor(monkeypatch, supported=True, pid=4242, player="", exe="")
    monkeypatch.setattr(player_smtc, "PlayerBridge", AutoBridge)
    monkeypatch.setattr(player_process, "guess_player_exe", lambda app_id: "vlc.exe")

    cli.doctor()

    out = capsys.readouterr().out
    assert "player=vlc," in out
    assert "exe=vlc.exe" in out
    assert "pid=4242" in out
    assert "backend=process loopback" in out


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


def _fake_interpreter(tmp_path: Path, *, windowed: bool) -> Path:
    """A stand-in venv whose python.exe has (or lacks) a sibling pythonw.exe."""
    exe = tmp_path / "python.exe"
    exe.write_text("")
    if windowed:
        (tmp_path / "pythonw.exe").write_text("")
    return exe


def _fail_detach(pythonw: Path) -> None:
    pytest.fail("the window was detached instead of opening in this process")


def _fake_run_gui(monkeypatch, code: int = 0) -> None:
    from mvsync import gui

    monkeypatch.setattr(gui, "run_gui", lambda: code)


def test_gui_opens_in_a_detached_windowed_process(monkeypatch, tmp_path):
    exe = _fake_interpreter(tmp_path, windowed=True)
    spawned = []
    monkeypatch.setattr(cli.sys, "executable", str(exe))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kw: spawned.append((args, kw)))
    monkeypatch.setattr(cli.paths, "logs_dir", lambda root=None: tmp_path / "logs")
    monkeypatch.delenv(cli._NO_DETACH_ENV, raising=False)
    _fake_run_gui(monkeypatch)

    assert cli._gui() == 0

    ((args, kwargs),) = spawned
    assert args == [str(tmp_path / "pythonw.exe"), "-m", "mvsync", "gui"]
    assert kwargs["creationflags"] == cli._DETACHED_PROCESS
    assert kwargs["stdin"] is cli.subprocess.DEVNULL
    assert kwargs["stdout"] is cli.subprocess.DEVNULL
    assert str(paths.src_dir()) in kwargs["env"]["PYTHONPATH"].split(os.pathsep)
    assert (tmp_path / "logs" / "gui-launch.log").exists()
    # The parent process must not keep a live handle on the launch log.
    assert kwargs["stderr"].closed


def test_gui_runs_in_process_when_already_windowed(monkeypatch, tmp_path):
    exe = tmp_path / "pythonw.exe"
    exe.write_text("")
    monkeypatch.setattr(cli.sys, "executable", str(exe))
    monkeypatch.setattr(cli, "_spawn_gui_window", _fail_detach)
    _fake_run_gui(monkeypatch, code=7)

    assert cli._gui() == 7


def test_gui_runs_in_process_without_a_windowed_interpreter(monkeypatch, tmp_path):
    exe = _fake_interpreter(tmp_path, windowed=False)
    monkeypatch.setattr(cli.sys, "executable", str(exe))
    monkeypatch.setattr(cli, "_spawn_gui_window", _fail_detach)
    _fake_run_gui(monkeypatch, code=3)

    assert cli._gui() == 3


def test_gui_runs_in_process_when_detaching_is_disabled(monkeypatch, tmp_path):
    exe = _fake_interpreter(tmp_path, windowed=True)
    monkeypatch.setattr(cli.sys, "executable", str(exe))
    monkeypatch.setattr(cli, "_spawn_gui_window", _fail_detach)
    monkeypatch.setenv(cli._NO_DETACH_ENV, "1")
    _fake_run_gui(monkeypatch, code=5)

    assert cli._gui() == 5


def test_gui_falls_back_in_process_when_the_detached_launch_fails(monkeypatch, tmp_path):
    exe = _fake_interpreter(tmp_path, windowed=True)

    def boom(args, **kwargs):
        raise OSError("blocked")

    monkeypatch.setattr(cli.sys, "executable", str(exe))
    monkeypatch.setattr(cli.subprocess, "Popen", boom)
    monkeypatch.setattr(cli.paths, "logs_dir", lambda root=None: tmp_path / "logs")
    monkeypatch.delenv(cli._NO_DETACH_ENV, raising=False)
    _fake_run_gui(monkeypatch, code=9)

    assert cli._gui() == 9


def test_windowed_python_ignores_an_empty_interpreter_path():
    assert cli._windowed_python("") is None
