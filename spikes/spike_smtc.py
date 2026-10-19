"""Spike: measure SMTC update cadence for the Spotify desktop client.

Run inside the venv with a track playing in Spotify:
    .venv\\Scripts\\python.exe spikes\\spike_smtc.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mvsync.player_smtc import PlayerBridge  # noqa: E402

SECONDS = 20.0


def main() -> int:
    bridge = PlayerBridge()
    state = bridge.current()
    if state is None:
        print("no player SMTC session found; start playback in your media player")
        return 1
    print(f"session: {state.artist} - {state.title} playing={state.playing}")
    updates = 0
    polls = 0
    last_update = state.updated_epoch_s
    started = time.time()
    while time.time() - started < SECONDS:
        current = bridge.current()
        polls += 1
        if current is not None and current.updated_epoch_s != last_update:
            updates += 1
            last_update = current.updated_epoch_s
            print(f"  update #{updates}: pos={current.position_s:.2f}s playing={current.playing}")
        time.sleep(0.25)
    rate = updates / SECONDS
    print(f"polls={polls} updates={updates} over {SECONDS:.0f}s -> ~{rate:.2f} updates/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
