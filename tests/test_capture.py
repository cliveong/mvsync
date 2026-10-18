import wave

import numpy as np
import pytest

from mvsync import capture


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
