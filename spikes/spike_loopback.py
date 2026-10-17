"""Spike: record a few seconds of system audio and report the level.

Play music first, then run inside the venv:
    .venv\\Scripts\\python.exe spikes\\spike_loopback.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mvsync import paths  # noqa: E402
from mvsync.capture import LoopbackRecorder  # noqa: E402

SECONDS = 5.0


def main() -> int:
    target = paths.ensure_dir(paths.recordings_dir()) / "spike-loopback.wav"
    recorder = LoopbackRecorder(target)
    print(f"recording {SECONDS:.0f}s from the default output device...")
    recorder.start()
    time.sleep(SECONDS)
    recorder.stop()
    print(f"wrote {target}")
    print(f"peak level: {recorder.peak_db:.1f} dBFS")
    if recorder.peak_db < -60.0:
        print("WARNING: near silence; is anything playing?")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
