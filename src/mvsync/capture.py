"""WASAPI loopback recorder (system output capture), built on SoundCard.

Used by the scan flow to record what Spotify is playing. All data stays in
``data/recordings`` inside the project folder.
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np

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


class LoopbackRecorder:
    """Records the default output device to a WAV file on a worker thread."""

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
        with wave.open(str(self.path), "wb") as wav:
            wav.setnchannels(self.channels)
            wav.setsampwidth(2)
            wav.setframerate(self.samplerate)
            with mic.recorder(
                samplerate=self.samplerate,
                channels=self.channels,
                blocksize=self.block_frames,
            ) as recorder:
                self.ready_epoch = time.time()
                while not self._stop.is_set():
                    data = recorder.record(numframes=self.block_frames)
                    self.peak_db = max(self.peak_db, rms_db(data))
                    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2")
                    wav.writeframes(pcm.tobytes())
