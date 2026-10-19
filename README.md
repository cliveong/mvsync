# mvsync

Play a muted YouTube music video in mpv while the track you are already playing
stays the master clock.

## Install

Requirements: Windows 10 or 11 (64-bit), Python 3.12 or newer, and a media
player that appears in the Windows media controls (Spotify, VLC, MusicBee, and
similar). Per-process capture needs Windows build 20348 or newer; older builds
record the whole output device.

1. Install Python 3.12: https://www.python.org/downloads/windows/
2. Install yt-dlp yourself: `winget install yt-dlp.yt-dlp`, or download
   https://github.com/yt-dlp/yt-dlp/releases and put `yt-dlp.exe` in `tools/`.
   Setup does not download or bundle it.
3. Run `setup.bat`. It creates `.venv`, installs the Python dependencies, and
   downloads mpv and ffmpeg into `tools/`.
4. Optional: the main window's Player row picks the player. The selection is
   saved in `data/config.json`; `player`, `player_exe`, `capture_mode`, and
   `dev_log` can also be edited by hand.

mvsync does not provide music, bypass DRM, or redistribute anything. The audio
comes from the player you already use. Captured audio is local, used only to
align the video, and deleted after a successful fit. Use your own accounts and
follow the terms of the services you use.

## How to use

1. Start a song or playlist in your media player.
2. Run `run.bat`. Pick the player in the Player row if it is not selected.
3. Press `Play`. The video follows the player; a first listen plays roughly and
   is recorded so the next listen is fully synced.
4. Use `Scan` to build a map for the current track explicitly, and `Fix MV` to
   choose a different video. The picker's `Back` button returns to the main
   window.
5. During playback: `q` quits, `r` resyncs, `[` and `]` trim the offset by
   50 ms, `p` toggles the player. The mpv window keeps its size and fullscreen
   state when the video changes.

## License

MIT. See `LICENSE`.
