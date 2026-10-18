"""The opt-in developer log: off by default, one structured file per run."""

from __future__ import annotations

import json

import pytest

from mvsync import paths


@pytest.fixture(autouse=True)
def _reset_dev_log():
    from mvsync import devlog

    devlog.close()
    yield
    devlog.close()


def _write_config(root, payload: dict) -> None:
    path = paths.config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_dev_log_is_off_by_default(tmp_path, monkeypatch):
    from mvsync import devlog

    monkeypatch.delenv(devlog.ENV_VAR, raising=False)
    assert devlog.configure(tmp_path) is None
    devlog.log("ignored", spotify_s=1.0)
    assert not paths.logs_dir(tmp_path).exists()


def test_the_environment_toggle_writes_structured_lines(tmp_path, monkeypatch):
    from mvsync import devlog

    monkeypatch.setenv(devlog.ENV_VAR, "1")
    path = devlog.configure(tmp_path)
    assert path is not None
    assert path.parent == paths.logs_dir(tmp_path)

    devlog.log("poll", spotify_s=12.5, playing=True)
    devlog.close()

    line = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert line["event"] == "poll"
    assert line["spotify_s"] == 12.5
    assert line["playing"] is True
    assert isinstance(line["t"], float)


def test_the_config_toggle_writes_a_file(tmp_path, monkeypatch):
    from mvsync import devlog

    monkeypatch.delenv(devlog.ENV_VAR, raising=False)
    _write_config(tmp_path, {"dev_log": True})
    assert devlog.configure(tmp_path) is not None


def test_a_false_or_unknown_config_value_keeps_it_off(tmp_path, monkeypatch):
    from mvsync import devlog

    monkeypatch.delenv(devlog.ENV_VAR, raising=False)
    _write_config(tmp_path, {"dev_log": "yes"})
    assert devlog.configure(tmp_path) is None
    _write_config(tmp_path, {"something_else": True})
    assert devlog.configure(tmp_path) is None


def test_configure_is_idempotent_and_close_is_safe(tmp_path, monkeypatch):
    from mvsync import devlog

    monkeypatch.setenv(devlog.ENV_VAR, "1")
    first = devlog.configure(tmp_path)
    second = devlog.configure(tmp_path)
    assert first is not None and first == second
    devlog.close()
    devlog.close()


def test_logging_after_close_is_a_no_op(tmp_path, monkeypatch):
    from mvsync import devlog

    monkeypatch.setenv(devlog.ENV_VAR, "1")
    path = devlog.configure(tmp_path)
    devlog.close()
    devlog.log("ignored")
    assert path.read_text(encoding="utf-8") == ""
