import ctypes
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from mvsync import capture, config, paths


def test_sink_writes_pcm_s16_from_float_blocks(tmp_path):
    sink = capture.PcmSink(tmp_path / "a.wav")
    sink.open()
    sink.write(np.array([[0.0, 1.0], [-1.0, 0.5]], dtype=np.float32))
    sink.close()
    with wave.open(str(tmp_path / "a.wav"), "rb") as handle:
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 48000
        assert handle.getnframes() == 2
        assert handle.readframes(2) == b"".join(
            int(v * 32767).to_bytes(2, "little", signed=True)
            for v in (0.0, 1.0, -1.0, 0.5)
        )


def test_sink_tracks_peak_db(tmp_path):
    sink = capture.PcmSink(tmp_path / "a.wav")
    sink.open()
    sink.write(np.full((480, 2), 0.5, dtype=np.float32))
    sink.close()
    assert sink.peak_db == pytest.approx(-6.02, abs=0.1)


def test_sink_peak_is_minus_infinity_when_nothing_was_written(tmp_path):
    sink = capture.PcmSink(tmp_path / "a.wav")
    sink.open()
    sink.close()
    assert sink.peak_db == float("-inf")


def test_sink_clamps_out_of_range_samples(tmp_path):
    sink = capture.PcmSink(tmp_path / "a.wav")
    sink.open()
    sink.write(np.array([[4.0, -4.0]], dtype=np.float32))
    sink.close()
    with wave.open(str(tmp_path / "a.wav"), "rb") as handle:
        assert handle.readframes(1) == (
            (32767).to_bytes(2, "little", signed=True)
            + (-32767).to_bytes(2, "little", signed=True)
        )


def test_sink_close_is_idempotent(tmp_path):
    sink = capture.PcmSink(tmp_path / "a.wav")
    sink.open()
    sink.close()
    sink.close()


def test_is_supported_needs_build_20348():
    from mvsync import capture_proc

    assert capture_proc.is_supported(19045) is False
    assert capture_proc.is_supported(20348) is True
    assert capture_proc.is_supported(26200) is True


def test_process_structs_have_the_win32_layout():
    from mvsync import capture_proc

    assert ctypes.sizeof(capture_proc.WAVEFORMATEX) == 18
    assert ctypes.sizeof(capture_proc.AUDIOCLIENT_ACTIVATION_PARAMS) == 12
    assert ctypes.sizeof(capture_proc.PROPVARIANT) == 24
    assert capture_proc.PROPVARIANT.union.offset == 8
    assert capture_proc._BLOB.pBlobData.offset == 8
    assert (
        capture_proc.PROPVARIANT.union.offset
        + capture_proc._BLOB.pBlobData.offset
        == 16
    )


def test_process_recorder_keeps_its_pid():
    from mvsync import capture_proc

    recorder = capture_proc.ProcessLoopbackRecorder(Path("a.wav"), pid=4242)
    assert recorder._pid == 4242


def test_capture_mode_defaults_to_spotify(tmp_path):
    assert config.load_capture_mode(tmp_path) == "spotify"


def test_capture_mode_round_trips(tmp_path):
    config.set_capture_mode("device", tmp_path)
    assert config.load_capture_mode(tmp_path) == "device"


def test_capture_mode_ignores_malformed_json(tmp_path):
    paths.ensure_dir(tmp_path / "data")
    (tmp_path / "data" / "config.json").write_text("{not json", encoding="utf-8")
    assert config.load_capture_mode(tmp_path) == "spotify"


def test_set_capture_mode_rejects_unknown_values(tmp_path):
    with pytest.raises(ValueError):
        config.set_capture_mode("stereo", tmp_path)


@pytest.mark.parametrize(
    ("mode", "supported", "pid", "expected"),
    [
        ("spotify", True, 4242, "process"),
        ("spotify", False, 4242, "device"),
        ("spotify", True, None, "device"),
        ("device", True, 4242, "device"),
        ("stereo", True, 4242, "device"),
    ],
)
def test_select_backend_matches_the_recorder_factory(mode, supported, pid, expected):
    assert capture.select_backend(mode, supported, pid) == expected


def test_make_recorder_falls_back_when_spotify_is_missing(tmp_path, capsys, monkeypatch):
    from mvsync import capture, capture_proc

    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_spotify_pid", lambda: None)
    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path)
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "Spotify audio session not found; using device loopback" in capsys.readouterr().out


def test_make_recorder_honors_device_mode(tmp_path):
    recorder = capture.make_recorder(tmp_path / "a.wav", mode="device")
    assert isinstance(recorder, capture.LoopbackRecorder)


def test_make_recorder_falls_back_when_process_capture_is_unimportable(
    tmp_path, capsys, monkeypatch
):
    import mvsync

    monkeypatch.delattr(mvsync, "capture_proc", raising=False)
    monkeypatch.setitem(sys.modules, "mvsync.capture_proc", None)
    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path)
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "process capture unavailable" in capsys.readouterr().out


def test_make_recorder_falls_back_when_process_capture_is_unsupported(
    tmp_path, capsys, monkeypatch
):
    from mvsync import capture_proc

    monkeypatch.setattr(capture_proc, "is_supported", lambda: False)
    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path)
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "needs Windows 10 build 20348" in capsys.readouterr().out


def test_make_recorder_falls_back_on_an_unknown_mode(tmp_path, capsys):
    recorder = capture.make_recorder(tmp_path / "a.wav", mode="stereo")
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "unknown capture mode 'stereo'; using device loopback" in capsys.readouterr().out


def test_make_recorder_uses_the_process_backend(tmp_path, monkeypatch):
    from mvsync import capture, capture_proc

    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_spotify_pid", lambda: 4242)
    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path)
    assert isinstance(recorder, capture.ProcessLoopbackRecorder)
    assert recorder._pid == 4242


def test_process_recorder_surfaces_a_wav_close_failure_after_uninitializing(
    tmp_path, monkeypatch
):
    import comtypes

    from mvsync import capture_proc

    class FakeSink:
        def __init__(self, path, **kwargs):
            self.path = path

        def open(self) -> None:
            pass

        def close(self) -> None:
            raise OSError("disk full")

    class FakeClient:
        def GetService(self, iid):
            return self

        def QueryInterface(self, iface):
            return self

        def SetEventHandle(self, handle) -> None:
            pass

        def Start(self) -> None:
            pass

        def Stop(self) -> None:
            pass

    recorder = capture_proc.ProcessLoopbackRecorder(tmp_path / "a.wav", pid=4242)
    recorder._stop.set()
    calls: list[str] = []
    monkeypatch.setattr(capture_proc, "PcmSink", FakeSink)
    monkeypatch.setattr(recorder, "_activate", lambda: FakeClient())
    monkeypatch.setattr(capture_proc._kernel32, "CreateEventW", lambda *args: 123)
    monkeypatch.setattr(capture_proc._kernel32, "CloseHandle", lambda handle: 1)
    monkeypatch.setattr(comtypes, "CoInitializeEx", lambda flags: calls.append("init"))
    monkeypatch.setattr(comtypes, "CoUninitialize", lambda: calls.append("uninit"))

    recorder._run()

    assert calls == ["init", "uninit"]
    with pytest.raises(RuntimeError, match="WAV finalization failed"):
        recorder.stop()
