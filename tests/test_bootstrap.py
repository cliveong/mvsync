"""Tests for the bootstrap status report (the run.bat --check path)."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _fake_env(monkeypatch, *, venv_ok: bool, deps_ok: bool, missing: set[str]) -> None:
    import bootstrap

    def find_tool(name: str, root: Path | None = None) -> Path | None:
        return None if name in missing else Path("tools") / name

    monkeypatch.setattr(bootstrap, "venv_python_ok", lambda python: venv_ok)
    monkeypatch.setattr(bootstrap, "deps_are_current", lambda: deps_ok)
    monkeypatch.setattr(bootstrap.paths, "find_tool", find_tool)


def test_report_status_ok_when_everything_is_present(monkeypatch):
    import bootstrap

    _fake_env(monkeypatch, venv_ok=True, deps_ok=True, missing=set())
    assert bootstrap.report_status() == 0


def test_report_status_fails_when_a_tool_is_missing(monkeypatch):
    import bootstrap

    _fake_env(monkeypatch, venv_ok=True, deps_ok=True, missing={"mpv"})
    assert bootstrap.report_status() == 1


def test_report_status_fails_when_venv_or_deps_are_missing(monkeypatch):
    import bootstrap

    _fake_env(monkeypatch, venv_ok=False, deps_ok=False, missing=set())
    assert bootstrap.report_status() == 1


def test_extract_ffmpeg_copies_both_binaries(tmp_path):
    import bootstrap

    archive = tmp_path / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("ffmpeg-x/bin/ffmpeg.exe", b"ffmpeg")
        zipped.writestr("ffmpeg-x/bin/ffprobe.exe", b"ffprobe")

    target = tmp_path / "tools" / "ffmpeg" / "bin"
    result = bootstrap.extract_ffmpeg(archive, target)

    assert result == target / "ffmpeg.exe"
    assert (target / "ffprobe.exe").is_file()


def test_extract_ffmpeg_returns_none_without_the_binaries(tmp_path):
    import bootstrap

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("readme.txt", b"no binaries")

    assert bootstrap.extract_ffmpeg(archive, tmp_path / "bin") is None


def test_the_ytdlp_hint_points_at_the_official_release():
    import bootstrap

    assert "https://github.com/yt-dlp/yt-dlp/releases" in bootstrap.YTDLP_HINT


def test_ensure_ffmpeg_reuses_the_existing_tools(monkeypatch, tmp_path):
    import bootstrap

    existing = tmp_path / "ffmpeg.exe"

    def find_tool(name, root=None):
        return existing if name in {"ffmpeg", "ffprobe"} else None

    calls: list[str] = []
    monkeypatch.setattr(bootstrap.paths, "find_tool", find_tool)
    monkeypatch.setattr(bootstrap, "download", lambda url, dest: calls.append(url))

    assert bootstrap.ensure_ffmpeg() == existing
    assert calls == []


def test_ensure_ffmpeg_check_only_never_downloads(monkeypatch):
    import bootstrap

    calls: list[str] = []
    monkeypatch.setattr(bootstrap.paths, "find_tool", lambda name, root=None: None)
    monkeypatch.setattr(bootstrap, "download", lambda url, dest: calls.append(url))

    assert bootstrap.ensure_ffmpeg(check_only=True) is None
    assert calls == []


def test_ensure_ffmpeg_downloads_and_extracts(monkeypatch, tmp_path):
    import bootstrap

    tools = tmp_path / "tools"

    def fake_download(url, dest):
        assert url == bootstrap.FFMPEG_URL
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w") as zipped:
            zipped.writestr("build/bin/ffmpeg.exe", b"ffmpeg")
            zipped.writestr("build/bin/ffprobe.exe", b"ffprobe")

    monkeypatch.setattr(bootstrap.paths, "find_tool", lambda name, root=None: None)
    monkeypatch.setattr(bootstrap.paths, "tools_dir", lambda root=None: tools)
    monkeypatch.setattr(bootstrap, "download", fake_download)

    result = bootstrap.ensure_ffmpeg()

    assert result == tools / "ffmpeg" / "bin" / "ffmpeg.exe"
    assert (tools / "ffmpeg" / "bin" / "ffprobe.exe").is_file()
    assert not (tools / ".download" / "ffmpeg.zip").exists()


def test_ensure_ffmpeg_failure_keeps_the_winget_hint(monkeypatch, capsys, tmp_path):
    import bootstrap

    def fake_download(url, dest):
        raise RuntimeError("no network")

    monkeypatch.setattr(bootstrap.paths, "find_tool", lambda name, root=None: None)
    monkeypatch.setattr(bootstrap.paths, "tools_dir", lambda root=None: tmp_path / "tools")
    monkeypatch.setattr(bootstrap, "download", fake_download)

    assert bootstrap.ensure_ffmpeg() is None
    assert "winget install Gyan.FFmpeg" in capsys.readouterr().out


def test_main_warns_but_returns_zero_without_ytdlp(monkeypatch, capsys, tmp_path):
    import bootstrap

    monkeypatch.setattr(bootstrap, "ensure_venv", lambda: tmp_path / "python.exe")
    monkeypatch.setattr(bootstrap, "ensure_deps", lambda python, force: None)
    monkeypatch.setattr(bootstrap.paths, "find_tool", lambda name, root=None: None)
    monkeypatch.setattr(bootstrap, "ensure_mpv", lambda check_only=False: None)
    monkeypatch.setattr(bootstrap, "ensure_ffmpeg", lambda check_only=False: None)
    monkeypatch.setattr(bootstrap, "ensure_mpv_config", lambda ytdlp: tmp_path / "mpv.conf")

    assert bootstrap.main([]) == 0
    assert bootstrap.YTDLP_HINT in capsys.readouterr().out
