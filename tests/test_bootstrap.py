"""Tests for the bootstrap status report (the run.bat --check path)."""

from __future__ import annotations

import sys
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
