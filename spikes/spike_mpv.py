"""Spike: load a video in mpv muted, nudge it, then quit.

Run inside the venv (requires bootstrap tools):
    .venv\\Scripts\\python.exe spikes\\spike_mpv.py [youtube-url]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mvsync.mpv_ctl import MpvController  # noqa: E402

DEFAULT_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def show(value: float | None) -> str:
    return "None" if value is None else f"{value:.2f}"


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    controller = MpvController()
    controller.start()
    print("loading:", url)
    controller.load(url)
    time.sleep(5)
    print("time_pos:", show(controller.time_pos))
    print("duration:", show(controller.duration))
    print("paused:", controller.paused)
    controller.set_rate(1.05)
    time.sleep(1)
    before = controller.time_pos
    controller.seek_absolute(2.0)
    time.sleep(1)
    print(f"seek check: before={show(before)} after={show(controller.time_pos)}")
    controller.stop()
    print("mpv spike ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
