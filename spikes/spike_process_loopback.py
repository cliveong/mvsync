"""Spike: record a few seconds of Spotify-only audio and report the level.

Starts paused Spotify if needed, then run inside the venv:
    .venv\\Scripts\\python.exe spikes\\spike_process_loopback.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mvsync import paths  # noqa: E402
from mvsync.capture_proc import ProcessLoopbackRecorder, resolve_spotify_pid  # noqa: E402
from mvsync.player_smtc import PlayerBridge  # noqa: E402

SECONDS = 5.0
PID_TIMEOUT_S = 3.0


def _wait_for_pid(timeout: float = PID_TIMEOUT_S) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = resolve_spotify_pid()
        if pid is not None:
            return pid
        time.sleep(0.1)
    return None


def _wait_until_paused(bridge, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = bridge.current()
        if state is not None and not state.playing:
            return True
        time.sleep(0.1)
    return False


def main() -> int:
    target = paths.ensure_dir(paths.recordings_dir()) / "spike-process.wav"
    bridge = PlayerBridge()
    state = bridge.current()
    was_playing = bool(state and state.playing)
    toggled = False
    try:
        pid = resolve_spotify_pid()
        if not was_playing:
            print("spotify is paused; starting playback for the spike")
            if not bridge.toggle_play_pause():
                print("WARNING: could not start spotify playback")
                return 1
            toggled = True
        if pid is None:
            pid = _wait_for_pid()
        print(f"spotify pid: {pid}")
        if pid is None:
            print("WARNING: spotify has no audio session")
            return 1
        recorder = ProcessLoopbackRecorder(target, pid=pid)
        print(f"recording {SECONDS:.0f}s from the spotify process tree...")
        recorder.start()
        time.sleep(SECONDS)
        recorder.stop()
        print(f"wrote {target}")
        print(f"peak level: {recorder.peak_db:.1f} dBFS")
        if recorder.peak_db < -60.0:
            print("WARNING: near silence; is spotify playing?")
            return 1
        return 0
    finally:
        if toggled:
            bridge.toggle_play_pause()
            if _wait_until_paused(bridge):
                print("restored spotify to paused")
            else:
                print("WARNING: could not confirm spotify was restored to paused")


if __name__ == "__main__":
    raise SystemExit(main())
