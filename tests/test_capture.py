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


def _install_fake_audio_modules(monkeypatch, *, pid=4242, process_name="vlc.exe"):
    import types

    session = type("Session", (), {"GetProcessId": lambda self: pid})()
    control = type("Control", (), {"QueryInterface": lambda self, iface: session})()
    enumerator = type(
        "Enumerator",
        (),
        {"GetCount": lambda self: 1, "GetSession": lambda self, index: control},
    )()
    session_manager = type(
        "SessionManager", (), {"GetSessionEnumerator": lambda self: enumerator}
    )()
    device = type("Device", (), {"AudioSessionManager": session_manager})()

    class FakeProcess:
        def __init__(self, process_id):
            self.pid = process_id

        def name(self):
            return process_name

    psutil = types.ModuleType("psutil")
    psutil.Process = FakeProcess

    pycaw = types.ModuleType("pycaw")
    pycaw_api = types.ModuleType("pycaw.api")
    audiopolicy = types.ModuleType("pycaw.api.audiopolicy")
    audiopolicy.IAudioSessionControl2 = object
    constants = types.ModuleType("pycaw.constants")
    constants.DEVICE_STATE = types.SimpleNamespace(ACTIVE=types.SimpleNamespace(value=1))
    constants.EDataFlow = types.SimpleNamespace(eRender=types.SimpleNamespace(value=0))
    utils = types.ModuleType("pycaw.utils")
    utils.AudioUtilities = types.SimpleNamespace(
        GetAllDevices=lambda flow, state: [device]
    )
    pycaw.api = pycaw_api
    pycaw_api.audiopolicy = audiopolicy
    for name, module in (
        ("psutil", psutil),
        ("pycaw", pycaw),
        ("pycaw.api", pycaw_api),
        ("pycaw.api.audiopolicy", audiopolicy),
        ("pycaw.constants", constants),
        ("pycaw.utils", utils),
    ):
        monkeypatch.setitem(sys.modules, name, module)


def test_resolve_player_pid_matches_a_full_path_exe(monkeypatch):
    from mvsync import capture_proc

    _install_fake_audio_modules(monkeypatch)

    assert capture_proc.resolve_player_pid(r"C:\Program Files\VLC\vlc.exe") == 4242


def test_resolve_player_pid_rejects_empty_input(monkeypatch):
    from mvsync import capture_proc

    assert capture_proc.resolve_player_pid("   ") is None


def test_process_recorder_keeps_its_pid():
    from mvsync import capture_proc

    recorder = capture_proc.ProcessLoopbackRecorder(Path("a.wav"), pid=4242)
    assert recorder._pid == 4242


def test_capture_mode_defaults_to_process(tmp_path):
    assert config.load_capture_mode(tmp_path) == "process"


def test_capture_mode_round_trips(tmp_path):
    config.set_capture_mode("device", tmp_path)
    assert config.load_capture_mode(tmp_path) == "device"


def test_capture_mode_ignores_malformed_json(tmp_path):
    paths.ensure_dir(tmp_path / "data")
    (tmp_path / "data" / "config.json").write_text("{not json", encoding="utf-8")
    assert config.load_capture_mode(tmp_path) == "process"


def test_stored_spotify_mode_loads_as_process(tmp_path):
    paths.ensure_dir(tmp_path / "data")
    (tmp_path / "data" / "config.json").write_text(
        '{"capture_mode": "spotify"}', encoding="utf-8"
    )
    assert config.load_capture_mode(tmp_path) == "process"


def test_set_capture_mode_rejects_unknown_values(tmp_path):
    with pytest.raises(ValueError):
        config.set_capture_mode("stereo", tmp_path)


@pytest.mark.parametrize(
    ("mode", "supported", "pid", "expected"),
    [
        ("process", True, 4242, "process"),
        ("process", False, 4242, "device"),
        ("process", True, None, "device"),
        ("device", True, 4242, "device"),
        ("stereo", True, 4242, "device"),
    ],
)
def test_select_backend_matches_the_recorder_factory(mode, supported, pid, expected):
    assert capture.select_backend(mode, supported, pid) == expected


def test_make_recorder_honors_device_mode(tmp_path):
    recorder = capture.make_recorder(tmp_path / "a.wav", mode="device")
    assert isinstance(recorder, capture.LoopbackRecorder)


def test_make_recorder_falls_back_when_process_capture_is_unimportable(
    tmp_path, capsys, monkeypatch
):
    import mvsync

    monkeypatch.setattr("mvsync.capture.config.load_player_exe", lambda root=None: "vlc.exe")
    monkeypatch.delattr(mvsync, "capture_proc", raising=False)
    monkeypatch.setitem(sys.modules, "mvsync.capture_proc", None)
    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path, mode="process")
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "process capture unavailable" in capsys.readouterr().out


def test_make_recorder_falls_back_when_process_capture_is_unsupported(
    tmp_path, capsys, monkeypatch
):
    from mvsync import capture_proc

    monkeypatch.setattr("mvsync.capture.config.load_player_exe", lambda root=None: "vlc.exe")
    monkeypatch.setattr(capture_proc, "is_supported", lambda: False)
    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path, mode="process")
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "needs Windows 10 build 20348" in capsys.readouterr().out


