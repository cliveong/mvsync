"""Spike: fetch an MV's audio into the cache (needs bootstrap tools).

Run inside the venv:
    .venv\\Scripts\\python.exe spikes\\spike_pcm.py [youtube-url]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mvsync.mv_audio import fetch_video_wav, wav_duration_s  # noqa: E402

DEFAULT_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    path = fetch_video_wav(url)
    print(f"cached at {path} ({wav_duration_s(path):.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
