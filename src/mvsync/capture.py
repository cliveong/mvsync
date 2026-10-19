"""WASAPI loopback recorder (system output capture), built on SoundCard.

Used by the scan flow to record what the player is playing. All data stays in
``data/recordings`` inside the project folder.
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from mvsync import config, devlog
from mvsync.player_process import guess_player_exe

if TYPE_CHECKING:
    from mvsync.capture_proc import ProcessLoopbackRecorder

SAMPLE_RATE = 48000
CHANNELS = 2
BLOCK_FRAMES = 4800  # 100 ms
SILENT_PEAK_DBFS = -45.0


def default_loopback():
    """The loopback microphone corresponding to the default speaker."""
    import soundcard as sc

    speaker = sc.default_speaker()
    if speaker is None:
        raise RuntimeError("no default output device found")
    return sc.get_microphone(speaker.name, include_loopback=True)


def rms_db(data: np.ndarray) -> float:
    if data.size == 0:
        return float("-inf")
    rms = float(np.sqrt(np.mean(np.square(data.astype(np.float64)))))
    return float(20.0 * np.log10(rms)) if rms > 0 else float("-inf")


class PcmSink:
    """Writes float32 blocks to a 16-bit PCM WAV file and tracks their peak."""

    def __init__(
        self,
        path: Path,
        *,
        samplerate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
    ) -> None:
        self.path = path
        self.samplerate = samplerate
        self.channels = channels
        self.peak_db = float("-inf")
        self.frames_written = 0
        self._wav: wave.Wave_write | None = None

    def open(self) -> None:
        """Open the WAV file and write its header."""
        self._wav = wave.open(str(self.path), "wb")  # noqa: SIM115
        self._wav.setnchannels(self.channels)
        self._wav.setsampwidth(2)
        self._wav.setframerate(self.samplerate)

    def write(self, data: np.ndarray) -> None:
        """Append one float32 ``(frames, channels)`` block in [-1, 1]."""
        if self._wav is None:
            raise RuntimeError("PcmSink is not open")
        self.peak_db = max(self.peak_db, rms_db(data))
        pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2")
        self._wav.writeframes(pcm.tobytes())
        self.frames_written += int(data.shape[0])

    def close(self) -> None:
        """Close the WAV file; safe to call more than once."""
        if self._wav is not None:
            self._wav.close()
            self._wav = None


class LoopbackRecorder:
    """Records the default output device to a WAV file on a worker thread."""

    backend = "device"

    def __init__(
        self,
        path: Path,
        samplerate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        block_frames: int = BLOCK_FRAMES,
    ) -> None:
        self.path = path
        self.samplerate = samplerate
        self.channels = channels
        self.block_frames = block_frames
        self.peak_db = float("-inf")
        self.ready_epoch: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="loopback-recorder", daemon=True)
        self._thread.start()

    def stop(self) -> Path:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        return self.path

    def _run(self) -> None:
        mic = default_loopback()
        sink = PcmSink(self.path, samplerate=self.samplerate, channels=self.channels)
        sink.open()
        try:
            with mic.recorder(
                samplerate=self.samplerate,
                channels=self.channels,
                blocksize=self.block_frames,
            ) as recorder:
                self.ready_epoch = time.time()
                while not self._stop.is_set():
                    sink.write(recorder.record(numframes=self.block_frames))
                    self.peak_db = sink.peak_db
        finally:
            sink.close()


def select_backend(mode: str, supported: bool, pid: int | None) -> str:
    """The backend both the recorder factory and doctor must agree on."""
    if mode == "process" and supported and pid is not None:
        return "process"
    return "device"


def make_recorder(
    path: Path,
    *,
    root: Path | None = None,
    mode: str | None = None,
) -> LoopbackRecorder | ProcessLoopbackRecorder:
    """Pick process capture when usable, otherwise whole-device loopback."""
    chosen = config.normalize_capture_mode(
        mode if mode is not None else config.load_capture_mode(root)
    )
    if chosen == "device":
        devlog.log("capture_backend", mode=chosen, backend="device", reason="configured")
        return LoopbackRecorder(path)
    if chosen != "process":
        print(f"[capture] unknown capture mode {chosen!r}; using device loopback")
        devlog.log("capture_backend", mode=chosen, backend="device", reason="unknown mode")
        return LoopbackRecorder(path)
    player = config.load_player(root)
    exe = config.load_player_exe(root)
    if not exe and player:
        exe = guess_player_exe(player)
    if not exe and not player:
        try:
            from mvsync.player_smtc import PlayerBridge

            app_id = PlayerBridge().selected_app_id()
        except Exception:  # noqa: BLE001 - auto-detection is best effort
            app_id = None
        if app_id:
            exe = guess_player_exe(app_id)
    if not exe:
        print("[capture] no player executable configured; using device loopback")
        devlog.log("capture_backend", mode=chosen, backend="device", reason="no player exe")
        return LoopbackRecorder(path)
    try:
        from mvsync import capture_proc
    except ImportError as exc:
        print(f"[capture] process capture unavailable ({exc}); using device loopback")
        devlog.log(
            "capture_backend", mode=chosen, backend="device", reason="process import failed"
        )
        return LoopbackRecorder(path)
    supported = capture_proc.is_supported()
    pid = capture_proc.resolve_player_pid(exe) if supported else None
    if select_backend(chosen, supported, pid) == "process":
        devlog.log("capture_backend", mode=chosen, backend="process", player_exe=exe, pid=pid)
        return capture_proc.ProcessLoopbackRecorder(path, pid=pid)
    if not supported:
        print("[capture] process capture needs Windows 10 build 20348; using device loopback")
        devlog.log("capture_backend", mode=chosen, backend="device", reason="unsupported os")
    else:
        print(f"[capture] {exe} audio session not found; using device loopback")
        devlog.log(
            "capture_backend",
            mode=chosen,
            backend="device",
            player_exe=exe,
            reason="no player session",
        )
    return LoopbackRecorder(path)


def __getattr__(name: str) -> object:
    """Load the optional process backend only when it is actually referenced."""
    if name == "ProcessLoopbackRecorder":
        from mvsync.capture_proc import ProcessLoopbackRecorder

        return ProcessLoopbackRecorder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