def test_make_recorder_falls_back_on_an_unknown_mode(tmp_path, capsys):
    recorder = capture.make_recorder(tmp_path / "a.wav", mode="stereo")
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "unknown capture mode 'stereo'; using device loopback" in capsys.readouterr().out


def test_make_recorder_uses_the_process_backend(tmp_path, monkeypatch):
    from mvsync import capture, capture_proc

    monkeypatch.setattr("mvsync.capture.config.load_player_exe", lambda root=None: "vlc.exe")
    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_player_pid", lambda exe: 4242)
    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path, mode="process")
    assert isinstance(recorder, capture.ProcessLoopbackRecorder)
    assert recorder._pid == 4242


def test_make_recorder_falls_back_when_no_player_exe_is_known(tmp_path, capsys, monkeypatch):
    from mvsync import capture, player_smtc

    class FailingBridge:
        def __init__(self):
            raise RuntimeError("winsdk is not installed")

    monkeypatch.setattr("mvsync.capture.config.load_player_exe", lambda root=None: "")
    monkeypatch.setattr("mvsync.capture.config.load_player", lambda root=None: "")
    monkeypatch.setattr(player_smtc, "PlayerBridge", FailingBridge)
    recorder = capture.make_recorder(tmp_path / "a.wav", mode="process")
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "no player executable" in capsys.readouterr().out


def test_make_recorder_auto_selects_the_player_when_no_configuration(tmp_path, monkeypatch):
    from mvsync import capture, capture_proc, player_smtc

    class AutoBridge:
        def selected_app_id(self):
            return "vlc"

    guessed = []

    def guess(app_id):
        guessed.append(app_id)
        return "vlc.exe"

    monkeypatch.setattr("mvsync.capture.config.load_player_exe", lambda root=None: "")
    monkeypatch.setattr("mvsync.capture.config.load_player", lambda root=None: "")
    monkeypatch.setattr("mvsync.capture.guess_player_exe", guess)
    monkeypatch.setattr(player_smtc, "PlayerBridge", AutoBridge)
    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_player_pid", lambda exe: 4242)

    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path, mode="process")

    assert guessed == ["vlc"]
    assert isinstance(recorder, capture.ProcessLoopbackRecorder)
    assert recorder._pid == 4242


def test_make_recorder_prefers_a_configured_exe_over_auto_selection(tmp_path, monkeypatch):
    from mvsync import capture, capture_proc, player_smtc

    built = []

    class AutoBridge:
        def __init__(self):
            built.append(True)

        def selected_app_id(self):
            return "vlc"

    resolved = []
    monkeypatch.setattr(
        "mvsync.capture.config.load_player_exe", lambda root=None: "custom.exe"
    )
    monkeypatch.setattr("mvsync.capture.config.load_player", lambda root=None: "")
    monkeypatch.setattr(player_smtc, "PlayerBridge", AutoBridge)
    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(
        capture_proc, "resolve_player_pid", lambda exe: resolved.append(exe) or 4242
    )

    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path, mode="process")

    assert built == []
    assert resolved == ["custom.exe"]
    assert isinstance(recorder, capture.ProcessLoopbackRecorder)
    assert recorder._pid == 4242


def test_make_recorder_falls_back_when_the_player_has_no_audio_session(
    tmp_path, capsys, monkeypatch
):
    from mvsync import capture, capture_proc

    monkeypatch.setattr("mvsync.capture.config.load_player_exe", lambda root=None: "vlc.exe")
    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_player_pid", lambda exe: None)
    recorder = capture.make_recorder(tmp_path / "a.wav", mode="process")
    assert isinstance(recorder, capture.LoopbackRecorder)
    assert "session not found" in capsys.readouterr().out


def test_make_recorder_accepts_the_legacy_spotify_mode(tmp_path, monkeypatch):
    from mvsync import capture, capture_proc

    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_player_pid", lambda exe: 4242)
    monkeypatch.setattr("mvsync.capture.config.load_player_exe", lambda root=None: "spotify.exe")

    recorder = capture.make_recorder(tmp_path / "a.wav", mode="spotify")
    assert isinstance(recorder, capture.ProcessLoopbackRecorder)
    assert recorder._pid == 4242


def test_default_capture_mode_uses_the_process_backend(tmp_path, capsys, monkeypatch):
    from mvsync import capture, capture_proc

    monkeypatch.setattr("mvsync.capture.config.load_player_exe", lambda root=None: "vlc.exe")
    monkeypatch.setattr("mvsync.capture.config.load_player", lambda root=None: "")
    monkeypatch.setattr(capture_proc, "is_supported", lambda: True)
    monkeypatch.setattr(capture_proc, "resolve_player_pid", lambda exe: 4242)

    recorder = capture.make_recorder(tmp_path / "a.wav", root=tmp_path)

    assert config.load_capture_mode(tmp_path) == "process"
    assert isinstance(recorder, capture.ProcessLoopbackRecorder)
    assert recorder._pid == 4242
    assert "unknown capture mode" not in capsys.readouterr().out


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
