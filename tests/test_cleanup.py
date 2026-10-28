from __future__ import annotations

import os
import time

from mvsync import cleanup


def _age(path, seconds, now):
    stamp = now - seconds
    os.utime(path, (stamp, stamp))


def test_stale_recordings_only_returns_files_older_than_seven_days(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    old = recordings / "pending__old.wav"
    fresh = recordings / "pending__new.wav"
    old.write_bytes(b"old")
    fresh.write_bytes(b"new")
    now = time.time()
    _age(old, 8 * 86400, now)
    _age(fresh, 6 * 86400, now)

    assert cleanup.stale_recordings(recordings, now=now) == [old]


def test_stale_recordings_tolerates_a_missing_folder(tmp_path):
    assert cleanup.stale_recordings(tmp_path / "missing", now=time.time()) == []


def test_redundant_wavs_requires_a_valid_matching_video(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    paired = cache / "paired.wav"
    paired.write_bytes(b"wav")
    (cache / "paired.mkv").write_bytes(b"x" * 1_000_001)
    tiny = cache / "tiny.wav"
    tiny.write_bytes(b"wav")
    (cache / "tiny.mkv").write_bytes(b"x")
    orphan = cache / "orphan.wav"
    orphan.write_bytes(b"wav")

    assert cleanup.redundant_wavs(cache) == [paired]


def test_startup_cleanup_removes_old_recordings_and_paired_wavs(tmp_path):
    data = tmp_path / "data"
    recordings = data / "recordings"
    recordings.mkdir(parents=True)
    cache = data / "cache"
    cache.mkdir()
    old = recordings / "pending__old.wav"
    fresh = recordings / "pending__new.wav"
    old.write_bytes(b"old")
    fresh.write_bytes(b"new")
    paired = cache / "vid1.wav"
    paired.write_bytes(b"wav")
    video = cache / "vid1.mkv"
    video.write_bytes(b"x" * 1_000_001)
    orphan = cache / "vid2.wav"
    orphan.write_bytes(b"wav")
    now = time.time()
    _age(old, 8 * 86400, now)
    _age(fresh, 1 * 86400, now)

    count, freed = cleanup.run_startup_cleanup(tmp_path, now=now)

    assert (count, freed) == (2, 6)
    assert not old.exists()
    assert fresh.exists()
    assert not paired.exists()
    assert video.exists()
    assert orphan.exists()
    assert not (data / "session.lock").exists()


def test_startup_cleanup_dry_run_keeps_the_files(tmp_path, capsys):
    data = tmp_path / "data"
    recordings = data / "recordings"
    recordings.mkdir(parents=True)
    cache = data / "cache"
    cache.mkdir()
    old = recordings / "pending__old.wav"
    old.write_bytes(b"old")
    paired = cache / "vid1.wav"
    paired.write_bytes(b"wav")
    (cache / "vid1.mkv").write_bytes(b"x" * 1_000_001)
    now = time.time()
    _age(old, 8 * 86400, now)

    count, freed = cleanup.run_startup_cleanup(tmp_path, now=now, dry_run=True)

    assert (count, freed) == (2, 6)
    assert old.exists()
    assert paired.exists()
    assert "[cleanup] would remove" in capsys.readouterr().out


def test_startup_cleanup_skips_when_a_session_holds_the_lock(
    tmp_path, monkeypatch, capsys
):
    data = tmp_path / "data"
    recordings = data / "recordings"
    recordings.mkdir(parents=True)
    old = recordings / "pending__old.wav"
    old.write_bytes(b"old")
    now = time.time()
    _age(old, 8 * 86400, now)
    (data / "session.lock").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cleanup.session, "_pid_alive", lambda pid: True)

    count, freed = cleanup.run_startup_cleanup(tmp_path, now=now)

    assert (count, freed) == (0, 0)
    assert old.exists()
    assert "[cleanup] skipped" in capsys.readouterr().out
