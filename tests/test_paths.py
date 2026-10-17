from pathlib import Path

from mvsync import paths


def test_project_root_contains_package():
    root = paths.project_root()
    assert (root / "pyproject.toml").is_file()
    assert (root / "src" / "mvsync" / "paths.py").is_file()


def test_data_dirs_live_under_root(tmp_path):
    assert paths.cache_dir(tmp_path) == tmp_path / "data" / "cache"
    assert paths.recordings_dir(tmp_path) == tmp_path / "data" / "recordings"
    assert paths.alignments_dir(tmp_path) == tmp_path / "data" / "alignments"


def test_ensure_dir_creates_nested_dir(tmp_path):
    created = paths.ensure_dir(tmp_path / "a" / "b")
    assert created.is_dir()


def test_find_tool_prefers_project_tools_dir(tmp_path):
    tool = tmp_path / "tools" / "mpv" / "mpv.exe"
    tool.parent.mkdir(parents=True)
    tool.write_bytes(b"stub")
    assert paths.find_tool("mpv", root=tmp_path) == tool


def test_find_tool_falls_back_to_path(tmp_path, monkeypatch):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"stub")
    monkeypatch.setenv("PATH", str(tmp_path))
    found = paths.find_tool("ffmpeg", root=tmp_path / "empty-root")
    assert found == Path(fake)


def test_find_tool_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    assert paths.find_tool("yt-dlp", root=tmp_path) is None
